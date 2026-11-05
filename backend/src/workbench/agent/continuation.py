"""Follow-ups continue the model conversation they reply to instead of starting over.

A finished correction run saves its conversation (system prompt, messages, tool results). A
follow-up appends one message: what changed in the model since that run, then its request. The
model keeps what it looked up, and providers that cache a request's opening (llama-server's
prompt cache, Claude, Gemini) read the earlier turns back instead of processing them again.
The person can start fresh instead, which rebuilds the context from the current model.
"""
from __future__ import annotations

from typing import Any

from ..project import Project
from ..schemas import AgentRun
from .tools import entity_line

TABLE = "run_conversations"
MAX_CHANGED_ROWS = 30
MAX_REVISIONS = 50


def save_state(project: Project, run_id: str, state: dict[str, Any]) -> None:
    """Keep what a run's model conversation holds, so a follow-up can continue it. Images are not
    kept: an attachment belongs to its own message."""
    messages = [{k: v for k, v in m.items() if k != "images"} for m in state["messages"]]
    project.store.put_body(TABLE, run_id, {
        "system": state["system"],
        "messages": messages,
        "results": [{**r, "key": list(r["key"])} for r in state["results"]],
        "evidence": [e.model_dump(mode="json") for e in state["evidence"].values()],
    })


def load_state(project: Project, run_id: str) -> dict[str, Any] | None:
    return project.store.get_body(TABLE, run_id)


def _chain(project: Project, base: str, head: str) -> tuple[list, bool]:
    """The revisions after ``base`` up to ``head`` (oldest first), and whether ``head`` is not a
    descendant of ``base`` (the model was taken back, e.g. by undo)."""
    chain, rid = [], head
    for _ in range(MAX_REVISIONS):
        if rid == base:
            return list(reversed(chain)), False
        if rid is None:
            break
        rev = project.revision(rid)
        chain.append(rev)
        rid = rev.parent_id
    return [], True


def changes_since(project: Project, run: AgentRun) -> dict[str, Any]:
    """What happened to the model since ``run`` answered: ``changed`` (anything the run's model
    conversation does not know: edits, other proposals, undo; applying the run's own proposal is
    not), ``summary`` (for the person) and ``note`` (the first part of the follow-up's message)."""
    base, head = run.input_revision, project.head()
    prop = None
    if pid := (run.outcome or {}).get("proposal_id"):
        try:
            prop = project.proposal(pid)
        except KeyError:
            pass
    chain, back = _chain(project, base, head) if base != head else ([], False)
    others = [r for r in chain if not (prop and r.proposal_id == prop.id)]
    changed = back or bool(others)

    lines: list[str] = []
    if prop is not None:
        applied = next((r for r in chain if r.proposal_id == prop.id), None)
        lines.append(f"Your proposed change {prop.id} " + (
            f"was applied as {applied.id}." if applied else
            "is still pending (not applied)." if prop.status == "pending" else
            "was discarded." if prop.status == "dismissed" else
            "is out of date: the model changed under it." if prop.status == "stale" else f"is {prop.status}."))
    if back:
        lines.append(f"The model was taken back to {head} (undo or an earlier revision); later changes are gone.")
    elif others:
        lines.append("The model changed since: " + "; ".join(f"{r.id} ({r.kind}): {r.summary}" for r in others[-10:]))
    if changed:
        before, after = project.view(base).rows(), project.view(head).rows()
        vocab = project.vocab
        diff = []
        for eid in sorted(set(before) | set(after)):
            old = entity_line(vocab, before[eid]) if eid in before else None
            new = entity_line(vocab, after[eid]) if eid in after else None
            if old != new:
                diff.append(f"  + {new}" if old is None else f"  - {old}" if new is None else f"  now {new}")
        if diff:
            lines.append("Objects that differ from what you saw:\n" + "\n".join(diff[:MAX_CHANGED_ROWS])
                         + (f"\n  (+{len(diff) - MAX_CHANGED_ROWS} more; look them up)" if len(diff) > MAX_CHANGED_ROWS else ""))
        lines.append("Earlier tool results may be out of date; check the current model with tools before relying on them.")
    if head != base:
        lines.append(f"Current revision: {head}.")

    if back:
        summary = f"The model went back to {head} since the last reply."
    elif others:
        kinds: dict[str, int] = {}
        for r in others:
            kinds[r.kind] = kinds.get(r.kind, 0) + 1
        names = {"autofix": ("auto-fix", "auto-fixes"), "proposal": ("applied change", "applied changes")}
        what = ", ".join(f"{n} {names.get(k, (k, k + 's'))[n != 1]}" for k, n in kinds.items())
        summary = f"The model changed since the last reply ({base} → {head}: {what})."
    else:
        summary = ""
    return {"changed": changed, "summary": summary,
            "note": ("Since your last reply:\n" + "\n".join(f"- {line}" for line in lines)) if lines else ""}
