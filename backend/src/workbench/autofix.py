"""Automatic fixing of validation issues, with each fix classified by checks, not by the model.

Issues are grouped by what is wrong (severity, shape, path), so one decision covers every
issue with the same cause. For each group the assistant proposes a fix on the current head;
``verify`` then decides whether the fix is obvious:

- it resolves the group's issues and introduces no new ones (validation and the repair
  engine's soundness gate);
- it only changes the issues' own objects (new objects are fine), deletes nothing, and
  overrides no human-locked field;
- it raises no notes or questions, and every vocabulary term it chooses is grounded: already
  used in the model, named by the issue itself, or in the evidence of the objects concerned;
- it is minimal: without any one of its operations (or updated fields) the issues would not
  all be resolved, so nothing rides along with the fix (a "while I'm here" change).

An obvious fix is applied at once as an automatic (unlocked) revision. A group that needs a
decision gets options to pick from (see "choices" below); anything else is left as a proposal
for review, or as the assistant's questions when it needs input. Groups run one
after another on the newest revision, so each sees the fixes before it.

Interface: ``group_issues``, ``verify``, ``ungrounded_terms`` and ``run_autofix``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rdflib import URIRef

from .operations import TERM_FIELDS, OperationError, OperationList
from .schemas import ChangeProposal, ReviewIssue, SelectionScope

if TYPE_CHECKING:
    from .agent.guidance import SkillGuidance
    from .llm import CancelToken, LLMClient
    from .project import Project

MAX_GROUP = 8  # issues per assistant request; larger groups are split
SEVERITY_ORDER = {"violation": 0, "warning": 1, "suggestion": 2}


# ------------------------------------------------------------------ grouping

def _cause(issue: ReviewIssue) -> tuple:
    findings = issue.details.get("findings", [])
    return (issue.severity, tuple(sorted({str(d.get("shape")) for d in findings})),
            tuple(sorted({str(d.get("path")) for d in findings})), issue.category if not findings else "")


def group_issues(issues: list[ReviewIssue]) -> list[list[ReviewIssue]]:
    """Open issues grouped by cause (severity, shape, path), violations first, at most MAX_GROUP each."""
    groups: dict[tuple, list[ReviewIssue]] = {}
    for issue in issues:
        if issue.resolution_state == "open":
            groups.setdefault(_cause(issue), []).append(issue)
    out = []
    for key in sorted(groups, key=lambda k: (SEVERITY_ORDER.get(k[0], 3), -len(groups[k]))):
        members = groups[key]
        out += [members[i:i + MAX_GROUP] for i in range(0, len(members), MAX_GROUP)]
    return out


def instruction(issues: list[ReviewIssue], rows: dict[str, Any]) -> str:
    """The request for one group (as "add to chat" would phrase it), asking for questions over guesses."""
    def describe(issue: ReviewIssue, n: int) -> str:
        objects = [f"{rows[i].label} ({rows[i].kind}, {i})" if i in rows else i for i in issue.affected_ids]
        findings = [", ".join(x for x in (d.get("message"), d.get("path") and f"path {d['path']}",
                                          d.get("shape") and f"shape {d['shape']}") if x)
                    for d in issue.details.get("findings", [])]
        return "\n".join(x for x in (
            f"{f'{n}. ' if len(issues) > 1 else ''}[{issue.id}] {issue.severity}: {issue.explanation}",
            f"Affected objects: {'; '.join(objects)}" if objects else "",
            f"Validation details: {' | '.join(findings)}" if findings else "") if x)

    return "\n\n".join([
        "Fix these issues; they share a cause, so fix them together. Use only what the model, the "
        "vocabulary and the sources establish. If a fix needs a value or a choice that nothing "
        "establishes, do not guess: ask a question instead. If an issue is expected, say why. "
        "Change nothing else and ask nothing about other issues: they are handled separately.",
        *(describe(issue, n + 1) for n, issue in enumerate(issues)),
    ])


def selection_for(issues: list[ReviewIssue], rows: dict[str, Any]) -> SelectionScope:
    ids = list(dict.fromkeys(i for issue in issues for i in issue.affected_ids if i in rows))
    return SelectionScope(entity_ids=ids)


# ---------------------------------------------------------------- verifying

def verify(project: Project, proposal: ChangeProposal, issues: list[ReviewIssue], chosen: bool = False) -> list[str]:
    """Why this proposal is not an obvious fix for these issues; empty when it is. ``chosen``: an
    option the person picks, so its choices need no other grounding and it asks nothing."""
    reasons: list[str] = []
    if not proposal.operations:
        return ["it changes nothing" + (" (it dismisses the issues instead)" if proposal.issue_dismissals else "")]
    val = proposal.validation
    resolved = set(val.resolved) if val else set()
    unresolved = [i.explanation for i in issues if i.explanation not in resolved]
    if unresolved:
        reasons.append(f"{len(unresolved)} of the issues remain")
    introduced = (list(val.introduced) if val else []) + list((proposal.gate or {}).get("introduced", []))
    if introduced:
        reasons.append(f"it introduces {len(introduced)} new issue(s)")
    if deleted := [c.label for c in proposal.changes if c.change == "deleted"]:
        reasons.append(f"it removes {', '.join(deleted[:3])}")
    if outside := [c.label for c in proposal.changes if c.change == "updated" and not c.in_selection]:
        reasons.append(f"it changes other objects: {', '.join(outside[:3])}")
    if locked := [c.label for c in proposal.changes if c.overrides_locked]:
        reasons.append(f"it overrides your edits on {', '.join(locked[:3])}")
    if proposal.questions and not chosen:
        reasons.append("the assistant has questions")
    reasons += proposal.notes
    if not chosen and (ungrounded := ungrounded_terms(project, proposal, issues)):
        reasons.append("it chooses " + ", ".join(project.vocab.curie(t) for _, _, t in ungrounded[:4])
                       + ", which nothing in the model, the issues or the evidence names")
    if not reasons and (extra := _unneeded_parts(project, proposal, issues)):
        reasons.append("it also makes changes the fix does not need: " + ", ".join(extra[:4]))
    return reasons


MAX_PARTS = 12  # larger fixes are not tested part by part (and so are not minimal-checked)


def _unneeded_parts(project: Project, proposal: ChangeProposal, issues: list[ReviewIssue]) -> list[str]:
    """Operations, or fields of update operations, the fix resolves its issues without."""
    ops = [op.model_dump(exclude_unset=True) for op in proposal.operations]
    parts = [(k, f) for k, op in enumerate(ops)
             for f in (sorted(set(op) - {"op", "id"}) if op["op"].startswith("update_") else [None])]
    if len(parts) <= 1 or len(parts) > MAX_PARTS:
        return []
    targets = {i.explanation for i in issues}
    unneeded = []
    for k, f in parts:
        trial = [dict(op) for op in ops]
        if f is not None and len(set(trial[k]) - {"op", "id"}) > 1:
            trial[k].pop(f)
            what = f"{f.replace('_', ' ')} of {_name(project, proposal, trial[k]['id'])}"
        else:
            trial.pop(k)
            what = ops[k]["op"].replace("_", " ") + (f" {ops[k].get('label')}" if ops[k].get("label") else "")
        try:
            cand = project.build_candidate(proposal.base_revision, OperationList.validate_python(trial), lock=False)
        except OperationError:
            continue  # later operations need it
        if not targets & {i.explanation for i in cand.issues}:
            unneeded.append(what)
    return unneeded


def _name(project: Project, proposal: ChangeProposal, eid: str) -> str:
    row = project.view(proposal.base_revision).rows().get(eid)
    return getattr(row, "label", eid)


def ungrounded_terms(project: Project, proposal: ChangeProposal, issues: list[ReviewIssue]) -> list[tuple[int, str, str]]:
    """(operation index, field, term) for each vocabulary term the proposal chooses that is not
    already used in the model, named by the issues, or in the evidence of their objects."""
    vocab, pg = project.vocab, project.graph(proposal.base_revision)
    issue_text = json.dumps([[i.explanation, i.details] for i in issues], default=str).lower()
    evidence_text = ""
    for eid in {i for issue in issues for i in issue.affected_ids}:
        node = pg.iri(eid)
        for oid in (pg.evidence(node) if node is not None else []):
            evidence_text += json.dumps(project.store.get_body("observations", oid) or {}, default=str).lower()

    def grounded(iri: str) -> bool:
        if (None, None, URIRef(iri)) in pg.model:
            return True
        t = vocab.term(iri)
        names = {iri.lower(), vocab.curie(iri).lower(), *([t.label.lower(), t.symbol.lower()] if t else [])} - {""}
        return any(n in issue_text for n in names) or any(n in evidence_text for n in names if len(n) > 2)

    return [(k, f, v) for k, f, v in _term_choices(proposal) if not grounded(v)]


def _term_choices(proposal: ChangeProposal) -> list[tuple[int, str, str]]:
    out = []
    for k, op in enumerate(proposal.operations):
        for f, v in op.model_dump(exclude_unset=True).items():
            if isinstance(v, str) and (f in TERM_FIELDS or (op.op == "relate" and f == "object" and ":" in v)):
                out.append((k, f, v))
    return out


# ------------------------------------------------------------------ choices
#
# A group that needs a decision is offered options; each option is an ordinary pending proposal
# (so choosing one is applying it, which locks what it sets: the person confirmed it), checked
# like an automatic fix except that the person's choice is its grounding. Options come from the
# assistant (choices with operations) or, when a fix was held back only for a term nothing
# grounds, from substituting the other candidate terms of that kind and keeping those that work.

MAX_OPTIONS = 6


def _option(project: Project, base: str, issues: list[ReviewIssue], ops: list[dict], label: str,
            selection: SelectionScope, request: str, run_id: str) -> dict | None:
    """A checked option ({label, proposal_id, note?}), or None when it does not work."""
    try:
        cand = project.build_candidate(base, OperationList.validate_python(ops), selection)
    except (OperationError, ValueError):
        return None
    if not cand.diff.added and not cand.diff.removed:
        return None
    rev = project.revision(base)
    prop = project.save_proposal(cand, selection, request, label, [], [], run_id, rev.validation,  # type: ignore[arg-type]
                                 project.issues(base))
    return _checked(project, prop, issues, label)


def _checked(project: Project, prop: ChangeProposal, issues: list[ReviewIssue], label: str) -> dict | None:
    reasons = verify(project, prop, issues, chosen=True)
    remain = [r for r in reasons if r.endswith("of the issues remain")]
    if reasons and (reasons != remain or len(set(prop.validation.resolved if prop.validation else [])
                                              & {i.explanation for i in issues}) == 0):
        project.dismiss_proposal(prop.id)
        return None
    return {"label": label, "proposal_id": prop.id, **({"note": remain[0].replace("of the issues", "issue(s)")
                                                        .replace("remain", "would remain")} if remain else {})}


def _model_choices(project: Project, base: str, issues: list[ReviewIssue], choices: list[dict],
                   selection: SelectionScope, request: str, run_id: str) -> list[dict]:
    out = []
    for choice in choices:
        options = [o for o in (_option(project, base, issues, opt["operations"], opt["label"], selection, request, run_id)
                               for opt in choice["options"][:MAX_OPTIONS] if opt.get("operations")) if o]
        if options:
            out.append({"question": choice["question"], "options": options})
    return out


def _term_alternatives(project: Project, proposal: ChangeProposal, issues: list[ReviewIssue],
                       run_id: str) -> list[dict]:
    """The same fix with each candidate term in place of the one term nothing grounds."""
    ungrounded = ungrounded_terms(project, proposal, issues)
    if len(ungrounded) != 1:
        return []
    k, fname, term = ungrounded[0]
    vocab, base = project.vocab, proposal.base_revision
    pg, ops = project.graph(base), [op.model_dump(exclude_unset=True) for op in proposal.operations]
    kind = vocab.kind_of(term)
    pool = {str(o) for o in pg.model.objects() if isinstance(o, URIRef) and vocab.kind_of(str(o)) == kind}
    if fname == "unit":  # the units valid for the point's quantity kind
        row = project.view(base).rows().get(ops[k].get("id", ""))
        qk = ops[k].get("quantity_kind") or getattr(getattr(row, "quantity_kind", None), "iri", None)
        pool |= {t.iri for t in vocab.units_for(qk)} if qk else set()
    alternatives = [term, *sorted(pool - {term}, key=lambda t: vocab.label(t).lower())][:MAX_OPTIONS + 6]
    subject = _name(project, proposal, ops[k].get("id") or ops[k].get("subject") or "")
    options = []
    for alt in alternatives:
        label = f"{vocab.label(alt)} ({vocab.curie(alt)})"
        if alt == term:
            option = _checked(project, proposal, issues, label)
        else:
            trial = [dict(op) for op in ops]
            trial[k][fname] = alt
            option = _option(project, base, issues, trial, label, proposal.selection, proposal.instruction, run_id)
        if option:
            options.append(option)
        if len(options) >= MAX_OPTIONS:
            break
    return [{"question": f"Which {fname.replace('_', ' ')} for {subject}?", "options": options}] if options else []


# ------------------------------------------------------------------ running

@dataclass
class AutofixOutcome:
    groups: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""

    @property
    def autofix(self) -> dict[str, Any]:
        """Stored on the run's outcome for the interface."""
        return {"groups": self.groups,
                "revisions": [g["revision"] for g in self.groups if g.get("revision")]}


