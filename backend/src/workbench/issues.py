"""Translate validation findings into domain-language review issues.

A finding's focus node is often a sub-node the user never sees (a connection point, a
sensor). Issues are attached to the top-level entity the user works with: a port's
equipment, a sensor's point. The text is the validator's own message; only IRIs are shown
as labels or prefixed names. The raw SHACL detail stays in ``details`` for the inspector.
"""

from __future__ import annotations

import hashlib
import json
import re

from rdflib import URIRef
from rdflib.namespace import RDFS

from .graph import ProjectGraph
from .projection import is_port, owner_of_port, port_id, port_label
from .schemas import ReviewIssue, ValidationSummary
from .vocabulary import S223, ValidationRun, Vocabulary

SEVERITY = {"Violation": "violation", "Warning": "warning", "Info": "suggestion"}


def owning_entity(pg: ProjectGraph, focus: str) -> str | None:
    node = URIRef(focus)
    eid = pg.id_of(node)
    if eid:
        return eid
    g = pg.model
    for p in g.objects(node, S223.observes):  # sensor -> its point
        if pg.id_of(p):
            return pg.id_of(p)
    owner = owner_of_port(pg, node)  # port -> equipment
    if owner is not None and pg.id_of(owner):
        return pg.id_of(owner)
    for cx in g.subjects(S223.cnx, node):
        if pg.id_of(cx):
            return pg.id_of(cx)
    if "." in focus:  # app-minted sub-node: <ns><entity-id>.<part>
        head = focus.rsplit(".", 1)[0]
        return pg.id_of(URIRef(head))
    return None


def _label(pg: ProjectGraph, node: str) -> str | None:
    lbl = next(iter(pg.model.objects(URIRef(node), RDFS.label)), None)
    return str(lbl) if lbl is not None else None


def render_iri(pg: ProjectGraph, vocab: Vocabulary, iri: str) -> str:
    """A model node by its own label (a connection point by its table label), a vocabulary
    term as a prefixed name, otherwise the IRI."""
    if _label(pg, iri) is None and iri.startswith(("http", "urn")) and is_port(pg, URIRef(iri)):
        return port_label(pg, vocab, URIRef(iri))
    return _label(pg, iri) or vocab.curie(iri)


def _render(pg: ProjectGraph, vocab: Vocabulary, eid: str | None, d: dict) -> str:
    """The validator's message, with IRIs shown as labels or prefixed names; nothing added."""
    msg = re.sub(r"<([^<>\s]+)>", lambda m: render_iri(pg, vocab, m.group(1)), d.get("message") or "")
    msg = re.sub(r"\s+", " ", msg).strip()
    who = (_label(pg, str(pg.iri(eid))) if eid else None) or render_iri(pg, vocab, d["focus"])
    path = f" ({vocab.curie(d['path'])})" if d.get("path") else ""
    return f"{who}{path}: {msg}"


def finding_key(eid: str | None, d: dict) -> str:
    """Identity of a validation issue, from the validator's output only (not our rendering)."""
    raw = json.dumps([eid, d.get("shape"), d.get("path"), d.get("message")])
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def issues_from_validation(pg: ProjectGraph, vocab: Vocabulary, run: ValidationRun) -> list[ReviewIssue]:
    return group_findings(pg, vocab, [(owning_entity(pg, f.focus), _detail(f)) for f in run.findings])


def group_findings(pg: ProjectGraph, vocab: Vocabulary,
                   findings: list[tuple[str | None, dict]]) -> list[ReviewIssue]:
    """One issue per entity and distinct validator message; repeated findings are listed under it."""
    seen: dict[str, ReviewIssue] = {}
    for eid, d in findings:
        key = finding_key(eid, d)
        ports = [port_id(pg, URIRef(n)) for n in (d.get("focus"), d.get("value"))
                 if n and n.startswith(("http", "urn")) and is_port(pg, URIRef(n))]
        if key in seen:
            seen[key].details["findings"].append(d)
            seen[key].affected_ids += [i for i in ports if i not in seen[key].affected_ids]
            continue
        seen[key] = ReviewIssue(
            id=f"val-{key}",
            # The owning entity first (its id is part of the issue id); then the connection points involved.
            affected_ids=([eid] if eid else []) + [i for i in dict.fromkeys(ports) if i != eid],
            category="validation",
            severity=SEVERITY.get(d.get("severity") or "", "warning"),  # type: ignore[arg-type]
            explanation=_render(pg, vocab, eid, d),
            origin="validation",
            details={"findings": [d]},
        )
    order = {"violation": 0, "warning": 1, "suggestion": 2}
    return sorted(seen.values(), key=lambda i: (order[i.severity], i.explanation))


def _detail(f) -> dict:
    return {"focus": f.focus, "shape": f.shape, "path": f.path, "value": f.value,
            "message": f.message, "severity": f.severity, "statement_id": f.statement_id}


def render_text(pg: ProjectGraph, vocab: Vocabulary, text: str) -> str:
    return re.sub(r"<([^<>\s]+)>", lambda m: render_iri(pg, vocab, m.group(1)), text or "")


def render_repair(pg: ProjectGraph, vocab: Vocabulary, w: dict) -> dict:
    """A repair witness for display: the engine's own words, IRIs shown as labels or prefixed names."""
    show = lambda text: render_text(pg, vocab, str(text))  # noqa: E731
    summary = []
    for a in w["atoms"]:
        line = show(f"{a['path']}: {a['detail']}" if a.get("path") else a["detail"])
        if line not in summary:
            summary.append(line)
    missing = []
    for o in w["missing"]:
        line = show(f"<{o['node']}> {o['path']}: have {o['observed']}, need {o['required']}")
        if line not in missing:
            missing.append(line)
    return {
        "blocked": w["blocked"],
        # Only opaque (SPARQL) constraints failed: the engine can say nothing about them.
        "opaque": bool(w["atoms"]) and all(str(a["kind"]).endswith("Opaque") for a in w["atoms"]),
        "summary": summary,
        "missing": missing,
        "offending": [show(v) for v in dict.fromkeys(w["offending"])],
        "repair": show(w["repair"]),
        "shape": vocab.curie(w["shape"]) if w.get("shape") else None,
    }


def recount(stored: list[dict], summary: ValidationSummary) -> ValidationSummary:
    """A revision's stored summary with its counts regrouped the way issues are read now,
    so revisions validated under an older grouping report the issues the list shows."""
    severity: dict[str, str] = {}
    for issue in stored:
        eid = issue["affected_ids"][0] if issue.get("affected_ids") else None
        for d in issue.get("details", {}).get("findings", []):
            severity.setdefault(finding_key(eid, d), SEVERITY.get(d.get("severity") or "", "warning"))
    values = list(severity.values())
    return summary.model_copy(update={"violations": values.count("violation"), "warnings": values.count("warning"),
                                      "suggestions": values.count("suggestion")})


def summarize(issues: list[ReviewIssue], run: ValidationRun) -> ValidationSummary:
    return ValidationSummary(
        conforms=run.conforms,
        violations=sum(i.severity == "violation" for i in issues),
        warnings=sum(i.severity == "warning" for i in issues),
        suggestions=sum(i.severity == "suggestion" for i in issues),
        duration_s=round(run.duration_s, 3),
    )
