"""Translate validation findings into domain-language review issues.

A finding's focus node is often a sub-node the user never sees (a connection point, a
sensor). Issues are attached to the top-level entity the user works with: a port's
equipment, a sensor's point. The raw SHACL detail stays in ``details`` for the
inspector.
"""

from __future__ import annotations

import hashlib
import re

from rdflib import URIRef
from rdflib.namespace import RDF, RDFS

from .graph import ProjectGraph
from .projection import owner_of_port
from .schemas import ReviewIssue, ValidationSummary
from .vocabulary import S223, ValidationRun, Vocabulary, local_name

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


def _label(pg: ProjectGraph, vocab: Vocabulary, eid: str | None, focus: str) -> str:
    node = pg.iri(eid) if eid else URIRef(focus)
    lbl = next(iter(pg.model.objects(node, RDFS.label)), None)
    return str(lbl) if lbl is not None else local_name(focus)


def _explain(pg: ProjectGraph, vocab: Vocabulary, f, eid: str | None) -> tuple[str, str]:
    """(category, explanation) for one finding."""
    who = _label(pg, vocab, eid, f.focus)
    path = local_name(f.path) if f.path else ""
    msg = f.message or ""

    m = re.match(r"Instances of (\w+) must have the ([\w ]+) process", msg)
    if m:
        return "missing_information", (
            f"{who} is typed as {vocab.label(f.shape) if f.shape else humanize_camel(m.group(1))}, "
            f"which requires the {humanize_camel(m.group(2))} treatment process.")
    if re.search(r"probably needs an association with a `?Connection", msg):
        types = {local_name(t) for t in pg.model.objects(URIRef(f.focus), RDF.type)}
        port_kind = ("inlet" if "InletConnectionPoint" in types
                     else "outlet" if "OutletConnectionPoint" in types else "connection point")
        return "topology", f"{who} has an unconnected {port_kind}."
    if path == "hasConnectionPoint":
        side = ("an inlet" if "InletConnectionPoint" in msg
                else "an outlet" if "OutletConnectionPoint" in msg else "a connection")
        needs = " carrying a fluid" if "Fluid" in msg else ""
        return "topology", f"{who} needs {side}{needs} — connect it to upstream/downstream equipment."
    if path == "hasObservationLocation" or (f.shape or "").endswith("SensorObservationLocationShape"):
        return "unassigned", f"{who} isn't associated with any equipment, so its sensor has no location."
    if path == "hasEnumerationKind":
        return "missing_information", f"{who} needs a value set (for example on/off or run status)."
    if path in ("hasUnit", "unit"):
        return "missing_information", f"{who} has no unit."
    if path == "hasQuantityKind":
        return "missing_information", f"{who} doesn't say what it measures (quantity kind)."
    if path == "hasMedium":
        return "missing_information", f"{who} doesn't say what medium it carries."
    if path == "observes":
        return "missing_information", f"{who} has a sensor that doesn't observe exactly one point."
    compact = re.sub(r"<([^>]+)>", lambda mm: vocab.label(mm.group(1)), msg)
    compact = re.sub(r"\s+", " ", compact)[:240]
    return "other", f"{who}: {compact}"


def humanize_camel(s: str) -> str:
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)


def issues_from_validation(pg: ProjectGraph, vocab: Vocabulary, run: ValidationRun) -> list[ReviewIssue]:
    seen: dict[str, ReviewIssue] = {}
    for f in run.findings:
        eid = owning_entity(pg, f.focus)
        category, explanation = _explain(pg, vocab, f, eid)
        key = hashlib.sha1(f"{eid}|{explanation}".encode()).hexdigest()[:12]
        if key in seen:
            seen[key].details.setdefault("findings", []).append(_detail(f))
            continue
        seen[key] = ReviewIssue(
            id=f"val-{key}",
            affected_ids=[eid] if eid else [],
            category=category,  # type: ignore[arg-type]
            severity=SEVERITY.get(f.severity, "warning"),  # type: ignore[arg-type]
            explanation=explanation,
            origin="validation",
            details={"findings": [_detail(f)]},
        )
    order = {"violation": 0, "warning": 1, "suggestion": 2}
    return sorted(seen.values(), key=lambda i: (order[i.severity], i.explanation))


def _detail(f) -> dict:
    return {"focus": f.focus, "shape": f.shape, "path": f.path, "value": f.value,
            "message": f.message, "severity": f.severity}


def summarize(issues: list[ReviewIssue], run: ValidationRun) -> ValidationSummary:
    return ValidationSummary(
        conforms=run.conforms,
        violations=sum(i.severity == "violation" for i in issues),
        warnings=sum(i.severity == "warning" for i in issues),
        suggestions=sum(i.severity == "suggestion" for i in issues),
        duration_s=round(run.duration_s, 3),
    )
