"""The tool contract.

Everything an agent can reach outside its own reasoning is a ``ToolSpec``, and
every answer it gets back is a ``ToolResult``. Four properties of this module
carry the weight:

* **Access class is structural.** ``access="write"`` means the call can change
  something outside Aegis. Such a tool is required, at registration time, to
  also demand an ``execution.ValidatedAction``, so a write tool that skips the
  gate chain cannot be registered, never mind invoked.
* **Untrusted output is typed, not documented.** A field carrying log lines,
  commit messages, alert bodies, file contents or PR text must be annotated
  ``UntrustedText``. ``validate_output_model`` refuses the registration
  otherwise, which is the only version of this rule that survives a busy week.
* **"Nothing found" and "could not look" are different answers.** An empty
  ``value`` with ``degraded=False`` is a finding. ``degraded=True`` carries a
  reason and an evidence gap. No caller can confuse the two by accident.
* **Nothing here grants authority.** A budget ledger can be charged but not
  raised, a caller identity can be read but not amended, and no field on a
  result feeds back into a permission decision.
"""

from __future__ import annotations

import types as pytypes
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Final, Literal, Protocol, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

from aegis.core.clock import SYSTEM_CLOCK, Clock
from aegis.core.config import Environment
from aegis.core.errors import AegisError, ValidationError
from aegis.domain.models import UntrustedText
from aegis.persistence.audit import AuditEvent

# --------------------------------------------------------------------------- #
# closed vocabularies                                                          #
# --------------------------------------------------------------------------- #

AccessClass = Literal["read", "write"]

# What a tool can change. ``environment`` is the only value that implies a write
# class, because it is the only one that reaches a system Aegis does not own.
MutationClass = Literal["nothing", "aegis_state", "sandbox", "environment"]

CostHint = Literal["cheap", "moderate", "expensive"]

ActorType = Literal["human", "agent", "system"]

# Environments a tool may be declared for. A context naming anything outside
# this set is denied rather than treated as a new environment (fail closed).
ENVIRONMENTS: Final[frozenset[str]] = frozenset(e.value for e in Environment)

# The complete permission vocabulary. A spec or a caller naming a scope outside
# this set is rejected: an unknown scope grants nothing and is never assumed to
# be a harmless new read.
SCOPES: Final[frozenset[str]] = frozenset(
    {
        "telemetry:metrics",
        "telemetry:traces",
        "telemetry:logs",
        "topology:read",
        "knowledge:search",
        "code:read",
        "memory:read",
        "runtime:read",
        "sandbox:run",
        "remediation:propose",
        "remediation:approval",
        "remediation:execute",
    }
)

# Audit event emitted for every write-class invocation. Aliased from the closed
# vocabulary in ``persistence.audit.AuditEvent`` rather than restated: two
# copies of an event-type string is how a compliance query silently stops
# matching half the rows it should. The name is kept for backward compatibility
# with callers that already import it from here.
TOOL_WRITE_EVENT: Final = AuditEvent.TOOL_WRITE_INVOKED

# Result bodies are summarised into ``tool_calls.result_summary``. The cap keeps
# one pathological tool from growing the incident database a row at a time.
MAX_RESULT_SUMMARY_CHARS: Final = 2_000
MAX_ARGUMENT_CHARS: Final = 8_000


# --------------------------------------------------------------------------- #
# errors                                                                       #
# --------------------------------------------------------------------------- #


