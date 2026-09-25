"""Typed self-edit tools: the only way the brain changes its working memory.

Each tool is validated by code before anything changes. A rejected edit leaves
the state exactly as it was and produces a ``self_edit_rejected`` event, so a
model that cites an evidence id it invented learns nothing it can exploit and
the operator sees the attempt.

Schemas are strict-mode compatible: every object has ``additionalProperties:
false`` and lists every property as required, with optionality expressed as a
nullable type (the rule Anthropic strict tool use and Gemini both accept).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Final

from aegis.agents.horizon.phases import recompute_confidences
from aegis.agents.horizon.ports import HorizonStore, ToolSpec
from aegis.domain.enums import ActionType
from aegis.domain.horizon import (
    GOAL_TRANSITIONS,
    MAX_HYPOTHESES,
    MAX_NOTE_CHARS,
    MAX_NOTES,
    EvidenceCard,
    GoalStatus,
    HorizonEventType,
    HorizonHypothesis,
    HorizonState,
)

UPDATE_HYPOTHESES: Final = "update_hypotheses"
RECORD_EVIDENCE: Final = "record_evidence"
DISCARD_EVIDENCE: Final = "discard_evidence"
SET_GOAL_STATUS: Final = "set_goal_status"
WRITE_NOTE: Final = "write_note"
RECALL_EVIDENCE: Final = "recall_evidence"

SELF_EDIT_TOOLS: Final = frozenset(
    {
        UPDATE_HYPOTHESES,
        RECORD_EVIDENCE,
        DISCARD_EVIDENCE,
        SET_GOAL_STATUS,
        WRITE_NOTE,
        RECALL_EVIDENCE,
    }
)

_ID_RE: Final = re.compile(r"^[A-Za-z0-9_:.\-]{1,64}$")
_STR_IDS: Final[dict[str, Any]] = {"type": "array", "items": {"type": "string"}, "maxItems": 12}
_EVIDENCE_ID_ONLY: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {"evidence_id": {"type": "string"}},
    "required": ["evidence_id"],
    "additionalProperties": False,
}

SPECS: Final[dict[str, ToolSpec]] = {
    UPDATE_HYPOTHESES: ToolSpec(
        name=UPDATE_HYPOTHESES,
        description=(
            "Add or revise hypotheses. Each cites supporting and refuting evidence ids "
            "from [EVIDENCE]. Confidence is derived from the evidence, not accepted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    "maxItems": MAX_HYPOTHESES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": ["string", "null"]},
                            "statement": {"type": "string"},
                            "supporting": _STR_IDS,
                            "refuting": _STR_IDS,
                            "suggested_action": {"type": ["string", "null"]},
                        },
                        "required": [
                            "id",
                            "statement",
                            "supporting",
                            "refuting",
                            "suggested_action",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["hypotheses"],
            "additionalProperties": False,
        },
    ),
    RECORD_EVIDENCE: ToolSpec(
        name=RECORD_EVIDENCE,
        description="Pin an existing observation so it stays in context.",
        input_schema=_EVIDENCE_ID_ONLY,
    ),
    DISCARD_EVIDENCE: ToolSpec(
        name=DISCARD_EVIDENCE,
        description="Drop a card from context. The raw observation stays stored.",
        input_schema=_EVIDENCE_ID_ONLY,
    ),
    SET_GOAL_STATUS: ToolSpec(
        name=SET_GOAL_STATUS,
        description="Move a goal between pending/active/done/failed/skipped (legal moves only).",
        input_schema={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string"},
                "status": {"type": "string", "enum": [s.value for s in GoalStatus]},
            },
            "required": ["goal_id", "status"],
            "additionalProperties": False,
        },
    ),
    WRITE_NOTE: ToolSpec(
        name=WRITE_NOTE,
        description=f"Scratchpad note, at most {MAX_NOTE_CHARS} characters.",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": MAX_NOTE_CHARS}},
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    RECALL_EVIDENCE: ToolSpec(
        name=RECALL_EVIDENCE,
        description="Bring a discarded card back into context by its evidence id.",
        input_schema=_EVIDENCE_ID_ONLY,
    ),
}


@dataclass(slots=True)
class EditResult:
    """What one self-edit did. ``ok=False`` means the state was not touched."""

    ok: bool
    event_type: HorizonEventType
    message: str
    payload: dict[str, Any] = field(default_factory=dict)


def _reject(tool: str, why: str, **payload: Any) -> EditResult:
    return EditResult(
        ok=False,
        event_type=HorizonEventType.SELF_EDIT_REJECTED,
        message=f"{tool} rejected: {why}",
        payload={"tool": tool, "reason": why, **payload},
    )


def _str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return value if isinstance(value, str) else None


class SelfEditor:
    """Validates and applies self-edits. ``known_cards`` spans context and storage."""

    __slots__ = ("_store",)

    def __init__(self, store: HorizonStore) -> None:
        self._store = store

    async def apply(
        self,
        name: str,
        args: dict[str, Any],
        state: HorizonState,
        known_cards: dict[str, EvidenceCard],
    ) -> EditResult:
        if name == UPDATE_HYPOTHESES:
            return self._update_hypotheses(args, state, known_cards)
        if name == RECORD_EVIDENCE:
            return await self._record(args, state)
        if name == DISCARD_EVIDENCE:
            return self._discard(args, state)
        if name == SET_GOAL_STATUS:
            return self._goal(args, state)
        if name == WRITE_NOTE:
            return self._note(args, state)
        if name == RECALL_EVIDENCE:
            return await self._recall(args, state)
        return _reject(name, "not a self-edit tool")

    # ---- update_hypotheses ------------------------------------------------ #

    def _update_hypotheses(
        self, args: dict[str, Any], state: HorizonState, known: dict[str, EvidenceCard]
    ) -> EditResult:
        items = args.get("hypotheses")
        if not isinstance(items, list) or not items:
            return _reject(UPDATE_HYPOTHESES, "hypotheses must be a non-empty list")
        if len(items) > MAX_HYPOTHESES:
            return _reject(UPDATE_HYPOTHESES, f"at most {MAX_HYPOTHESES} hypotheses")

        # Validate everything before changing anything: a partial apply would
        # leave the state half-edited by a call that was rejected.
        parsed: list[tuple[str | None, str, list[str], list[str], str | None]] = []
        for raw in items:
            if not isinstance(raw, dict):
                return _reject(UPDATE_HYPOTHESES, "each hypothesis must be an object")
            statement = (_str(raw, "statement") or "").strip()
            if not statement or len(statement) > 240:
                return _reject(UPDATE_HYPOTHESES, "statement must be 1-240 characters")
            hid = _str(raw, "id")
            if hid is not None and not _ID_RE.match(hid):
                return _reject(UPDATE_HYPOTHESES, "hypothesis id is malformed")
            supporting = raw.get("supporting") or []
            refuting = raw.get("refuting") or []
            if not isinstance(supporting, list) or not isinstance(refuting, list):
                return _reject(UPDATE_HYPOTHESES, "supporting/refuting must be lists")
            cited = [*supporting, *refuting]
            if not all(isinstance(i, str) for i in cited):
                return _reject(UPDATE_HYPOTHESES, "evidence ids must be strings")
            unknown = sorted({i for i in cited if i not in known})
            if unknown:
                # The citation validator's rule: an unresolvable citation is
                # rejected, never annotated.
                return _reject(
                    UPDATE_HYPOTHESES, "cited evidence does not exist", unknown=unknown[:8]
                )
            action = _str(raw, "suggested_action")
            if action is not None and action not in {a.value for a in ActionType}:
                return _reject(UPDATE_HYPOTHESES, f"unknown action type {action[:40]!r}")
            # ``confidence`` from the model, if it sent one, is dropped here.
            parsed.append(
                (
                    hid,
                    statement,
                    list(dict.fromkeys(supporting))[:12],
                    list(dict.fromkeys(refuting))[:12],
                    action,
                )
            )

        by_id = {h.id: h for h in state.hypotheses}
        new_count = sum(1 for p in parsed if p[0] is None or p[0] not in by_id)
        if len(state.hypotheses) + new_count > MAX_HYPOTHESES:
            return _reject(UPDATE_HYPOTHESES, f"would exceed {MAX_HYPOTHESES} hypotheses")

        next_n = len(state.hypotheses) + 1
        changed: list[str] = []
        for hid, statement, supporting, refuting, action in parsed:
            if hid is not None and hid in by_id:
                hyp = by_id[hid]
                hyp.statement = statement
                hyp.supporting = supporting
                hyp.refuting = refuting
                hyp.suggested_action = action
            else:
                while f"h{next_n}" in by_id:
                    next_n += 1
                new_id = hid or f"h{next_n}"
                next_n += 1
                hyp = HorizonHypothesis(
                    id=new_id,
                    statement=statement,
                    supporting=supporting,
                    refuting=refuting,
                    suggested_action=action,
                )
                state.hypotheses = [*state.hypotheses, hyp]
                by_id[new_id] = hyp
            changed.append(hyp.id)
        recompute_confidences(state, list(known.values()))
        return EditResult(
            ok=True,
            event_type=HorizonEventType.HYPOTHESES_UPDATED,
            message=f"{len(changed)} hypotheses updated",
            payload={
                "hypotheses": [h.model_dump(mode="json") for h in state.hypotheses],
                "changed": changed,
            },
        )

    # ---- evidence curation ------------------------------------------------ #

    async def _record(self, args: dict[str, Any], state: HorizonState) -> EditResult:
        eid = _str(args, "evidence_id") or ""
        card = state.card(eid)
        if card is None:
            obs = await self._store.get_observation(eid)
            if obs is None or obs.card is None:
                return _reject(
                    RECORD_EVIDENCE, "no stored observation has that id", evidence_id=eid
                )
            card = obs.card
            state.evidence = [*state.evidence, card]
            state.discarded = [d for d in state.discarded if d != eid]
        pinned = card.model_copy(update={"pinned": True})
        state.evidence = [pinned if c.id == eid else c for c in state.evidence]
        return EditResult(
            ok=True,
            event_type=HorizonEventType.EVIDENCE_ADDED,
            message=f"{eid} pinned",
            payload={"card": pinned.model_dump(mode="json"), "pinned": True},
        )

    def _discard(self, args: dict[str, Any], state: HorizonState) -> EditResult:
        eid = _str(args, "evidence_id") or ""
        if state.card(eid) is None:
            return _reject(DISCARD_EVIDENCE, "evidence id is not in context", evidence_id=eid)
        state.evidence = [c for c in state.evidence if c.id != eid]
        if eid not in state.discarded:
            state.discarded = [*state.discarded, eid]
        return EditResult(
            ok=True,
            event_type=HorizonEventType.EVIDENCE_DISCARDED,
            message=f"{eid} discarded from context (raw kept)",
            payload={"evidence_id": eid, "by": "brain"},
        )

    async def _recall(self, args: dict[str, Any], state: HorizonState) -> EditResult:
        eid = _str(args, "evidence_id") or ""
        if state.card(eid) is not None:
            return _reject(RECALL_EVIDENCE, "evidence is already in context", evidence_id=eid)
        obs = await self._store.get_observation(eid)
        if obs is None or obs.card is None:
            return _reject(RECALL_EVIDENCE, "no stored observation has that id", evidence_id=eid)
        # Pinned on recall, or the ranking would evict it again at once: a
        # recalled card is old by definition.
        recalled = obs.card.model_copy(update={"pinned": True})
        state.evidence = [*state.evidence, recalled]
        state.discarded = [d for d in state.discarded if d != eid]
        return EditResult(
            ok=True,
            event_type=HorizonEventType.EVIDENCE_RECALLED,
            message=f"{eid} recalled into context",
            payload={"card": recalled.model_dump(mode="json")},
        )

    # ---- goals and notes --------------------------------------------------- #

    def _goal(self, args: dict[str, Any], state: HorizonState) -> EditResult:
        gid = _str(args, "goal_id") or ""
        raw = _str(args, "status") or ""
        try:
            target = GoalStatus(raw)
        except ValueError:
            return _reject(SET_GOAL_STATUS, f"unknown status {raw[:20]!r}")
        goal = next((g for g in state.goals if g.id == gid), None)
        if goal is None:
            return _reject(SET_GOAL_STATUS, "unknown goal id", goal_id=gid)
        if target is goal.status:
            return EditResult(
                ok=True,
                event_type=HorizonEventType.GOAL_UPDATED,
                message="no change",
                payload={"goal": goal.model_dump(mode="json")},
            )
        if target not in GOAL_TRANSITIONS[goal.status]:
            return _reject(
                SET_GOAL_STATUS,
                f"{goal.status.value} -> {target.value} is not a legal goal move",
                goal_id=gid,
            )
        goal.status = target
        return EditResult(
            ok=True,
            event_type=HorizonEventType.GOAL_UPDATED,
            message=f"goal {gid} -> {target.value}",
            payload={"goal": goal.model_dump(mode="json")},
        )

    def _note(self, args: dict[str, Any], state: HorizonState) -> EditResult:
        text = (_str(args, "text") or "").strip()
        if not text:
            return _reject(WRITE_NOTE, "note is empty")
        if len(text) > MAX_NOTE_CHARS:
            return _reject(WRITE_NOTE, f"note exceeds {MAX_NOTE_CHARS} characters")
        # Stored as data: envelope tags are neutralised so a note copied from a
        # log line cannot close the untrusted envelope it is rendered inside.
        safe = text.replace("<untrusted", "<_untrusted").replace("</untrusted>", "<_/untrusted>")
        state.notes = [*state.notes, safe][-MAX_NOTES:]
        return EditResult(
            ok=True,
            event_type=HorizonEventType.NOTE_WRITTEN,
            message="note written",
            payload={"text": safe, "untrusted": True},
        )


__all__ = [
    "DISCARD_EVIDENCE",
    "RECALL_EVIDENCE",
    "RECORD_EVIDENCE",
    "SELF_EDIT_TOOLS",
    "SET_GOAL_STATUS",
    "SPECS",
    "UPDATE_HYPOTHESES",
    "WRITE_NOTE",
    "EditResult",
    "SelfEditor",
]
