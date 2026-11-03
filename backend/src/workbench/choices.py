"""The assistant's choices as pending proposals: each option is built and validated like any
change, so picking one is reviewing (or, in auto-fix, applying) an ordinary proposal."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .operations import OperationError, OperationList
from .schemas import ChangeProposal, SelectionScope

if TYPE_CHECKING:
    from .project import Project

MAX_OPTIONS = 6


def build_option(project: Project, base: str, ops: list[dict], label: str, selection: SelectionScope,
                 request: str, run_id: str) -> ChangeProposal | None:
    """A pending proposal for one option, or None when its operations do not apply or change nothing."""
    try:
        cand = project.build_candidate(base, OperationList.validate_python(ops), selection)
    except (OperationError, ValueError):
        return None
    if not cand.diff.added and not cand.diff.removed:
        return None
    rev = project.revision(base)
    return project.save_proposal(cand, selection, request, label, [], [], run_id, rev.validation,  # type: ignore[arg-type]
                                 project.issues(base))


def _note(prop: ChangeProposal) -> str | None:
    v = prop.validation
    if v is None:
        return None
    parts = [f"fixes {len(v.resolved)} issue(s)" if v.resolved else "",
             f"adds {len(v.introduced)} issue(s)" if v.introduced else ""]
    return ", ".join(p for p in parts if p) or None


def chat_choices(project: Project | None, base: str, choices: list[dict[str, Any]], selection: SelectionScope,
                 request: str, run_id: str) -> tuple[list[dict], list[dict]]:
    """Options for a chat reply: (choices offered as options, choices left as questions).

    An offered choice is {question, options: [{label, proposal_id, note?}]}. Options whose operations
    do not apply are dropped; a choice with fewer than two working options stays a question for the
    person to answer in words.
    """
    offered, unbuilt = [], []
    for choice in choices:
        options = []
        for opt in choice.get("options", [])[:MAX_OPTIONS]:
            if project is None or not opt.get("operations"):
                continue
            prop = build_option(project, base, opt["operations"], opt.get("label") or "", selection, request, run_id)
            if prop is not None:
                note = _note(prop)
                options.append({"label": opt.get("label") or "", "proposal_id": prop.id,
                                **({"note": note} if note else {})})
        if len(options) >= 2:
            offered.append({"question": choice.get("question") or "", "options": options})
        else:
            if project is not None:
                for o in options:  # one working option is no choice: ask instead
                    project.dismiss_proposal(o["proposal_id"])
            unbuilt.append(choice)
    return offered, unbuilt
