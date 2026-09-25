"""Sandbox tools: run code, never in production.

Classification, stated plainly: these tools execute code, so they are declared
``mutates="sandbox"``, but they are ``access="read"`` because the thing they
change is a disposable container with no network, not the observed environment.
The write class exists to force the gate chain in front of environment
mutation; putting a reproduction run behind it would demand a
``ValidatedAction`` for work whose entire purpose is to gather the evidence that
justifies one.

What keeps that classification honest is the boundary below: ``SandboxRunner``
runs with no network, bounded CPU, memory and wall clock, a refused credential
env allowlist, and a reaper for orphans. Nothing here can reach a production
endpoint even if the code it runs tries.

There is no arbitrary shell. A caller picks a suite from a closed set of command
templates and may name one repo-relative target, validated by pattern. A free
``command`` parameter would be a remote-code-execution surface wearing a tool
schema, and the sandbox limits would be the only thing left between a model's
output and the host.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field

from aegis.core.errors import AegisError, ValidationError
from aegis.core.logging import get_logger
from aegis.domain.enums import EvidenceType, SourceType
from aegis.domain.models import UntrustedText
from aegis.execution.sandbox import MAX_PATCH_BYTES, Purpose, SandboxSpec
from aegis.mcp.deps import ToolDeps
from aegis.mcp.registry import ToolRegistry
from aegis.mcp.tools import support
from aegis.mcp.types import (
    ENVIRONMENTS,
    ToolContext,
    ToolInput,
    ToolOutcome,
    ToolOutput,
    ToolSpec,
    untrusted,
)

log = get_logger(__name__)

# The complete set of commands a tool may cause to run. Each entry is argv, not
# a shell string: there is no interpreter in between to expand a metacharacter.
SUITES: dict[str, tuple[str, ...]] = {
    "pytest": ("python", "-m", "pytest", "-q"),
    "unittest": ("python", "-m", "unittest", "-v"),
    "npm_test": ("npm", "test", "--silent"),
    "go_test": ("go", "test", "./..."),
    "make_test": ("make", "test"),
}

SuiteName = Literal["pytest", "unittest", "npm_test", "go_test", "make_test"]

# A repo-relative target: no absolute paths, no parent traversal, no shell
# metacharacters, no whitespace.
_TARGET_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}$")

MAX_OUTPUT_LINES = 400


def build_command(suite: SuiteName, target: str | None) -> tuple[str, ...]:
    """Render one argv from the closed template set."""
    argv = SUITES[suite]
    if not target:
        return argv
    if ".." in target or not _TARGET_RE.match(target):
        raise ValidationError(
            "sandbox target must be a repo-relative path",
            context={"target": target[:120]},
        )
    return (*argv, target)


# --------------------------------------------------------------------------- #
# models                                                                       #
# --------------------------------------------------------------------------- #


class SandboxRunInput(ToolInput):
    repo_url: str = Field(min_length=4, max_length=400)
    base_ref: str = Field(min_length=1, max_length=200)
    suite: SuiteName = "pytest"
    target: str | None = Field(default=None, max_length=200)
    timeout_s: int | None = Field(default=None, ge=10, le=900)


class PatchRunInput(SandboxRunInput):
    # A unified diff, bounded by the same limit the sandbox enforces. It is
    # applied inside the container and nowhere else.
    patch: str = Field(min_length=1, max_length=MAX_PATCH_BYTES)


class SandboxResultOutput(ToolOutput):
    sandbox_id: str
    purpose: str
    image: str
    command: str
    exit_code: int | None = None
    succeeded: bool = False
    timed_out: bool = False
    killed: bool = False
    duration_ms: int = 0
    network: str = "none"
    repo: str | None = None
    base_ref: str | None = None
    patch_sha256: str | None = None
    artifact_count: int = 0
    # Program output is free text written by whatever ran. Tier D.
    stdout: UntrustedText | None = None
    stderr: UntrustedText | None = None

    @property
    def is_empty(self) -> bool:
        """A sandbox run always produces a result; there is no empty case."""
        return False


# --------------------------------------------------------------------------- #
# registration                                                                 #
# --------------------------------------------------------------------------- #


def register(registry: ToolRegistry, deps: ToolDeps) -> None:
    """Declare the sandbox tools against an injected dependency set."""

    def _empty(purpose: Purpose, command: tuple[str, ...]) -> SandboxResultOutput:
        return SandboxResultOutput(
            sandbox_id="", purpose=purpose, image=deps.sandbox_image,
            command=" ".join(command),
        )

    async def _run(
        context: ToolContext,
        *,
        purpose: Purpose,
        evidence_type: EvidenceType,
        args: SandboxRunInput,
        patch: str | None = None,
    ) -> ToolOutcome:
        command = build_command(args.suite, args.target)
        if deps.sandbox is None:
            return await support.degraded(
                deps, context, source="sandbox", source_type=SourceType.SANDBOX,
                reason="no sandbox runner is configured",
                value=_empty(purpose, command),
            )
        if not deps.sandbox.enabled:
            return await support.degraded(
                deps, context, source="sandbox", source_type=SourceType.SANDBOX,
                reason="sandboxing is disabled by configuration",
                value=_empty(purpose, command),
            )

        spec = SandboxSpec(
            purpose=purpose,
            command=list(command),
            repo_url=args.repo_url,
            base_ref=args.base_ref,
            patch=patch,
            timeout_s=args.timeout_s,
            incident_id=context.incident_id,
        )
        try:
            result = await deps.sandbox.run(spec)
        except AegisError as exc:
            # An unusable sandbox - no daemon, missing image - is a source that
            # could not be consulted, not a failing test.
            return await support.degraded(
                deps, context, source="sandbox", source_type=SourceType.SANDBOX,
                reason=exc.message, value=_empty(purpose, command),
            )

        uri = f"sandbox://{result.id}?purpose={result.purpose}&ref={args.base_ref}"
        value = SandboxResultOutput(
            sandbox_id=result.id,
            purpose=result.purpose,
            image=result.image,
            command=result.command,
            exit_code=result.exit_code,
            succeeded=result.succeeded,
            timed_out=result.timed_out,
            killed=result.killed,
            duration_ms=result.duration_ms,
            network=result.network,
            repo=result.repo,
            base_ref=result.base_ref,
            patch_sha256=result.patch_sha256,
            artifact_count=len(result.artifacts),
            stdout=untrusted(_tail(result.stdout), origin="sandbox_stdout"),
            stderr=untrusted(_tail(result.stderr), origin="sandbox_stderr"),
        )
        # The exit code is a direct machine observation and is recorded as
        # structure. The output text is not recorded as trusted content: it goes
        # back to the caller wrapped instead.
        ids = await support.record_evidence(
            deps, context, source="sandbox", source_type=SourceType.SANDBOX,
            evidence_type=evidence_type,
            summary=(
                f"{purpose} {result.command}: exit={result.exit_code} "
                f"timed_out={result.timed_out} in {result.duration_ms}ms"
            ),
            structured_value={
                "sandbox_id": result.id,
                "purpose": result.purpose,
                "exit_code": result.exit_code,
                "succeeded": result.succeeded,
                "timed_out": result.timed_out,
                "killed": result.killed,
                "duration_ms": result.duration_ms,
                "repo": result.repo,
                "base_ref": result.base_ref,
                "patch_sha256": result.patch_sha256,
            },
            provenance_uri=uri,
            observed_at=result.finished_at,
        )
        if result.timed_out or result.killed:
            # The run did not complete, so its exit code decides nothing. That is
            # an incomplete measurement, not a failing test.
            return ToolOutcome(
                value=value, evidence_ids=ids, provenance=(uri,),
                degraded=True,
                degraded_reason="sandbox run was killed or timed out before finishing",
            )
        return ToolOutcome(value=value, evidence_ids=ids, provenance=(uri,))

    async def run_reproduction(context: ToolContext, args: SandboxRunInput) -> ToolOutcome:
        return await _run(
            context, purpose="reproduce",
            evidence_type=EvidenceType.REPRODUCTION, args=args,
        )

    async def test_patch(context: ToolContext, args: PatchRunInput) -> ToolOutcome:
        return await _run(
            context, purpose="test_patch",
            evidence_type=EvidenceType.TEST_RESULT, args=args, patch=args.patch,
        )

    async def run_regression_suite(
        context: ToolContext, args: SandboxRunInput
    ) -> ToolOutcome:
        return await _run(
            context, purpose="regression",
            evidence_type=EvidenceType.TEST_RESULT, args=args,
        )

    def _spec(
        name: str,
        description: str,
        input_model: type[ToolInput],
        *,
        timeout_s: float,
    ) -> ToolSpec:
        return ToolSpec(
            name=name,
            description=description,
            server="sandbox",
            input_model=input_model,
            output_model=SandboxResultOutput,
            access="read",
            mutates="sandbox",
            scope="sandbox:run",
            environments=ENVIRONMENTS,
            timeout_s=timeout_s,
            # A sandbox run consumes real CPU and minutes. Replaying one is never
            # free and its result is never guaranteed identical, so it is neither
            # idempotent nor auto-retried.
            retryable=False,
            idempotent=False,
            cost_hint="expensive",
        )

    registry.register(
        _spec(
            "run_reproduction",
            "Run a test suite at a ref in an isolated, networkless sandbox to "
            "reproduce a failure.",
            SandboxRunInput,
            timeout_s=300.0,
        ),
        run_reproduction,
    )
    registry.register(
        _spec(
            "test_patch",
            "Apply a unified diff inside the sandbox and run a suite against it. "
            "The patch never leaves the container.",
            PatchRunInput,
            timeout_s=420.0,
        ),
        test_patch,
    )
    registry.register(
        _spec(
            "run_regression_suite",
            "Run the full suite at a ref to check a fix introduced nothing new.",
            SandboxRunInput,
            timeout_s=600.0,
        ),
        run_regression_suite,
    )


def _tail(text: str, lines: int = MAX_OUTPUT_LINES) -> str:
    """Keep the end of the output - the failure is at the bottom, not the top."""
    split = text.splitlines()
    if len(split) <= lines:
        return text
    dropped = len(split) - lines
    return f"...[{dropped} earlier lines omitted]\n" + "\n".join(split[-lines:])


__all__ = ["SUITES", "build_command", "register"]
