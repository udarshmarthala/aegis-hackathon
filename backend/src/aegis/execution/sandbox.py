"""Isolated code execution.

This is where Aegis runs work it does not fully trust: reproducing a failure,
applying a candidate patch, running a test suite, building a service. The
reasoning model never touches production to do any of it.

The isolation properties, and why each one exists:

* **No production credentials, ever.** The container environment is built from
  an explicit allowlist, not inherited from the host. A patch that exfiltrates
  ``os.environ`` finds nothing worth having. This is the single most important
  property in the module and it is enforced by construction rather than by
  remembering to unset things.
* **No network by default.** ``sandbox_network`` defaults to ``none``. A run
  that needs to install dependencies must ask for it explicitly, and that
  request is recorded on the run row.
* **Bounded CPU, memory, processes, disk and wall clock.** A runaway test suite
  is stopped by the daemon, not by hoping it finishes.
* **Read-only root filesystem with a tmpfs workspace.** The image cannot be
  modified, and everything the run writes disappears with the container.
* **Dropped capabilities and no privilege escalation.** A container breakout
  needs a capability the sandbox does not have.
* **Always cleaned up.** Removal runs in a ``finally``; a crashed worker leaves
  at most one container, which the reaper removes by label.

The Docker SDK is synchronous, so every call is dispatched to a worker thread.
Blocking the event loop here would stall every other incident in the process.
"""

from __future__ import annotations

import asyncio
import hashlib
import shlex
import tarfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from io import BytesIO
from typing import Any, Literal

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Settings
from aegis.core.errors import ConfigError, ExternalServiceError, ValidationError
from aegis.core.ids import SANDBOX, new_id
from aegis.core.logging import get_logger

log = get_logger(__name__)

Purpose = Literal["reproduce", "test_patch", "regression", "build", "static_check"]

# Output is truncated at write time. An unbounded log from a runaway test would
# otherwise be able to fill the incident database one row at a time.
MAX_OUTPUT_CHARS = 32_000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_PATCH_BYTES = 1024 * 1024

# Every sandbox container carries this label so the reaper can find orphans
# left by a worker that died mid-run.
SANDBOX_LABEL = "aegis.sandbox"


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """Everything one sandboxed run needs, stated up front.

    Immutable and fully declarative so the same spec can be replayed during an
    audit and produce the same container configuration.
    """

    purpose: Purpose
    command: list[str]
    image: str | None = None
    repo_url: str | None = None
    base_ref: str | None = None
    patch: str | None = None
    workdir: str = "/workspace"
    env: dict[str, str] = field(default_factory=dict)
    network: str | None = None
    timeout_s: int | None = None
    artifact_paths: list[str] = field(default_factory=list)
    incident_id: str | None = None
    action_id: str | None = None

    def __post_init__(self) -> None:
        if not self.command:
            raise ValidationError("a sandbox run needs a command")
        if self.patch is not None and len(self.patch.encode()) > MAX_PATCH_BYTES:
            raise ValidationError(
                f"patch exceeds {MAX_PATCH_BYTES} bytes",
                context={"size": len(self.patch.encode())},
            )
        for key in self.env:
            # The env allowlist is the credential boundary. Anything that looks
            # like a secret name is refused outright rather than trusted to be
            # harmless, because the cost of being wrong here is a leaked key.
            upper = key.upper()
            if any(
                marker in upper
                for marker in ("SECRET", "TOKEN", "PASSWORD", "KEY", "CREDENTIAL")
            ):
                raise ValidationError(
                    f"environment variable {key!r} may not be passed into the sandbox",
                    context={"variable": key},
                )

    @property
    def patch_sha256(self) -> str | None:
        if self.patch is None:
            return None
        return hashlib.sha256(self.patch.encode()).hexdigest()

    @property
    def command_line(self) -> str:
        return shlex.join(self.command)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    """What a run did, in a shape that persists directly to ``sandbox_runs``."""

    id: str
    purpose: Purpose
    image: str
    command: str
    exit_code: int | None
    timed_out: bool
    killed: bool
    duration_ms: int
    stdout: str
    stderr: str
    artifacts: list[dict[str, Any]]
    resource_limits: dict[str, Any]
    network: str
    started_at: datetime
    finished_at: datetime
    repo: str | None = None
    base_ref: str | None = None
    patch_sha256: str | None = None

    @property
    def succeeded(self) -> bool:
        """Exit zero, not killed, not timed out.

        Deliberately narrow. "The command returned 0" is the weakest possible
        notion of success and is never treated as verification on its own -
        that is the verification engine's job (PRD verification section).
        """
        return self.exit_code == 0 and not self.timed_out and not self.killed


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit] + f"\n...[truncated {dropped} characters]"


