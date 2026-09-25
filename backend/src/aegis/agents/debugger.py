"""Candidate patch generation, checked mechanically before anything runs it.

This is the node that turns "we believe the pool is exhausted in ``db.py``" into
a concrete, reviewable change - and it is the node with the most obvious way to
go wrong, so the order of operations is the point:

    diagnosis (not abstained) + localised files
        -> ask the model for ONE unified diff
        -> parse it deterministically and refuse anything that does not parse,
           touches a file outside the localisation, or exceeds the size bound
        -> record it, rejected or not
        -> run it in a disposable, networkless container
        -> set the patch state from the real exit code

The model chooses the change. Nothing else here is the model's decision: which
files may be touched, whether the diff is well formed, which test suite runs,
how big a patch may be, and what the outcome means are all computed. That is the
same split the policy engine applies to environment actions, applied to code.

**No patch is applied to any environment by this node.** It is generated,
validated, executed inside a container and recorded. Promotion is
``PROMOTE_PATCH``, which deliberately has no registered executor - there is
nothing to call even if something decided to.

Two distinctions are load-bearing and are kept apart all the way to the API:

* *the sandbox could not run it* - no daemon, no image, the repository could not
  be fetched - is an unavailable source. The patch stays PROPOSED and an
  evidence gap is recorded.
* *the tests failed* - the container ran and returned non-zero - is a verdict.
  The patch is REJECTED with the exit code.

A run that was killed or timed out is neither: it produced no verdict, so it
never becomes one.
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from aegis.agents.llm import LLMUnavailable, TaskClass, unavailable_reason
from aegis.agents.prompts import DEBUGGER_SYSTEM
from aegis.agents.schemas import PatchProposalOut
from aegis.agents.state import IncidentState
from aegis.agents.workflow import (
    WorkflowDeps,
    _evidence_digest,
    _publish,
    _record_agent_run,
    _tool_context,
    _unavailable_reason,
)
from aegis.core.errors import AegisError, ValidationError
from aegis.core.logging import get_logger
from aegis.domain.enums import AgentRole, SourceType
from aegis.domain.models import UntrustedText
from aegis.mcp import INVESTIGATION_SCOPES
from aegis.persistence.patches import PatchState

log = get_logger(__name__)

# The debugger is the only node granted ``sandbox:run``, and that scope reaches
# a disposable container with no network and no credentials - never the observed
# environment. No remediation scope is added: proposing an environment action
# remains plan_remediation's job, behind the policy engine.
DEBUGGER_SCOPES: Final[frozenset[str]] = INVESTIGATION_SCOPES | {"sandbox:run"}

# Size bounds. A patch this large is not a targeted fix for a diagnosed cause,
# whatever it claims to be, and reviewing it is not something an operator can do
# from an incident page.
MAX_DIFF_BYTES: Final = 64_000
MAX_DIFF_FILES: Final = 10

# How many candidates and how much source text reach the prompt. Bounded for the
# same reason every other digest in the workflow is: a noisy incident must not
# be able to blow the context window.
MAX_PROMPT_FILES: Final = 20
MAX_PROMPT_SYMBOLS: Final = 5
MAX_SNIPPET_CHARS: Final = 1_200

# Exit codes the sandbox bootstrap reserves for its own steps, so that "the
# checkout failed" and "the patch did not apply" are never read as test results.
# They are asserted against ``execution.sandbox._bootstrap_script``.
FETCH_FAILED: Final = 90
PATCH_DID_NOT_APPLY: Final = 91

# Test suite per language, chosen from the file extensions the patch touches.
# Deterministic on purpose: a model naming its own command would be a shell
# injection surface wearing a schema, and the closed suite set in
# ``mcp.tools.sandbox`` exists precisely so no caller has to be trusted with one.
_SUITE_BY_EXTENSION: Final[dict[str, str]] = {
    ".py": "pytest",
    ".go": "go_test",
    ".js": "npm_test",
    ".jsx": "npm_test",
    ".ts": "npm_test",
    ".tsx": "npm_test",
    ".mjs": "npm_test",
}

_OLD_FILE: Final = re.compile(r"^--- (?:a/)?(\S+)")
_NEW_FILE: Final = re.compile(r"^\+\+\+ (?:b/)?(\S+)")
_HUNK: Final = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER: Final = re.compile(r"^diff --git a/(\S+) b/(\S+)$")

# Extended-header directives that make ``git apply`` do something other than
# edit the file the ``---``/``+++`` pair names. A rename takes precedence over
# those headers, so treating these as harmless preamble would leave the file
# scope check enforcing a path git had already stopped using. Refused outright:
# a patch may modify an implicated file, and nothing else.
_FORBIDDEN_PREAMBLE: Final = (
    "new file mode",
    "deleted file mode",
    "similarity index",
    "dissimilarity index",
    "rename from",
    "rename to",
    "copy from",
    "copy to",
    "GIT binary patch",
    "Binary files ",
)

# Lines git puts around a diff that change nothing about which file is edited.
_PREAMBLE: Final = (
    "index ",
    "old mode",
    "new mode",
)


# --------------------------------------------------------------------------- #
# deterministic diff validation                                                #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CheckedDiff:
    """A diff that parsed, stayed inside the localisation and fits the bounds."""

    diff: str
    files: tuple[str, ...]
    lines_added: int
    lines_removed: int


def normalise_path(path: str) -> str:
    """One spelling for a repo-relative path, so comparison means something."""
    clean = path.strip().replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    return clean


def validate_diff(
    diff: str,
    *,
    allowed_paths: Iterable[str],
    max_bytes: int = MAX_DIFF_BYTES,
    max_files: int = MAX_DIFF_FILES,
) -> CheckedDiff:
    """Parse a unified diff and refuse it unless every check passes.

    Raises ``ValidationError`` with a reason an operator can read. The reason is
    recorded on the rejected patch, because "the model produced something we
    would not run" is a finding about the model, not a non-event.

    The checks, and why each one exists:

    * **It must parse as a unified diff**, hunk headers included, with the
      declared line counts matching the hunk bodies. A diff that ``git apply``
      would reject is not worth a container, and a model that emits prose around
      its diff has not followed the contract.
    * **Every path must be one the localisation implicated.** This is the file
      scope check: a patch that edits something the investigation never looked
      at is unreviewable, whatever it does. Creation and deletion are refused by
      the same rule - ``/dev/null`` is in no localisation.
    * **Bounded size and file count.** A 4,000-line "fix" is not a fix.
    """
    text = diff.strip("\n")
    if not text.strip():
        raise ValidationError("the model returned an empty diff")

    size = len(text.encode())
    if size > max_bytes:
        raise ValidationError(
            f"the diff is {size} bytes, over the {max_bytes} byte limit",
            context={"bytes": size, "limit": max_bytes},
        )

    allowed = {normalise_path(p) for p in allowed_paths if p and p.strip()}
    if not allowed:
        raise ValidationError("there are no candidate files a patch could touch")

    lines = text.split("\n")
    total = len(lines)
    files: list[str] = []
    added = 0
    removed = 0
    index = 0

    while index < total:
        line = lines[index]
        if line.startswith(_FORBIDDEN_PREAMBLE):
            raise ValidationError(
                f"line {index + 1} uses {line.split()[0]!r}: a patch may only "
                "modify a localised file, not create, delete, rename or copy one",
                context={"line": index + 1},
            )
        if line.startswith("diff --git "):
            # Cross-checked rather than skipped, so the header git reads and the
            # header this validator enforces cannot disagree.
            git_header = _GIT_HEADER.match(line)
            if git_header is None:
                raise ValidationError(
                    f"line {index + 1} is a 'diff --git' header this validator "
                    "cannot parse; a patch it cannot read is a patch it will not run",
                    context={"line": index + 1},
                )
            for path in (git_header.group(1), git_header.group(2)):
                if normalise_path(path) not in allowed:
                    raise ValidationError(
                        f"{normalise_path(path)} is not one of the localised "
                        "candidate files",
                        context={"path": path, "line": index + 1},
                    )
            index += 1
            continue
        if not line.strip() or line.startswith(_PREAMBLE):
            index += 1
            continue

        old_header = _OLD_FILE.match(line)
        if old_header is None:
            raise ValidationError(
                f"line {index + 1} is not part of a unified diff: {line[:80]!r}",
                context={"line": index + 1},
            )
        if index + 1 >= total:
            raise ValidationError("the diff ends after a '---' header with no '+++'")
        new_header = _NEW_FILE.match(lines[index + 1])
        if new_header is None:
            raise ValidationError(
                f"line {index + 2} should be a '+++' header, found {lines[index + 1][:80]!r}",
                context={"line": index + 2},
            )

        old_path = normalise_path(old_header.group(1))
        new_path = normalise_path(new_header.group(1))
        if old_path != new_path:
            # Covers renames and, more importantly, /dev/null on either side:
            # a patch may change an implicated file, not invent or remove one.
            raise ValidationError(
                f"the diff moves {old_path} to {new_path}; a patch may only "
                "modify files that were localised",
                context={"from": old_path, "to": new_path},
            )
        if new_path not in allowed:
            raise ValidationError(
                f"{new_path} is not one of the localised candidate files",
                context={"path": new_path, "allowed": sorted(allowed)[:10]},
            )
        if new_path not in files:
            files.append(new_path)
        if len(files) > max_files:
            raise ValidationError(
                f"the diff touches more than {max_files} files",
                context={"files": len(files), "limit": max_files},
            )

        index += 2
        hunks = 0
        while index < total:
            header = _HUNK.match(lines[index])
            if header is None:
                break
            index += 1
            want_old = int(header.group(2) or 1)
            want_new = int(header.group(4) or 1)
            saw_old = 0
            saw_new = 0
            while index < total and (saw_old < want_old or saw_new < want_new):
                body = lines[index]
                index += 1
                if body.startswith("\\"):      # "\ No newline at end of file"
                    continue
                if body == "" or body.startswith(" "):
                    saw_old += 1
                    saw_new += 1
                elif body.startswith("+"):
                    saw_new += 1
                    added += 1
                elif body.startswith("-"):
                    saw_old += 1
                    removed += 1
                else:
                    raise ValidationError(
                        f"line {index} is inside a hunk but starts with "
                        f"{body[:1]!r}; unified diff bodies start with "
                        "' ', '+' or '-'",
                        context={"line": index},
                    )
            # "\ No newline at end of file" after the hunk's last line falls
            # outside the count loop above, because the counts are already
            # satisfied by the time it is reached. git emits it for any file
            # without a trailing newline, so rejecting it would refuse ordinary,
            # applicable diffs and blame the model for the validator's gap.
            while index < total and lines[index].startswith("\\"):
                index += 1
            if saw_old != want_old or saw_new != want_new:
                raise ValidationError(
                    f"a hunk for {new_path} declares -{want_old}/+{want_new} "
                    f"lines but contains -{saw_old}/+{saw_new}",
                    context={"path": new_path},
                )
            hunks += 1

        if hunks == 0:
            raise ValidationError(
                f"the diff for {new_path} has no '@@' hunk header",
                context={"path": new_path},
            )

    if not files:
        raise ValidationError("the diff contains no file headers")
    if added == 0 and removed == 0:
        raise ValidationError("the diff changes no lines")

    # Re-emitted with a trailing newline: git apply refuses a patch whose last
    # line has none, and the model routinely omits it.
    return CheckedDiff(
        diff=text + "\n",
        files=tuple(files),
        lines_added=added,
        lines_removed=removed,
    )


def suite_for(paths: Sequence[str]) -> str | None:
    """The test suite to run, from the extensions the patch touches.

    ``None`` when nothing maps: an unknown language is a reason to record the
    patch untested, not a reason to guess at a command.
    """
    for path in paths:
        _, dot, extension = path.rpartition(".")
        if not dot:
            continue
        suite = _SUITE_BY_EXTENSION.get(f".{extension.lower()}")
        if suite is not None:
            return suite
    return None


def repo_url(repo: str) -> str:
    """A clone URL for a repository the localisation named.

    Repos arrive as ``owner/name`` from ``service_repositories``. Anything that
    already looks like a URL is passed through untouched, so a self-hosted
    forge configured that way still works.
    """
    if "://" in repo:
        return repo
    return f"https://github.com/{repo.strip('/')}.git"


# --------------------------------------------------------------------------- #
# the node                                                                     #
# --------------------------------------------------------------------------- #


def _candidate_digest(candidates: Sequence[Any]) -> str:
    """Candidate paths and why each was selected.

    The reason strings are built from commit metadata, so they carry text an
    author controlled. They travel in an untrusted envelope for the same reason
    a commit message does: a repository is not a trusted instruction source.
    """
    lines = "\n".join(
        f"- {c.path} (repo {c.repo} @ {c.ref}): {c.reason}"
        for c in candidates[:MAX_PROMPT_FILES]
    )
    return UntrustedText(text=lines, origin="code_localisation").as_prompt_block()


def _symbol_digest(localization: Any) -> str:
    """Source at the implicated symbols, as data rather than as instruction.

    This is the strongest prompt-injection surface in the node: a comment in a
    repository Aegis indexes can say anything at all. It is wrapped rather than
    interpolated, so the system prompt's rule about ``<untrusted>`` blocks
    actually applies to something (CLAUDE.md invariant 7).
    """
    symbols = list(getattr(localization, "symbols", ()) or ())[:MAX_PROMPT_SYMBOLS]
    if not symbols:
        return "(no symbol-level source was retrieved)"
    # One envelope per symbol rather than one around all of them: each snippet
    # stays under UntrustedText.MAX_RENDER, so nothing is silently cut, and each
    # block carries the path it came from.
    return "\n\n".join(
        f"{s.path}:L{s.start_line}-L{s.end_line} {s.symbol or ''}\n"
        + UntrustedText(
            text=s.snippet[:MAX_SNIPPET_CHARS], origin="repository_source"
        ).as_prompt_block()
        for s in symbols
    )


def make_debug_remediation(
    deps: WorkflowDeps,
) -> Callable[[IncidentState], Awaitable[dict[str, Any]]]:
    """Build the ``debug_remediation`` node closed over the injected deps."""

    async def _gap(
        incident_id: str,
        reason: str,
        *,
        source: str = "sandbox",
        source_type: SourceType = SourceType.SANDBOX,
    ) -> dict[str, Any]:
        """Record an unconsulted source. Never a claim that nothing was wrong."""
        item = await deps.evidence.record_unavailable(
            incident_id=incident_id,
            source=source,
            source_type=source_type,
            reason=reason,
        )
        return {
            "evidence_ids": [item.id],
            "evidence_gaps": [{"source": source, "reason": reason}],
        }

    async def debug_remediation(state: IncidentState) -> dict[str, Any]:
        """Write, check and test one candidate patch. Never deploy one."""
        deps.budget.check("debug_remediation")
        started = time.perf_counter()
        incident_id = state["incident_id"]
        diagnosis = state.get("diagnosis") or {}

        # Preconditions. Generating a patch for a cause that was not established
        # is the fabricated confidence this system exists to prevent, so the node
        # does nothing at all rather than doing something cheap and plausible.
        if state.get("abstained") or not diagnosis.get("statement"):
            return {}
        if deps.patches is None:
            return {}

        localization = deps.localization
        if localization is None and not deps.localization_attempted:
            # The run resumed from a checkpoint past localize_code, so the
            # handoff was lost with the process that made it. That is "we could
            # not look", and reporting it as "there is nothing to patch" would
            # be the collapse this system exists to avoid.
            reason = (
                "code localisation did not run in this process; the "
                "investigation resumed past it, so no patch was attempted"
            )
            log.warning("localisation handoff lost on resume", incident_id=incident_id)
            await _record_agent_run(
                deps, incident_id, AgentRole.DEBUGGER,
                task="write a candidate patch",
                summary=reason, evidence_ids=[], status="failed", error=reason,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return await _gap(
                incident_id, reason,
                source="code_localization", source_type=SourceType.VCS,
            )

        candidates = list(getattr(localization, "files", ()) or ()) if localization else []
        if not candidates:
            # localize_code ran and produced nothing. It has already recorded
            # its own agent run saying so, and an absent candidate set is a
            # finding rather than a gap.
            return {}

        # One diff applies to one checkout, so the patch is scoped to the
        # best-ranked repository and the files inside it.
        repo = str(candidates[0].repo)
        base_ref = str(candidates[0].ref)
        in_repo = [c for c in candidates if str(c.repo) == repo]
        allowed = [normalise_path(str(c.path)) for c in in_repo]

        user = (
            f"Diagnosis: {diagnosis.get('statement', '')}\n"
            f"Root cause category: {diagnosis.get('root_cause_category')}\n"
            f"Repository: {repo} at {base_ref}\n\n"
            f"CANDIDATE FILES - the diff may touch these and nothing else:\n"
            f"{_candidate_digest(in_repo)}\n\n"
            f"SOURCE AT THE IMPLICATED SYMBOLS:\n{_symbol_digest(localization)}\n\n"
            f"EVIDENCE:\n{_evidence_digest(state)}\n"
        )

        try:
            out, meta = await deps.router.structured(
                schema=PatchProposalOut,
                system=DEBUGGER_SYSTEM,
                user=user,
                task=TaskClass.CODE,
            )
            deps.budget.charge_llm(input_tokens=len(user) // 4, output_tokens=900)
        except LLMUnavailable as exc:
            log.warning("patch generation unavailable", error=str(exc))
            await _record_agent_run(
                deps, incident_id, AgentRole.DEBUGGER,
                task="write a candidate patch",
                summary=unavailable_reason(exc),
                evidence_ids=[], status="failed", error=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return {}

        if not out.propose_patch or not out.diff.strip():
            await _record_agent_run(
                deps, incident_id, AgentRole.DEBUGGER,
                task="write a candidate patch",
                summary=f"no patch proposed: {out.reason or 'declined'}",
                evidence_ids=[], meta=meta,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return {}

        # ---- deterministic validation, before anything executes ----------- #
        try:
            checked = validate_diff(out.diff, allowed_paths=allowed)
        except ValidationError as exc:
            patch, created = await deps.patches.propose(
                incident_id=incident_id,
                repo=repo,
                base_ref=base_ref,
                summary=(out.summary or "unvalidated candidate patch")[:1000],
                diff=out.diff,
                files_changed=[],
                rationale=out.rationale,
                supporting_evidence=_cited(state, out.supporting_evidence),
            )
            if created:
                # Recorded, then rejected - never silently dropped. A malformed
                # patch is evidence about the debugger, and the reason is what
                # makes it actionable.
                await deps.patches.transition(
                    patch.id,
                    to=PatchState.REJECTED,
                    expected=PatchState.PROPOSED,
                    reason=f"rejected before execution: {exc.message}",
                )
            log.warning(
                "candidate patch refused by validation",
                incident_id=incident_id,
                patch_id=patch.id,
                reason=exc.message,
            )
            await _record_agent_run(
                deps, incident_id, AgentRole.DEBUGGER,
                task="write a candidate patch",
                summary=f"patch rejected without running it: {exc.message}",
                evidence_ids=[], status="failed", error=exc.message, meta=meta,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return {}

        patch, created = await deps.patches.propose(
            incident_id=incident_id,
            repo=repo,
            base_ref=base_ref,
            summary=(out.summary or "candidate patch")[:1000],
            diff=checked.diff,
            files_changed=list(checked.files),
            rationale=out.rationale,
            supporting_evidence=_cited(state, out.supporting_evidence),
        )
        await _publish(deps, incident_id, {
            "type": "patch_proposed",
            "patch_id": patch.id,
            "files": len(checked.files),
            "state": patch.state.value,
        })

        if not created:
            # The identical diff was already proposed for this incident. Running
            # it again would burn a container to learn what is already recorded.
            await _record_agent_run(
                deps, incident_id, AgentRole.DEBUGGER,
                task="write a candidate patch",
                summary=(
                    f"identical patch {patch.id} was already proposed "
                    f"({patch.state.value}); not re-running it"
                ),
                evidence_ids=[], meta=meta,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return {}

        # ---- run it, somewhere disposable --------------------------------- #
        suite = suite_for(checked.files)
        if deps.tools is None or suite is None:
            reason = (
                "the tool boundary is not configured; the patch was not run"
                if deps.tools is None
                else (
                    "no test suite is defined for "
                    f"{', '.join(checked.files[:3])}; the patch was not run"
                )
            )
            await deps.patches.transition(
                patch.id, to=PatchState.PROPOSED, expected=PatchState.PROPOSED,
                reason=reason,
            )
            await _record_agent_run(
                deps, incident_id, AgentRole.DEBUGGER,
                task="test a candidate patch",
                summary=reason, evidence_ids=[], status="failed", meta=meta,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            return await _gap(incident_id, reason)

        context = _tool_context(
            deps, state, node="debug_remediation", scopes=DEBUGGER_SCOPES
        )
        ids: list[str] = []
        gaps: list[dict[str, str]] = []
        url = repo_url(repo)

        # Reproduction first: a suite that already passes cannot demonstrate the
        # fault, so "the tests pass after the patch" would prove nothing. The
        # answer is recorded either way and never assumed.
        reproduced = await _run_sandbox(
            deps, context, "run_reproduction",
            {"repo_url": url, "base_ref": base_ref, "suite": suite},
        )
        ids.extend(reproduced.evidence_ids)
        if reproduced.gap:
            gaps.append({"source": "sandbox", "reason": reproduced.gap})
        if reproduced.run_id:
            await deps.patches.link_runs(patch.id, reproduction_run_id=reproduced.run_id)
        # A reproduction "succeeds" when the suite FAILS: that is the failure
        # being reproduced. An exit of zero means the suite never showed the
        # problem, which is worth recording and is not a reason to stop.
        failure_reproduced = reproduced.ran and reproduced.exit_code not in (None, 0)
        if failure_reproduced:
            await deps.patches.transition(
                patch.id,
                to=PatchState.REPRODUCED,
                expected=PatchState.PROPOSED,
                reason=f"suite failed at {base_ref} (exit {reproduced.exit_code})",
            )

        tested = await _run_sandbox(
            deps, context, "test_patch",
            {
                "repo_url": url,
                "base_ref": base_ref,
                "suite": suite,
                "patch": checked.diff,
            },
        )
        ids.extend(tested.evidence_ids)
        if tested.gap:
            gaps.append({"source": "sandbox", "reason": tested.gap})
        if tested.run_id:
            await deps.patches.link_runs(patch.id, test_run_id=tested.run_id)

        expected = PatchState.REPRODUCED if failure_reproduced else PatchState.PROPOSED
        state_now, summary = _judge(tested, base_ref)
        if state_now is not None:
            await deps.patches.transition(
                patch.id, to=state_now, expected=expected, reason=summary
            )
        else:
            # No verdict: the sandbox never produced one. The patch keeps the
            # state it earned and the reason says why nothing more is known.
            await deps.patches.transition(
                patch.id, to=expected, expected=expected, reason=summary
            )
            if not gaps:
                gaps.append({"source": "sandbox", "reason": summary})

        await _publish(deps, incident_id, {
            "type": "patch_tested",
            "patch_id": patch.id,
            "state": (state_now or expected).value,
        })
        await _record_agent_run(
            deps, incident_id, AgentRole.DEBUGGER,
            task=f"test a candidate patch against {suite}",
            summary=f"{patch.id}: {summary}",
            evidence_ids=ids, meta=meta,
            status="done" if state_now is PatchState.TESTED else "failed",
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return {"evidence_ids": ids, "evidence_gaps": gaps}

    return debug_remediation


# --------------------------------------------------------------------------- #
# sandbox plumbing                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _RunOutcome:
    """What one sandbox tool call told us, with the two failure modes apart.

    ``ran`` is False when the sandbox could not be used at all. ``ran`` with
    ``exit_code is None`` means it started and was stopped - a timeout or a kill
    - which is an incomplete measurement, not a result.
    """

    ran: bool
    run_id: str | None
    exit_code: int | None
    timed_out: bool
    killed: bool
    evidence_ids: list[str]
    gap: str


async def _run_sandbox(
    deps: WorkflowDeps, context: Any, tool: str, args: dict[str, Any]
) -> _RunOutcome:
    """Invoke one sandbox tool through the boundary.

    Through the invoker rather than through ``deps.sandbox`` directly, for the
    same reasons ``recall_memory`` goes that way: the call is scope-checked,
    deadline-bounded, charged to the run's budget exactly once, written to
    ``tool_calls``, and its evidence is recorded once with the right provenance
    URI. Calling the runner here would do the same work and leave no trace that
    it happened - and the budget would then have to be charged by hand, which is
    how a read ends up billed twice.
    """
    try:
        result = await deps.tools.invoke(tool, args, context)
    except AegisError as exc:
        # The invoker refused the call outright - an unknown tool, a denied
        # scope, an expired deadline. Nothing ran.
        log.warning("sandbox tool call refused", tool=tool, error=str(exc))
        return _RunOutcome(
            ran=False, run_id=None, exit_code=None, timed_out=False, killed=False,
            evidence_ids=[], gap=f"{tool} was refused: {exc.message}",
        )

    ids = list(result.evidence_ids)
    value = result.value
    run_id = str(getattr(value, "sandbox_id", "") or "") or None

    if run_id is None:
        # No sandbox id means no container ran: the tool answered degraded with
        # an empty result. That is an unavailable source, not a failing test.
        return _RunOutcome(
            ran=False, run_id=None, exit_code=None, timed_out=False, killed=False,
            evidence_ids=ids, gap=_unavailable_reason(result),
        )

    timed_out = bool(getattr(value, "timed_out", False))
    killed = bool(getattr(value, "killed", False))
    exit_code = getattr(value, "exit_code", None)
    return _RunOutcome(
        ran=True,
        run_id=run_id,
        exit_code=None if (timed_out or killed) else exit_code,
        timed_out=timed_out,
        killed=killed,
        evidence_ids=ids,
        gap=_unavailable_reason(result) if (timed_out or killed) else "",
    )


def _judge(run: _RunOutcome, base_ref: str) -> tuple[PatchState | None, str]:
    """Turn one test run into a patch state and a sentence explaining it.

    ``None`` means no state change is warranted because no verdict was reached.
    The three reserved bootstrap exit codes are separated out, because "the
    repository could not be fetched" is not a statement about the patch at all.
    """
    if not run.ran:
        return None, run.gap or "the sandbox could not run the patch"
    if run.timed_out or run.killed:
        return None, (
            "the sandbox stopped the run before it finished; no test verdict "
            "was reached"
        )
    if run.exit_code == 0:
        return PatchState.TESTED, "the suite passed with the patch applied"
    if run.exit_code == PATCH_DID_NOT_APPLY:
        return PatchState.REJECTED, f"the patch did not apply to {base_ref}"
    if run.exit_code == FETCH_FAILED:
        return None, f"the repository could not be fetched at {base_ref}"
    return PatchState.REJECTED, f"the suite failed with the patch applied (exit {run.exit_code})"


def _cited(state: IncidentState, claimed: Sequence[str]) -> list[str]:
    """Keep only evidence ids this incident actually holds.

    A model listing an id it invented does not get to attach it to a stored
    artefact; the citation validator would reject it downstream anyway, and a
    patch row carrying unresolvable ids is worse than one carrying none.
    """
    known = set(state.get("evidence_ids") or [])
    return [e for e in dict.fromkeys(claimed) if e in known][:32]


__all__ = [
    "DEBUGGER_SCOPES",
    "MAX_DIFF_BYTES",
    "MAX_DIFF_FILES",
    "CheckedDiff",
    "make_debug_remediation",
    "normalise_path",
    "repo_url",
    "suite_for",
    "validate_diff",
]