class ToolError(BaseModel):
    """A failure as an agent sees it: a code, a sentence, and nothing else.

    A traceback is an implementation detail and, worse, an instruction-shaped
    blob of text arriving inside a model's context. Every exception raised
    anywhere under the tool boundary is converted into one of these.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    retryable: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_exception(cls, exc: BaseException, *, fallback_code: str) -> ToolError:
        if isinstance(exc, AegisError):
            return cls(
                code=exc.code,
                message=exc.message,
                retryable=exc.retryable,
                # Context is built by our own code, never by a remote payload.
                detail={
                    k: v
                    for k, v in exc.context.items()
                    if isinstance(v, str | int | float | bool)
                },
            )
        # Deliberately not ``str(exc)``: a third-party exception body can carry a
        # response fragment, and that fragment is attacker-influenceable text.
        return cls(code=fallback_code, message=type(exc).__name__, retryable=False)


class ToolContractError(ValidationError):
    """A tool was declared in a way the boundary cannot safely expose."""

    code = "TOOL_CONTRACT_INVALID"


# --------------------------------------------------------------------------- #
# input / output base models                                                   #
# --------------------------------------------------------------------------- #


class ToolInput(BaseModel):
    """Base for every tool argument model.

    ``strict`` is the point: arguments arrive from a language model, and
    coercing ``"5"`` into ``5`` or ``"true"`` into ``True`` hides the fact that
    the model did not produce what the schema asked for. Rejecting is
    information; coercing is a guess.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ToolOutput(BaseModel):
    """Base for every tool result body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def is_empty(self) -> bool:
        """True when the tool ran and found nothing. Never means 'unavailable'."""
        return False


def untrusted(text: str, *, origin: str, evidence_id: str | None = None) -> UntrustedText:
    """Wrap Tier-D content. The only sanctioned way to build one in this package."""
    return UntrustedText(text=text, origin=origin, evidence_id=evidence_id)


def untrusted_all(
    lines: Sequence[str], *, origin: str, limit: int = 500
) -> tuple[UntrustedText, ...]:
    """Wrap a bounded sequence of Tier-D lines."""
    return tuple(untrusted(line, origin=origin) for line in lines[:limit])


# Field names that carry text authored outside Aegis. Any output field with one
# of these names must be typed ``UntrustedText``; the registry enforces it. The
# list is deliberately a deny-list of *names* rather than a reviewer convention,
# because the failure it prevents - a log line reaching a prompt as a bare str -
# is invisible in review and catastrophic in production.
UNTRUSTED_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {
        "annotation",
        "annotations",
        "body",
        "commit_message",
        "line",
        "lines",
        "log",
        "logs",
        "message",
        "messages",
        "patch",
        "pr_body",
        "raw",
        "raw_text",
        "sample",
        "samples",
        "snippet",
        "snippets",
        "stderr",
        "stdout",
        "text",
    }
)


def _leaf_types(annotation: Any) -> list[Any]:
    """Flatten a type annotation into the concrete types it can hold."""
    origin = get_origin(annotation)
    if origin in (Union, pytypes.UnionType):
        out: list[Any] = []
        for arg in get_args(annotation):
            out.extend(_leaf_types(arg))
        return out
    if origin in (list, tuple, set, frozenset, Sequence):
        out = []
        for arg in get_args(annotation):
            if arg is Ellipsis:
                continue
            out.extend(_leaf_types(arg))
        return out
    if origin in (dict, Mapping):
        args = get_args(annotation)
        return _leaf_types(args[1]) if len(args) == 2 else []
    return [annotation]


def validate_output_model(model: type[BaseModel], *, _seen: set[type] | None = None) -> None:
    """Refuse an output model that would leak untrusted text as a bare string.

    Walks nested models too, because the dangerous field is usually two levels
    down - a ``LogLine`` inside a ``LogPage`` - which is exactly where a manual
    review stops looking.
    """
    # ``UntrustedText`` is the envelope itself, not a model to inspect: its own
    # ``text`` field is precisely the Tier-D payload the wrapper exists to hold.
    if model is UntrustedText:
        return
    seen = _seen if _seen is not None else set()
    if model in seen:
        return
    seen.add(model)

    for name, info in model.model_fields.items():
        leaves = _leaf_types(info.annotation)
        if name in UNTRUSTED_FIELD_NAMES:
            concrete = [t for t in leaves if t is not type(None)]
            # Either the envelope itself, or a nested model - which this
            # function then validates in turn, so the envelope is still reached.
            # What is refused is a bare ``str`` under one of these names.
            acceptable = concrete and all(
                t is UntrustedText or (isinstance(t, type) and issubclass(t, BaseModel))
                for t in concrete
            )
            if not acceptable:
                raise ToolContractError(
                    f"{model.__name__}.{name} carries untrusted content and must be "
                    "typed UntrustedText (or a model that wraps one)",
                    context={"model": model.__name__, "field": name},
                )
        for leaf in leaves:
            if isinstance(leaf, type) and issubclass(leaf, BaseModel):
                validate_output_model(leaf, _seen=seen)


# --------------------------------------------------------------------------- #
# caller identity and budget                                                   #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    """Who is invoking, and what they were granted before the call began.

    Scopes are handed down by whoever built the context - the orchestrator, the
    API, the MCP session. Nothing an agent says during an investigation can add
    one, because there is no mutator here to say it to.
    """

    subject: str
    actor_type: ActorType
    scopes: frozenset[str] = frozenset()

    def may(self, scope: str) -> bool:
        """Fail closed: a scope outside the closed vocabulary grants nothing."""
        return scope in SCOPES and scope in self.scopes


class BudgetView(Protocol):
    """The read-only budget surface. Deliberately has no widening method."""

    @property
    def tool_calls_remaining(self) -> int: ...

    @property
    def seconds_remaining(self) -> float: ...

    @property
    def exhausted(self) -> bool: ...


class BudgetLedger(Protocol):
    """What the invoker needs from a budget: look at it, and spend from it.

    Structural rather than a concrete import so ``agents.state.BudgetGuard``
    satisfies it without ``mcp`` depending on ``agents``.
    """

    def view(self) -> BudgetView: ...

    def charge_tool(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Remaining:
    tool_calls_remaining: int
    seconds_remaining: float

    @property
    def exhausted(self) -> bool:
        return self.tool_calls_remaining <= 0 or self.seconds_remaining <= 0


@dataclass
class ToolBudget:
    """A standalone ledger for callers outside the agent workflow.

    The MCP server serves external clients that have no ``BudgetGuard`` behind
    them; they still get a hard cap, because an unbounded external caller is
    just an unbounded agent wearing a different hat.
    """

    max_tool_calls: int
    max_seconds: float
    clock: Clock = field(default=SYSTEM_CLOCK)

    _calls: int = field(default=0, init=False)
    _started: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        if self.max_tool_calls < 1 or self.max_seconds <= 0:
            raise ValidationError("a tool budget must allow at least one bounded call")
        self._started = self.clock.monotonic()

    def view(self) -> BudgetView:
        elapsed = self.clock.monotonic() - self._started
        return _Remaining(
            tool_calls_remaining=max(0, self.max_tool_calls - self._calls),
            seconds_remaining=max(0.0, self.max_seconds - elapsed),
        )

    def charge_tool(self) -> None:
        self._calls += 1


# --------------------------------------------------------------------------- #
# context                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a call is bounded by, decided before the agent is consulted."""

    environment: str
    caller: CallerIdentity
    budget: BudgetLedger
    deadline: datetime
    correlation_id: str = ""
    incident_id: str | None = None
    agent_run_id: str | None = None

    def remaining_seconds(self, now: datetime) -> float:
        """Seconds left before the caller's deadline. Never negative."""
        return max(0.0, (self.deadline - now).total_seconds())

    @property
    def environment_known(self) -> bool:
        return self.environment in ENVIRONMENTS

    def budget_view(self) -> BudgetView:
        return self.budget.view()


