"""LangSmith wiring - agent tracing and the evaluation harness's datasets.

**LangSmith is observability, never a control-plane dependency** (ESD 34,
CLAUDE.md invariant 9). Every function in this module swallows its own failures
and returns a falsy value instead. There is no code path here that can raise
into a caller, because an incident workflow must survive LangSmith being down,
throttled, misconfigured or simply not installed, and it must survive it without
a single ``try`` at the call site.

The one exception to "swallow everything" is deliberate: ``trace_run`` re-raises
whatever the *traced body* raised. Tracing failures are absorbed; the work being
traced is not, or a crashed agent step would look like a successful one.

Nothing here decides anything. A missing trace degrades what an operator can see
afterwards, and it lowers nothing else.
"""

from __future__ import annotations

import functools
import os
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ParamSpec, TypeVar

from aegis.core.config import Settings
from aegis.core.logging import get_logger

log = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


@dataclass
class RunHandle:
    """What a traced block can write back to its span.

    Every mutator is a no-op when tracing is off, so instrumented code reads the
    same whether or not LangSmith is configured - there is no ``if traced:`` to
    forget.
    """

    run_id: str | None = None
    name: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        return self.run_id is not None

    def set_outputs(self, **values: Any) -> None:
        self.outputs.update(values)

    def add_metadata(self, **values: Any) -> None:
        self.metadata.update(values)