def run_autofix(project: Project, llm: LLMClient, guidance: SkillGuidance, issue_ids: list[str] | None,
                run_id: str, progress: Callable[[str, str, dict], None], cancel: CancelToken) -> AutofixOutcome:
    """Fix the given open issues (default: every open violation), group by group."""
    from .agent.correction import run_correction

    head = project.head()
    chosen = [i for i in project.issues(head) if i.resolution_state == "open" and (
        i.id in issue_ids if issue_ids else i.severity == "violation")]
    groups = group_issues(chosen)
    outcome = AutofixOutcome()
    progress("autofix", f"{len(chosen)} issue(s) in {len(groups)} group(s)", {"groups": len(groups)})
    for n, group in enumerate(groups, 1):
        cancel.check()
        head = project.head()
        still_open = {i.id: i for i in project.issues(head) if i.resolution_state == "open"}
        group = [still_open[i.id] for i in group if i.id in still_open]
        record: dict[str, Any] = {"issues": [i.id for i in group], "explanations": [i.explanation for i in group]}
        outcome.groups.append(record)
        if not group:
            record.update(status="resolved", reasons=["fixed by an earlier group"])
            continue
        tag = f"{n}/{len(groups)}"
        progress("group", f"Group {tag}: {group[0].explanation[:90]}" + (f" (+{len(group) - 1})" if len(group) > 1 else ""),
                 {"group": n, "issues": record["issues"]})
        rows = project.view(head).rows()
        try:
            out = run_correction(project, llm, guidance, head, selection_for(group, rows), instruction(group, rows),
                                 run_id, lambda s, m, d: progress(s, f"{tag} · {m}", {**d, "group": n}), cancel)
        except Exception as exc:  # one group failing does not stop the others
            from .llm import Cancelled

            if isinstance(exc, Cancelled):
                raise
            record.update(status="failed", reasons=[str(exc)])
            progress("group_done", f"Group {tag}: failed ({exc})", {"group": n, "status": "failed"})
            continue
        record.update(explanation=out.explanation, questions=out.questions)
        selection, request = selection_for(group, rows), instruction(group, rows)
        reasons = verify(project, out.proposal, group) if out.proposal else []
        if out.proposal is not None and not reasons:
            record["proposal_id"] = out.proposal.id
            try:
                rev = project.apply_proposal(out.proposal.id, automatic=True)
                record.update(status="fixed", revision=rev.id, reasons=[])
            except Exception as exc:  # e.g. the model changed meanwhile: leave it for review
                record.update(status="review", reasons=[f"could not apply automatically: {exc}"])
        else:
            progress("choices", f"{tag} · Checking the options", {"group": n})
            choices = _model_choices(project, head, group, out.choices, selection, request, run_id)
            if out.proposal is not None and all(r.startswith("it chooses ") for r in reasons):
                choices += _term_alternatives(project, out.proposal, group, run_id)
            offered = {o["proposal_id"] for c in choices for o in c["options"]}
            if choices:
                record.update(status="choice", choices=choices, reasons=reasons)
                if out.proposal is not None and out.proposal.id not in offered:
                    project.dismiss_proposal(out.proposal.id)
            elif out.proposal is not None:
                record.update(status="review", proposal_id=out.proposal.id, reasons=reasons)
            else:
                record.update(status="input", reasons=["the assistant needs input" if out.questions or out.choices
                                                       else "no change proposed"])
        progress("group_done", f"Group {tag}: {record['status']}", {"group": n, "status": record["status"],
                                                                   "reasons": record.get("reasons", [])})
    counts = {s: sum(g["status"] == s for g in outcome.groups)
              for s in ("fixed", "choice", "review", "input", "failed", "resolved")}
    outcome.explanation = ", ".join(f"{v} {k}" for k, v in counts.items() if v) or "nothing to fix"
    return outcome