# --------------------------------------------------------------------------- #
# specification                                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """The declaration of one tool. Immutable and validated at import time."""

    name: str
    description: str
    server: str
    input_model: type[ToolInput]
    output_model: type[ToolOutput]
    access: AccessClass
    mutates: MutationClass
    scope: str
    environments: frozenset[str]
    timeout_s: float
    retryable: bool
    idempotent: bool
    cost_hint: CostHint
    requires_validated_action: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ToolContractError(f"tool name {self.name!r} must be snake_case alphanumeric")
        if not self.description.strip():
            raise ToolContractError(f"tool {self.name} has no description")
        if self.scope not in SCOPES:
            raise ToolContractError(
                f"tool {self.name} declares unknown scope {self.scope!r}",
                context={"tool": self.name, "scope": self.scope},
            )
        unknown = self.environments - ENVIRONMENTS
        if unknown or not self.environments:
            raise ToolContractError(
                f"tool {self.name} declares unknown or empty environments",
                context={"tool": self.name, "unknown": sorted(unknown)},
            )
        if self.timeout_s <= 0 or self.timeout_s > 600:
            raise ToolContractError(
                f"tool {self.name} needs a timeout in (0, 600]s",
                context={"tool": self.name, "timeout_s": self.timeout_s},
            )
        # The structural rule the whole safety model rests on: reaching the
        # environment and demanding a ValidatedAction are the same condition.
        env_mutating = self.mutates == "environment"
        if env_mutating != (self.access == "write"):
            raise ToolContractError(
                f"tool {self.name}: access={self.access!r} disagrees with "
                f"mutates={self.mutates!r}; only environment mutation is a write",
                context={"tool": self.name},
            )
        if env_mutating != self.requires_validated_action:
            raise ToolContractError(
                f"tool {self.name}: a write tool must require a ValidatedAction "
                "and a non-write tool must not",
                context={"tool": self.name},
            )
        if self.access == "write" and self.retryable:
            raise ToolContractError(
                f"tool {self.name}: a write is never auto-retried",
                context={"tool": self.name},
            )
        if self.retryable and not self.idempotent:
            raise ToolContractError(
                f"tool {self.name}: only an idempotent tool may be retryable",
                context={"tool": self.name},
            )
        validate_output_model(self.output_model)

    @property
    def attempts(self) -> int:
        """Retry budget. Non-idempotent work gets exactly one attempt."""
        return 3 if (self.retryable and self.idempotent) else 1

    def json_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "server": self.server,
            "access": self.access,
            "mutates": self.mutates,
            "scope": self.scope,
            "environments": sorted(self.environments),
            "timeout_s": self.timeout_s,
            "retryable": self.retryable,
            "idempotent": self.idempotent,
            "cost_hint": self.cost_hint,
            "requires_validated_action": self.requires_validated_action,
        }