class SandboxRunner:
    """Runs one bounded command in a disposable container."""

    __slots__ = ("_clock", "_client", "_settings")

    def __init__(self, settings: Settings, *, clock: Clock = SYSTEM_CLOCK) -> None:
        self._settings = settings
        self._clock = clock
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return self._settings.sandbox_enabled

    def _docker(self) -> Any:
        """Connect lazily so a host without Docker can still boot the API.

        Sandboxing is a capability, not a hard dependency: an Aegis that cannot
        run code can still detect, investigate and diagnose.
        """
        if self._client is not None:
            return self._client
        if not self._settings.sandbox_enabled:
            raise ConfigError(
                "sandbox is disabled; set SANDBOX_ENABLED=true to run code",
                context={"capability": "sandbox"},
            )
        try:
            import docker
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ConfigError(
                "the docker SDK is not installed; sandboxed execution is unavailable"
            ) from exc
        try:
            self._client = docker.DockerClient(
                base_url=self._settings.sandbox_docker_host, timeout=30
            )
        except Exception as exc:
            raise ExternalServiceError(
                f"cannot reach the docker daemon: {type(exc).__name__}",
                code="SANDBOX_UNAVAILABLE",
                retryable=False,
                context={"docker_host": self._settings.sandbox_docker_host},
            ) from exc
        return self._client

    async def healthy(self) -> bool:
        """Cheap probe for the integration-health surface."""
        if not self._settings.sandbox_enabled:
            return False
        try:
            client = self._docker()
            await asyncio.to_thread(client.ping)
        except Exception as exc:  # noqa: BLE001 - health probes never raise
            log.warning("sandbox health check failed", error=str(exc))
            return False
        return True

    def _host_config(self, spec: SandboxSpec) -> dict[str, Any]:
        """Container limits, assembled in one place so they cannot drift apart.

        Every field here is a containment control. Changing one is a
        security-relevant change.
        """
        s = self._settings
        return {
            "mem_limit": s.sandbox_memory_limit,
            "memswap_limit": s.sandbox_memory_limit,  # no swap escape hatch
            "nano_cpus": int(s.sandbox_cpu_limit * 1_000_000_000),
            "pids_limit": 512,
            "network_mode": spec.network or s.sandbox_network,
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "tmpfs": {
                # The only writable location, capped so a run cannot fill the host.
                spec.workdir: "rw,size=512m,mode=1777",
                "/tmp": "rw,size=128m,mode=1777",  # noqa: S108 - container-local tmpfs
            },
            "auto_remove": False,  # removed explicitly so logs survive to be read
        }

    @staticmethod
    def _safe_env(spec: SandboxSpec) -> dict[str, str]:
        """Build the container environment from an allowlist only.

        Nothing is inherited from the host process. This is what guarantees no
        production credential is reachable from inside a sandbox, whatever the
        executed code tries to read.
        """
        env = {
            "HOME": spec.workdir,
            "PATH": "/usr/local/bin:/usr/local/sbin:/usr/bin:/bin",
            "CI": "true",
            "AEGIS_SANDBOX": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        env.update(spec.env)
        return env

    @staticmethod
    def _bootstrap_script(spec: SandboxSpec) -> str:
        """The shell preamble that prepares the workspace.

        Checkout, patch application and the user command are separate steps with
        distinct exit codes, so "the patch did not apply" is distinguishable
        from "the tests failed". Conflating them would let a malformed patch be
        reported as a failing fix.
        """
        lines = ["set -eu", f"cd {shlex.quote(spec.workdir)}"]
        if spec.repo_url:
            ref = spec.base_ref or "HEAD"
            lines += [
                "git init -q .",
                f"git remote add origin {shlex.quote(spec.repo_url)}",
                f"git fetch --depth 1 -q origin {shlex.quote(ref)} || "
                f"{{ echo 'AEGIS: fetch of {ref} failed' >&2; exit 90; }}",
                "git checkout -q FETCH_HEAD",
                # Detached HEAD on a throwaway branch: nothing can be pushed and
                # no local state survives the container.
                "git switch -c aegis/sandbox -q",
            ]
        if spec.patch:
            lines += [
                "git apply --check /aegis/patch.diff 2>/tmp/apply.err || "
                "{ echo 'AEGIS: patch does not apply'; cat /tmp/apply.err >&2; exit 91; }",
                "git apply /aegis/patch.diff",
                "echo 'AEGIS: patch applied'",
            ]
        lines.append("exec " + spec.command_line)
        return "\n".join(lines)

    async def run(self, spec: SandboxSpec) -> SandboxResult:
        """Execute one spec. Always cleans up, whatever happens.

        Returns a result for every terminal condition including timeout and
        non-zero exit - those are outcomes to record, not exceptions. Only an
        unusable sandbox (no daemon, missing image) raises.
        """
        run_id = new_id(SANDBOX)
        image = spec.image or self._settings.sandbox_image
        timeout_s = spec.timeout_s or self._settings.sandbox_wall_clock_limit_s
        started_at = self._clock.now()
        started_perf = time.perf_counter()
        client = self._docker()
        container: Any = None
        timed_out = False
        killed = False
        exit_code: int | None = None

        log.info(
            "sandbox starting",
            sandbox_id=run_id,
            purpose=spec.purpose,
            image=image,
            network=spec.network or self._settings.sandbox_network,
            timeout_s=timeout_s,
            incident_id=spec.incident_id,
        )

        try:
            container = await asyncio.to_thread(
                client.containers.create,
                image=image,
                command=["/bin/sh", "-c", self._bootstrap_script(spec)],
                working_dir=spec.workdir,
                environment=self._safe_env(spec),
                labels={
                    SANDBOX_LABEL: "1",
                    "aegis.sandbox.id": run_id,
                    "aegis.incident.id": spec.incident_id or "",
                    "aegis.purpose": spec.purpose,
                },
                detach=True,
                **self._host_config(spec),
            )

            if spec.patch:
                await self._put_patch(container, spec.patch)

            await asyncio.to_thread(container.start)

            try:
                status = await asyncio.wait_for(
                    asyncio.to_thread(container.wait, timeout=timeout_s + 5),
                    timeout=timeout_s,
                )
                exit_code = int(status.get("StatusCode", -1))
            except TimeoutError:
                timed_out = True
                killed = True
                log.warning(
                    "sandbox timed out; killing",
                    sandbox_id=run_id,
                    timeout_s=timeout_s,
                )
                with suppress(Exception):
                    await asyncio.to_thread(container.kill)
            except asyncio.CancelledError:
                # Cancellation must still stop the container, then propagate:
                # swallowing it would leave the caller believing the run
                # continues while the worker is shutting down.
                killed = True
                with suppress(Exception):
                    await asyncio.to_thread(container.kill)
                raise

            stdout, stderr = await self._collect_logs(container)
            artifacts = await self._collect_artifacts(container, spec)

        except (ConfigError, ExternalServiceError):
            raise
        except Exception as exc:
            raise ExternalServiceError(
                f"sandbox run failed to start: {type(exc).__name__}: {exc}",
                code="SANDBOX_FAILED",
                retryable=False,
                context={"sandbox_id": run_id, "image": image},
            ) from exc
        finally:
            if container is not None:
                with suppress(Exception):
                    await asyncio.to_thread(container.remove, force=True, v=True)

        finished_at = self._clock.now()
        duration_ms = int((time.perf_counter() - started_perf) * 1000)
        result = SandboxResult(
            id=run_id,
            purpose=spec.purpose,
            image=image,
            command=spec.command_line,
            exit_code=exit_code,
            timed_out=timed_out,
            killed=killed,
            duration_ms=duration_ms,
            stdout=_truncate(stdout),
            stderr=_truncate(stderr),
            artifacts=artifacts,
            resource_limits={
                "cpu": self._settings.sandbox_cpu_limit,
                "memory": self._settings.sandbox_memory_limit,
                "pids": 512,
                "timeout_s": timeout_s,
            },
            network=spec.network or self._settings.sandbox_network,
            started_at=started_at,
            finished_at=finished_at,
            repo=spec.repo_url,
            base_ref=spec.base_ref,
            patch_sha256=spec.patch_sha256,
        )
        log.info(
            "sandbox finished",
            sandbox_id=run_id,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_ms=duration_ms,
            succeeded=result.succeeded,
        )
        return result

    @staticmethod
    async def _put_patch(container: Any, patch: str) -> None:
        """Place the diff inside the container as a tar stream.

        Written to ``/aegis/`` rather than the workspace so that a patch which
        adds files cannot collide with, or overwrite, the diff being applied.
        """
        payload = patch.encode()
        buf = BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="patch.diff")
            info.size = len(payload)
            info.mode = 0o444
            tar.addfile(info, BytesIO(payload))
        buf.seek(0)
        ok = await asyncio.to_thread(container.put_archive, "/aegis", buf.getvalue())
        if not ok:
            raise ExternalServiceError(
                "could not stage the patch inside the sandbox",
                code="SANDBOX_FAILED",
                retryable=False,
            )

    @staticmethod
    async def _collect_logs(container: Any) -> tuple[str, str]:
        """Read stdout and stderr separately.

        Interleaving them would make it impossible to tell a test's own
        diagnostics from the harness errors the bootstrap emits.
        """

        def _read(stream: str) -> str:
            kwargs = {"stdout": stream == "stdout", "stderr": stream == "stderr"}
            raw = container.logs(**kwargs)
            if isinstance(raw, bytes):
                return raw.decode("utf-8", errors="replace")
            return str(raw)

        stdout = await asyncio.to_thread(_read, "stdout")
        stderr = await asyncio.to_thread(_read, "stderr")
        return stdout, stderr

    @staticmethod
    async def _collect_artifacts(
        container: Any, spec: SandboxSpec
    ) -> list[dict[str, Any]]:
        """Copy declared artifact paths out before the container is removed.

        Only paths the spec named are copied, each capped in size. A run cannot
        decide after the fact to export something the caller never asked for.
        """
        artifacts: list[dict[str, Any]] = []
        for path in spec.artifact_paths[:10]:
            try:
                stream, stat = await asyncio.to_thread(container.get_archive, path)
            except Exception as exc:  # noqa: BLE001 - a missing artifact is a fact
                artifacts.append(
                    {"path": path, "collected": False, "reason": type(exc).__name__}
                )
                continue
            size = int(stat.get("size", 0))
            if size > MAX_ARTIFACT_BYTES:
                artifacts.append(
                    {
                        "path": path,
                        "collected": False,
                        "reason": f"exceeds {MAX_ARTIFACT_BYTES} bytes ({size})",
                    }
                )
                continue
            blob = b"".join(stream)
            artifacts.append(
                {
                    "path": path,
                    "collected": True,
                    "size": len(blob),
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "preview": blob[:2000].decode("utf-8", errors="replace"),
                }
            )
        return artifacts

    async def reap_orphans(self, *, older_than_s: int = 3600) -> int:
        """Remove sandbox containers left behind by a worker that died.

        Identified by label rather than by name, so a container started by a
        previous process generation is still recognised as ours.
        """
        try:
            client = self._docker()
            containers = await asyncio.to_thread(
                client.containers.list,
                all=True,
                filters={"label": SANDBOX_LABEL},
            )
        except Exception as exc:  # noqa: BLE001 - reaping never breaks the worker
            log.warning("sandbox reap failed", error=str(exc))
            return 0

        removed = 0
        cutoff = time.time() - older_than_s
        for container in containers:
            created = container.attrs.get("Created", "")
            try:
                created_ts = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
            except ValueError:
                created_ts = 0.0
            if created_ts and created_ts > cutoff:
                continue
            with suppress(Exception):
                await asyncio.to_thread(container.remove, force=True, v=True)
                removed += 1
        if removed:
            log.info("reaped orphaned sandbox containers", count=removed)
        return removed

    async def close(self) -> None:
        if self._client is not None:
            with suppress(Exception):
                await asyncio.to_thread(self._client.close)
            self._client = None


__all__ = [
    "MAX_OUTPUT_CHARS",
    "SANDBOX_LABEL",
    "SandboxResult",
    "SandboxRunner",
    "SandboxSpec",
]