class LangSmithIntegration:
    """Tracing and dataset helpers. Fails silent, by design."""

    __slots__ = ("_settings", "_client", "_attempted", "_last_error")

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any | None = None
        self._attempted = False
        self._last_error = ""

    # ---- configuration ------------------------------------------------------

    @property
    def configured(self) -> bool:
        """Tracing is on only when it was asked for *and* a key exists."""
        return bool(
            self._settings.langsmith_tracing
            and self._settings.langsmith_api_key.get_secret_value()
        )

    @property
    def project(self) -> str:
        return self._settings.langsmith_project

    @property
    def last_error(self) -> str:
        return self._last_error

    def enable(self) -> bool:
        """Publish LangChain's environment variables and build the client.

        LangChain reads these at call time, so setting them is what makes a
        LangGraph run appear in the project. They are only ever set when this
        integration is configured - enabling tracing by accident would ship
        prompt content to a third party.
        """
        if not self.configured:
            log.info("langsmith tracing disabled", reason="not configured")
            return False
        try:
            os.environ["LANGCHAIN_TRACING_V2"] = "true"
            os.environ["LANGSMITH_TRACING"] = "true"
            os.environ["LANGCHAIN_ENDPOINT"] = self._settings.langsmith_endpoint
            os.environ["LANGSMITH_ENDPOINT"] = self._settings.langsmith_endpoint
            os.environ["LANGCHAIN_PROJECT"] = self.project
            os.environ["LANGSMITH_PROJECT"] = self.project
            key = self._settings.langsmith_api_key.get_secret_value()
            os.environ["LANGCHAIN_API_KEY"] = key
            os.environ["LANGSMITH_API_KEY"] = key
        except Exception as exc:  # noqa: BLE001 - observability is never fatal
            self._last_error = str(exc)
            log.warning("langsmith environment setup failed", error=str(exc))
            return False
        return self._ensure_client() is not None

    def _ensure_client(self) -> Any | None:
        """Build the client once. A failure is remembered, not retried per call."""
        if self._client is not None or self._attempted:
            return self._client
        self._attempted = True
        if not self.configured:
            return None
        try:
            from langsmith import Client

            self._client = Client(
                api_url=self._settings.langsmith_endpoint,
                api_key=self._settings.langsmith_api_key.get_secret_value(),
            )
            log.info("langsmith client ready", project=self.project)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            log.warning("langsmith unavailable", error=str(exc))
            self._client = None
        return self._client

    def health(self) -> tuple[bool | None, str]:
        """(healthy, reason). ``None`` means unconfigured, never unhealthy."""
        if not self.configured:
            return None, "langsmith tracing is disabled or has no api key"
        client = self._ensure_client()
        if client is None:
            return False, self._last_error or "client could not be created"
        try:
            client.list_projects(limit=1)
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            return False, str(exc)[:200]
        return True, ""

    # ---- tracing ------------------------------------------------------------

    @contextmanager
    def trace_run(
        self,
        name: str,
        *,
        run_type: str = "chain",
        inputs: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        tags: Sequence[str] | None = None,
    ) -> Iterator[RunHandle]:
        """Trace a block of work.

        Failures inside LangSmith are absorbed and the block still runs.
        Failures inside the block propagate - they are recorded on the run first,
        then re-raised, so a crashed step is visible in both places.
        """
        handle = RunHandle(name=name)
        handle.metadata.update(dict(metadata or {}))
        client = self._ensure_client()
        started = datetime.now(UTC)

        if client is not None:
            run_id = str(uuid.uuid4())
            try:
                client.create_run(
                    id=run_id,
                    name=name,
                    run_type=run_type,
                    inputs=dict(inputs or {}),
                    project_name=self.project,
                    start_time=started,
                    extra={"metadata": dict(handle.metadata)},
                    tags=list(tags or []),
                )
                handle.run_id = run_id
            except Exception as exc:  # noqa: BLE001 - tracing never blocks work
                log.warning("langsmith run not started", run=name, error=str(exc))

        try:
            yield handle
        except Exception as exc:
            self._close_run(handle, error=f"{type(exc).__name__}: {exc}")
            raise
        self._close_run(handle, error=None)

    def _close_run(self, handle: RunHandle, *, error: str | None) -> None:
        client = self._client
        if client is None or handle.run_id is None:
            return
        try:
            client.update_run(
                handle.run_id,
                outputs=dict(handle.outputs),
                error=error,
                end_time=datetime.now(UTC),
                extra={"metadata": dict(handle.metadata)},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("langsmith run not closed", run=handle.name, error=str(exc))

    def traced(
        self, name: str | None = None, *, run_type: str = "chain"
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        """Decorator form of ``trace_run`` for sync and async callables."""

        def decorate(fn: Callable[P, T]) -> Callable[P, T]:
            label: str = name or str(getattr(fn, "__qualname__", "run"))

            if _is_coroutine_function(fn):

                @functools.wraps(fn)
                async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> Any:
                    with self.trace_run(label, run_type=run_type) as handle:
                        result = await fn(*args, **kwargs)  # type: ignore[misc]
                        handle.set_outputs(returned=type(result).__name__)
                        return result

                return async_wrapper  # type: ignore[return-value]

            @functools.wraps(fn)
            def sync_wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                with self.trace_run(label, run_type=run_type) as handle:
                    result = fn(*args, **kwargs)
                    handle.set_outputs(returned=type(result).__name__)
                    return result

            return sync_wrapper

        return decorate

    # ---- evaluation harness helpers ----------------------------------------

    def ensure_dataset(self, name: str, description: str = "") -> str | None:
        """Return the dataset id, creating it if absent. ``None`` when unusable.

        The benchmark harness treats ``None`` as "record results locally only".
        Ground truth lives in ``eval/scenarios``; LangSmith is where results are
        inspected, never where they are defined.
        """
        client = self._ensure_client()
        if client is None:
            return None
        try:
            existing = list(client.list_datasets(dataset_name=name, limit=1))
            if existing:
                return str(existing[0].id)
            return str(client.create_dataset(dataset_name=name, description=description).id)
        except Exception as exc:  # noqa: BLE001
            log.warning("langsmith dataset unavailable", dataset=name, error=str(exc))
            return None

    def add_examples(
        self, dataset_name: str, examples: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]]
    ) -> int:
        """Upload (inputs, outputs) pairs. Returns how many landed."""
        dataset_id = self.ensure_dataset(dataset_name)
        client = self._client
        if client is None or dataset_id is None or not examples:
            return 0
        try:
            client.create_examples(
                inputs=[dict(inputs) for inputs, _ in examples],
                outputs=[dict(outputs) for _, outputs in examples],
                dataset_id=dataset_id,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("langsmith examples not uploaded", dataset=dataset_name, error=str(exc))
            return 0
        return len(examples)

    def record_experiment(
        self,
        experiment: str,
        *,
        dataset_name: str,
        metrics: Mapping[str, float],
        metadata: Mapping[str, Any] | None = None,
    ) -> str | None:
        """Record one benchmark run's headline metrics as a run in its own project.

        The authoritative record of an evaluation is the ``eval_runs`` table.
        This is the copy an engineer browses, which is why losing it is a
        warning and nothing more.
        """
        client = self._ensure_client()
        if client is None:
            return None
        run_id = str(uuid.uuid4())
        now = datetime.now(UTC)
        try:
            client.create_run(
                id=run_id,
                name=experiment,
                run_type="chain",
                inputs={"dataset": dataset_name},
                outputs=dict(metrics),
                project_name=f"{self.project}-experiments",
                start_time=now,
                end_time=now,
                extra={"metadata": dict(metadata or {})},
                tags=["experiment", dataset_name],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("langsmith experiment not recorded", experiment=experiment, error=str(exc))
            return None
        return run_id

    def record_feedback(
        self, run_id: str, key: str, score: float, comment: str = ""
    ) -> bool:
        """Attach an evaluator's score to a run. False when it did not land."""
        client = self._ensure_client()
        if client is None:
            return False
        try:
            client.create_feedback(run_id, key=key, score=score, comment=comment or None)
        except Exception as exc:  # noqa: BLE001
            log.warning("langsmith feedback not recorded", run_id=run_id, error=str(exc))
            return False
        return True


def _is_coroutine_function(fn: Any) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


__all__ = ["LangSmithIntegration", "RunHandle"]