# --------------------------------------------------------------------------- #
# results                                                                      #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a tool handler returns.

    Handlers do not build a ``ToolResult``: ``ok``, timing and the call id are
    the invoker's to decide, so a tool cannot report success for a call that the
    boundary considers failed.
    """

    value: ToolOutput
    evidence_ids: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    degraded: bool = False
    degraded_reason: str = ""

    def __post_init__(self) -> None:
        if self.degraded and not self.degraded_reason:
            raise ToolContractError("a degraded outcome must carry a reason")


@dataclass(frozen=True, slots=True)
class ToolResult:
    """The complete answer to one invocation.

    ``ok`` says whether the call completed. ``degraded`` says whether the answer
    is partial because a source could not be consulted. They are independent on
    purpose: a successful call over a dead Loki is ``ok=True, degraded=True``,
    and reading that as "no errors in the logs" is the exact operational bug
    PRD 13 exists to prevent.
    """

    ok: bool
    tool: str
    call_id: str
    duration_ms: int
    value: ToolOutput | None = None
    error: ToolError | None = None
    evidence_ids: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    degraded: bool = False
    degraded_reason: str = ""

    def __post_init__(self) -> None:
        if self.ok == (self.error is not None):
            raise ToolContractError(
                "a ToolResult is either ok with a value or failed with an error"
            )

    @property
    def found_nothing(self) -> bool:
        """Ran, could look, and there was nothing there."""
        return self.ok and not self.degraded and (self.value is None or self.value.is_empty)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool": self.tool,
            "call_id": self.call_id,
            "duration_ms": self.duration_ms,
            "value": self.value.model_dump(mode="json") if self.value is not None else None,
            "error": self.error.model_dump(mode="json") if self.error is not None else None,
            "evidence_ids": list(self.evidence_ids),
            "provenance": list(self.provenance),
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
        }

    def summary(self) -> str:
        """Bounded, machine-written text for ``tool_calls.result_summary``."""
        if not self.ok and self.error is not None:
            body = f"{self.error.code}: {self.error.message}"
        elif self.value is None:
            body = "no value"
        else:
            body = self.value.model_dump_json()
        if self.degraded:
            body = f"[degraded: {self.degraded_reason}] {body}"
        return body[:MAX_RESULT_SUMMARY_CHARS]


class ToolResultEnvelope(BaseModel):
    """Serialisable form of a result, for the MCP wire and for the API."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    tool: str
    call_id: str
    duration_ms: int
    value: dict[str, Any] | None = None
    error: ToolError | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    provenance: list[str] = Field(default_factory=list)
    degraded: bool = False
    degraded_reason: str = ""

    ORIGIN: ClassVar[str] = "tool_result"

    @classmethod
    def of(cls, result: ToolResult) -> ToolResultEnvelope:
        return cls.model_validate(result.as_dict())


__all__ = [
    "ENVIRONMENTS",
    "MAX_ARGUMENT_CHARS",
    "MAX_RESULT_SUMMARY_CHARS",
    "SCOPES",
    "TOOL_WRITE_EVENT",
    "UNTRUSTED_FIELD_NAMES",
    "AccessClass",
    "ActorType",
    "BudgetLedger",
    "BudgetView",
    "CallerIdentity",
    "CostHint",
    "MutationClass",
    "ToolBudget",
    "ToolContext",
    "ToolContractError",
    "ToolError",
    "ToolInput",
    "ToolOutcome",
    "ToolOutput",
    "ToolResult",
    "ToolResultEnvelope",
    "ToolSpec",
    "untrusted",
    "untrusted_all",
    "validate_output_model",
]
