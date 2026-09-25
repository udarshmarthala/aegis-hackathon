"""The deterministic phase guard.

Code owns control flow; the model owns judgement. Everything in this module is
a pure function of ``HorizonState`` - no I/O, no model - so every rule below is
unit-testable exhaustively and replayable from a checkpoint. The brain can add
hypotheses and cite evidence; it cannot move a phase, set a confidence, or
re-run an action that already failed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aegis.domain.horizon import (
    MAX_REMEDIATION_CYCLES,
    ActionAttempt,
    EvidenceCard,
    HorizonHypothesis,
    HorizonPhase,
    HorizonState,
    assert_phase_transition,
)

GUARD_VERSION: Final = "1.0.0"

MIN_DISTINCT_OBSERVE_TOOLS: Final = 3
MIN_HYPOTHESES_TO_PLAN: Final = 2
MIN_CONFIDENCE_TO_PLAN: Final = 0.6

# The tool whose card records a sustained-window verification. Named here so
# the guard and the orchestrator agree on which card a reassessment must cite.
VERIFY_TOOL: Final = "verify_recovery"
FAILED_PREFIX: Final = "FAILED"
GAP_PREFIX: Final = "UNAVAILABLE"

# --------------------------------------------------------------------------- #
# derived confidence                                                           #
# --------------------------------------------------------------------------- #

# Versioned for the same reason ``evidence.confidence`` is: a benchmark result
# must be attributable to a specific confidence model. Same shape as that
# module - corroboration by distinct origin, reliability, a contradiction
# penalty and a gap penalty - adapted to compacted cards, which carry a
# code-assigned weight instead of tested predictions.
HORIZON_CONFIDENCE_VERSION: Final = "horizon-1.0.0"
W_STRENGTH: Final = 0.45
W_CORROBORATION: Final = 0.25
W_RELIABILITY: Final = 0.30
W_CONTRADICTION_PENALTY: Final = 0.50
W_GAP_PENALTY: Final = 0.10
HISTORY_LEN: Final = 16


def is_gap(card: EvidenceCard) -> bool:
    """A card recording that a source could not be consulted."""
    return card.claim.startswith(GAP_PREFIX)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def derive_confidence(hypothesis: HorizonHypothesis, cards: Sequence[EvidenceCard]) -> float:
    """Confidence from the cited evidence only. The model's number is ignored.

    Strength is the noisy-OR of the supporting weights (independent weak
    signals add up, but never past certainty); corroboration counts distinct
    origins rather than items, because three reads of one Prometheus agree by
    construction; a refutation costs more than a support earns.
    """
    by_id = {c.id: c for c in cards}
    cited = [by_id[i] for i in hypothesis.supporting if i in by_id]
    gaps = [c for c in cited if is_gap(c)]
    supporting = [c for c in cited if not is_gap(c)]
    refuting = [by_id[i] for i in hypothesis.refuting if i in by_id and not is_gap(by_id[i])]
    if not supporting:
        return 0.0

    miss = 1.0
    for card in supporting:
        miss *= 1.0 - card.weight
    strength = 1.0 - miss
    corroboration = min(len({c.origin for c in supporting}) / 3.0, 1.0)
    reliability = sum(c.weight for c in supporting) / len(supporting)
    total = len(supporting) + len(refuting)
    contradiction = len(refuting) / total if total else 0.0
    gap_ratio = len(gaps) / (len(cited) or 1)

    raw = (
        W_STRENGTH * strength
        + W_CORROBORATION * corroboration
        + W_RELIABILITY * reliability
        - W_CONTRADICTION_PENALTY * contradiction
        - W_GAP_PENALTY * gap_ratio
    )
    return round(_clamp(raw), 4)


def recompute_confidences(state: HorizonState, cards: Sequence[EvidenceCard]) -> None:
    """Overwrite every hypothesis confidence from evidence, keeping a short history.

    ``cards`` is every card the run knows about - in context or evicted - so
    evicting a card from the prompt never silently changes a confidence.
    """
    for hyp in state.hypotheses:
        value = derive_confidence(hyp, cards)
        hyp.confidence = value
        if not hyp.history or hyp.history[-1] != value:
            hyp.history = [*hyp.history, value][-HISTORY_LEN:]


# --------------------------------------------------------------------------- #
# guard predicates                                                             #
# --------------------------------------------------------------------------- #


def can_diagnose(state: HorizonState) -> tuple[bool, str]:
    distinct = len(set(state.observe_tools_run))
    if distinct >= MIN_DISTINCT_OBSERVE_TOOLS:
        return True, ""
    return False, (f"{distinct} of {MIN_DISTINCT_OBSERVE_TOOLS} distinct observe tools have run")


def can_plan(state: HorizonState) -> tuple[bool, str]:
    if len(state.hypotheses) < MIN_HYPOTHESES_TO_PLAN:
        return False, (f"{len(state.hypotheses)} of {MIN_HYPOTHESES_TO_PLAN} hypotheses recorded")
    top = state.top_hypothesis()
    if top is None or top.confidence < MIN_CONFIDENCE_TO_PLAN:
        value = top.confidence if top else 0.0
        return False, (f"top derived confidence {value:.2f} is below {MIN_CONFIDENCE_TO_PLAN:.2f}")
    return True, ""


def failed_verification_card(state: HorizonState) -> EvidenceCard | None:
    """The newest failed-verification card, which a reassessment must cite."""
    failed = [
        c for c in state.evidence if c.tool == VERIFY_TOOL and c.claim.startswith(FAILED_PREFIX)
    ]
    return max(failed, key=lambda c: c.step, default=None)


def reassessment_done(state: HorizonState) -> tuple[bool, str]:
    """REASSESSING -> DIAGNOSING only once the failure is recorded as refuting."""
    card = failed_verification_card(state)
    if card is None:
        # No card to cite means the failure was recorded some other way (an
        # execution error). Nothing to refute; the reassessment is trivially met.
        return True, ""
    if any(card.id in h.refuting for h in state.hypotheses):
        return True, ""
    return False, (
        f"update_hypotheses must cite {card.id} as refuting evidence before re-diagnosing"
    )


def is_excluded(state: HorizonState, attempt: ActionAttempt) -> bool:
    """An identical action that already failed, or was denied, is never re-run."""
    return attempt.signature in state.excluded_actions


def exclude(state: HorizonState, attempt: ActionAttempt) -> None:
    if attempt.signature not in state.excluded_actions:
        state.excluded_actions = [*state.excluded_actions, attempt.signature]


def cycles_exhausted(state: HorizonState) -> bool:
    return state.remediation_cycle >= MAX_REMEDIATION_CYCLES


# --------------------------------------------------------------------------- #
# transitions                                                                  #
# --------------------------------------------------------------------------- #


def move(state: HorizonState, dst: HorizonPhase) -> bool:
    """Apply one legal transition. Returns False for a self-move; raises if illegal."""
    if state.phase is dst:
        return False
    assert_phase_transition(state.phase, dst)
    state.phase = dst
    return True


def guard_after_step(state: HorizonState) -> tuple[HorizonPhase, str]:
    """The phase the brain-driven part of a step earns, and why not further.

    Only the phases the brain works in are decided here. PLANNING's exits are
    decided by the gate chain (policy, not the model); EXECUTING and VERIFYING
    by deterministic execution and measurement - see the orchestrator.
    """
    phase = state.phase
    if phase is HorizonPhase.DETECTING:
        return HorizonPhase.INVESTIGATING, ""
    if phase is HorizonPhase.INVESTIGATING:
        ok, why = can_diagnose(state)
        return (HorizonPhase.DIAGNOSING, "") if ok else (phase, why)
    if phase is HorizonPhase.DIAGNOSING:
        ok, why = can_plan(state)
        return (HorizonPhase.PLANNING, "") if ok else (phase, why)
    if phase is HorizonPhase.PLANNING:
        ok, why = can_plan(state)
        # Evidence can move under the planner's feet (a refutation lands). A
        # plan whose basis fell below the bar goes back to diagnosis.
        return (phase, "") if ok else (HorizonPhase.DIAGNOSING, why)
    if phase is HorizonPhase.REASSESSING:
        ok, why = reassessment_done(state)
        return (HorizonPhase.DIAGNOSING, "") if ok else (phase, why)
    return phase, ""


def advance_requirements(state: HorizonState) -> str:
    """One sentence for the [PHASE] section: what the guard needs next."""
    phase = state.phase
    if phase is HorizonPhase.INVESTIGATING:
        ok, why = can_diagnose(state)
        return (
            "ready to diagnose"
            if ok
            else f"need {MIN_DISTINCT_OBSERVE_TOOLS} distinct observe tools; {why}"
        )
    if phase in (HorizonPhase.DIAGNOSING, HorizonPhase.PLANNING):
        ok, why = can_plan(state)
        if phase is HorizonPhase.DIAGNOSING:
            return (
                "ready to plan"
                if ok
                else (
                    f"need >= {MIN_HYPOTHESES_TO_PLAN} hypotheses citing evidence and top "
                    f"confidence >= {MIN_CONFIDENCE_TO_PLAN}; {why}"
                )
            )
        return "propose one remediation with propose_remediation; policy decides autonomy"
    if phase is HorizonPhase.REASSESSING:
        ok, why = reassessment_done(state)
        return "ready to re-diagnose" if ok else why
    if phase is HorizonPhase.AWAITING_APPROVAL:
        return "waiting for a human decision"
    return ""


__all__ = [
    "FAILED_PREFIX",
    "GAP_PREFIX",
    "GUARD_VERSION",
    "HORIZON_CONFIDENCE_VERSION",
    "MIN_CONFIDENCE_TO_PLAN",
    "MIN_DISTINCT_OBSERVE_TOOLS",
    "MIN_HYPOTHESES_TO_PLAN",
    "VERIFY_TOOL",
    "advance_requirements",
    "can_diagnose",
    "can_plan",
    "cycles_exhausted",
    "derive_confidence",
    "exclude",
    "failed_verification_card",
    "guard_after_step",
    "is_excluded",
    "is_gap",
    "move",
    "reassessment_done",
    "recompute_confidences",
]
