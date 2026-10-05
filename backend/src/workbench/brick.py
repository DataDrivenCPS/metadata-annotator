"""The Brick model family: projection and operation compilation.

Domain mapping (per the BuildingMOTIF skill's point-label and Brick references):

* **Equipment** - an instance of a ``brick:Equipment`` subclass; containment is
  ``parent brick:hasPart child``.
* **Point** - an instance of a ``brick:Point`` subclass (the point type, e.g.
  ``brick:Zone_Air_Temperature_Sensor``); ``equipment brick:hasPoint point``; optional
  ``brick:hasUnit`` (QUDT). The point kind (measurement, setpoint, ...) follows from the class.
* **Connection** - ``upstream brick:feeds downstream``. A feeds edge is a triple, not a node,
  so its stable id lives in the annotation graph: ``<ns cx-id> wb:source s; wb:target o;
  wb:predicate brick:feeds``. The exported model contains only the Brick triple.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import RDF, RDFS

from .graph import WB, ProjectGraph, new_id
from .vocabulary import BRICK, Vocabulary, local_name

if TYPE_CHECKING:
    from .operations import ApplyResult
    from .projection import ModelView

POINT_KIND_CLASSES = {
    "measurement": BRICK.Sensor,
    "setpoint": BRICK.Setpoint,
    "command": BRICK.Command,
    "status": BRICK.Status,
    "alarm": BRICK.Alarm,
    "parameter": BRICK.Parameter,
}
EDGE_PREDICATES = (BRICK.feeds,)
KIND_LABELS = {"measurement": "Sensor", "setpoint": "Setpoint", "command": "Command", "status": "Status",
               "alarm": "Alarm", "parameter": "Parameter", "other": "Other point"}


def point_kind(vocab: Vocabulary, point_type: str | None) -> str:
    if point_type:
        for kind, cls in POINT_KIND_CLASSES.items():
            if vocab.is_a(point_type, str(cls)):
                return kind
    return "other"


def types(pg: ProjectGraph, node) -> list[str]:
    return [str(t) for t in pg.model.objects(node, RDF.type) if isinstance(t, URIRef)]


def node_kind(pg: ProjectGraph, vocab: Vocabulary, node) -> str | None:
    kinds = {vocab.kind_of(t) for t in types(pg, node)}
    if "point_class" in kinds:
        return "point"
    if "equipment" in kinds:
        return "equipment"
    return None


# ------------------------------------------------------------------ edges

def edge(pg: ProjectGraph, cid: str) -> tuple[URIRef, URIRef, URIRef] | None:
    """(source, predicate, target) for a registered connection id, if its triple exists."""
    node = pg.iri(cid)
    if node is None:
        return None
    s, p, o = (pg.ann.value(node, WB.source), pg.ann.value(node, WB.predicate), pg.ann.value(node, WB.target))
    if s is None or p is None or o is None or (s, p, o) not in pg.model:
        return None
    return s, p, o  # type: ignore[return-value]


def register_edge(pg: ProjectGraph, cid: str, s, p, o, label: str | None = None) -> None:
    node = pg.register(cid)
    pg.ann.set((node, WB.source, s))
    pg.ann.set((node, WB.predicate, p))
    pg.ann.set((node, WB.target, o))
    if label:
        pg.ann.set((node, RDFS.label, RDFLiteral(label)))


def edge_ids(pg: ProjectGraph) -> dict[tuple, str]:
    out = {}
    for node in pg.ann.subjects(WB.predicate, None):
        cid = pg.id_of(node)  # type: ignore[arg-type]
        s, p, o = pg.ann.value(node, WB.source), pg.ann.value(node, WB.predicate), pg.ann.value(node, WB.target)
        if cid and s is not None:
            out[(s, p, o)] = cid
    return out


def entity_kind(pg: ProjectGraph, vocab: Vocabulary, eid: str) -> str | None:
    node = pg.iri(eid)
    if node is None:
        return None
    if pg.ann.value(node, WB.predicate) is not None:
        return "connection" if edge(pg, eid) else None
    if (node, None, None) not in pg.model:
        return None
    return node_kind(pg, vocab, node)


def ensure_ids(pg: ProjectGraph, vocab: Vocabulary) -> int:
    added = 0
    for node in sorted({s for s in pg.model.subjects(RDF.type, None) if isinstance(s, URIRef)}, key=str):
        if pg.id_of(node) is None:
            kind = node_kind(pg, vocab, node)
            if kind:
                pg.register(new_id(kind), node)
                added += 1
    known = edge_ids(pg)
    for p in EDGE_PREDICATES:
        for s, o in sorted(pg.model.subject_objects(p), key=str):
            if (s, p, o) not in known and pg.id_of(s) and pg.id_of(o):  # type: ignore[arg-type]
                register_edge(pg, new_id("connection"), s, p, o)
                added += 1
    return added


# ------------------------------------------------------------- projection

def point_owner(pg: ProjectGraph, point) -> URIRef | None:
    owner = next(iter(pg.model.subjects(BRICK.hasPoint, point)), None)
    return owner if owner is not None else pg.model.value(point, BRICK.isPointOf)  # type: ignore[return-value]


def project(pg: ProjectGraph, vocab: Vocabulary) -> "ModelView":
    from .projection import (
        ConnectionRow, EntityRef, EquipmentRow, ModelView, PointRow, TermRef, most_specific,
    )

    g = pg.model
    view = ModelView()

    def label(node) -> str:
        return str(g.value(node, RDFS.label) or local_name(node))

    def ref(node) -> EntityRef | None:
        eid = pg.id_of(node) if node is not None else None
        return EntityRef(eid, label(node)) if eid else None

    counts: dict[str, int] = {}
    for eid in pg.entity_ids():
        node = pg.iri(eid)
        if node is None:
            continue
        common = dict(id=eid, iri=str(node), locked=sorted(pg.locked_fields(node)), evidence=pg.evidence(node))
        if pg.ann.value(node, WB.predicate) is not None:
            e = edge(pg, eid)
            if e is None:
                continue
            s, p, o = e
            view.connections.append(ConnectionRow(
                **common, label=str(pg.ann.value(node, RDFS.label) or f"{label(s)} → {label(o)}"),
                type=TermRef(str(p), "feeds"), from_equipment=ref(s), to_equipment=ref(o),
                medium=None, directed=True))
            continue
        if (node, None, None) not in g:
            continue
        kind = node_kind(pg, vocab, node)
        if kind == "equipment":
            parent = next(iter(g.subjects(BRICK.hasPart, node)), None) or g.value(node, BRICK.isPartOf)
            parent_ref = ref(parent) if parent is not None and node_kind(pg, vocab, parent) == "equipment" else None
            view.equipment.append(EquipmentRow(
                **common, label=label(node), type=TermRef.of(vocab, most_specific(vocab, types(pg, node), "equipment")),
                process=None, contained_in=parent_ref, point_count=0))
            if parent_ref:
                view.containment.append((parent_ref.id, eid))
        elif kind == "point":
            ptype = most_specific(vocab, types(pg, node), "point_class")
            pk = point_kind(vocab, ptype)
            owner = ref(point_owner(pg, node))
            if owner:
                counts[owner.id] = counts.get(owner.id, 0) + 1
            unit = g.value(node, BRICK.hasUnit)
            t = vocab.term(str(unit)) if unit is not None else None
            view.points.append(PointRow(
                **common, label=label(node), point_kind=pk, point_kind_label=KIND_LABELS.get(pk, pk.title()),
                quantity_kind=None, unit=TermRef.of(vocab, unit), unit_symbol=t.symbol if t else "",
                equipment=owner, medium=None, substance=None, sensor_type=None,
                point_type=TermRef.of(vocab, ptype)))
    for row in view.equipment:
        row.point_count = counts.get(row.id, 0)
    view.equipment.sort(key=lambda r: r.label.lower())
    view.points.sort(key=lambda r: r.label.lower())
    view.connections.sort(key=lambda r: r.label.lower())
    return view


# -------------------------------------------------------------- compiling

class BrickCompiler:
    def __init__(self, pg: ProjectGraph, vocab: Vocabulary, result: "ApplyResult"):
        self.pg, self.vocab, self.r = pg, vocab, result
        self.g = pg.model

    def node(self, eid: str) -> URIRef:
        n = self.pg.iri(eid)
        if n is None:
            from .operations import OperationError

            raise OperationError([f"{eid} no longer exists when this operation runs "
                                  "(an earlier operation in the proposal removed it)"])
        return n

    def _label(self, n) -> str:
        return str(self.g.value(n, RDFS.label) or local_name(n))

    def set_one(self, s, p, value) -> None:
        self.g.remove((s, p, None))
        if value is not None:
            self.g.add((s, p, value if isinstance(value, RDFLiteral) else URIRef(value)))

    def set_type(self, n, iri: str, kind: str) -> None:
        for t in list(self.g.objects(n, RDF.type)):
            if self.vocab.kind_of(str(t)) == kind:
                self.g.remove((n, RDF.type, t))
        self.g.add((n, RDF.type, URIRef(iri)))

    def _set_parent(self, n, parent_id: str | None) -> None:
        self.g.remove((None, BRICK.hasPart, n))
        self.g.remove((n, BRICK.isPartOf, None))
        if parent_id:
            self.g.add((self.node(parent_id), BRICK.hasPart, n))

    def _set_owner(self, p, equipment_id: str | None) -> None:
        self.g.remove((None, BRICK.hasPoint, p))
        self.g.remove((p, BRICK.isPointOf, None))
        if equipment_id:
            self.g.add((self.node(equipment_id), BRICK.hasPoint, p))

    def _drop_edges_of(self, n) -> None:
        for (s, p, o), cid in edge_ids(self.pg).items():
            if n in (s, o):
                self.g.remove((s, p, o))
                self.pg.unregister(cid)
                self.r.touch(cid, "deleted")

    # equipment
    def create_equipment(self, op) -> None:
        n = self.pg.register(op.id)
        self.g.add((n, RDF.type, URIRef(op.type)))
        self.g.add((n, RDFS.label, RDFLiteral(op.label)))
        if op.contained_in:
            self._set_parent(n, op.contained_in)
        self.pg.add_evidence(n, op.evidence or [])
        self.r.touch(op.id, "created", *op.provided())

    def update_equipment(self, op) -> None:
        n = self.node(op.id)
        fields = op.provided()
        if "label" in fields:
            self.set_one(n, RDFS.label, RDFLiteral(op.label or ""))
        if "type" in fields and op.type:
            self.set_type(n, op.type, "equipment")
        if "contained_in" in fields:
            self._set_parent(n, op.contained_in)
        self.r.touch(op.id, *fields)

    def delete_equipment(self, op) -> None:
        n = self.node(op.id)
        orphaned = [p for p in self.g.objects(n, BRICK.hasPoint)] + [p for p in self.g.subjects(BRICK.isPointOf, n)]
        for p in orphaned:
            if self.pg.id_of(p):
                self.r.touch(self.pg.id_of(p), "equipment")  # type: ignore[arg-type]
        self._drop_edges_of(n)
        self.g.remove((n, None, None))
        self.g.remove((None, None, n))
        self.pg.unregister(op.id)
        if orphaned:
            self.r.notes.append(f"{len(orphaned)} point(s) are no longer assigned to equipment")
        self.r.touch(op.id, "deleted")

    # points
    def create_point(self, op) -> None:
        n = self.pg.register(op.id)
        cls = op.point_type or str(POINT_KIND_CLASSES.get(op.point_kind, BRICK.Point))
        self.g.add((n, RDF.type, URIRef(cls)))
        self.g.add((n, RDFS.label, RDFLiteral(op.label)))
        if op.unit:
            self.g.add((n, BRICK.hasUnit, URIRef(op.unit)))
        if op.equipment:
            self._set_owner(n, op.equipment)
        self.pg.add_evidence(n, op.evidence or [])
        self.r.touch(op.id, "created", *op.provided())

    def update_point(self, op) -> None:
        n = self.node(op.id)
        fields = op.provided()
        if "label" in fields:
            self.set_one(n, RDFS.label, RDFLiteral(op.label or ""))
        if "point_type" in fields and op.point_type:
            self.set_type(n, op.point_type, "point_class")
        elif "point_kind" in fields and op.point_kind:
            current = next((t for t in types(self.pg, n) if self.vocab.kind_of(t) == "point_class"), None)
            if point_kind(self.vocab, current) != op.point_kind:
                self.set_type(n, str(POINT_KIND_CLASSES[op.point_kind]), "point_class")
                self.r.notes.append(f"{self._label(n)}: set to the generic {op.point_kind} class; "
                                    "choose a specific point type if you know it")
        if "unit" in fields:
            self.set_one(n, BRICK.hasUnit, op.unit)
        if "equipment" in fields:
            self._set_owner(n, op.equipment)
        self.r.touch(op.id, *fields)

    def delete_point(self, op) -> None:
        n = self.node(op.id)
        self.g.remove((n, None, None))
        self.g.remove((None, None, n))
        self.pg.unregister(op.id)
        self.r.touch(op.id, "deleted")

    # connections (brick:feeds)
    def create_connection(self, op) -> None:
        s, o = self.node(op.from_equipment), self.node(op.to_equipment)
        self.g.add((s, BRICK.feeds, o))
        register_edge(self.pg, op.id, s, BRICK.feeds, o, op.label)
        self.pg.add_evidence(self.pg.iri(op.id), op.evidence or [])
        self.r.touch(op.id, "created", *op.provided())

    def update_connection(self, op) -> None:
        e = edge(self.pg, op.id)
        if e is None:  # its equipment was deleted earlier in the proposal
            from .operations import OperationError

            raise OperationError([f"{op.id} no longer exists when this operation runs "
                                  "(an earlier operation in the proposal removed it)"])
        s, p, o = e
        fields = op.provided()
        ns = self.node(op.from_equipment) if "from_equipment" in fields and op.from_equipment else s
        no = self.node(op.to_equipment) if "to_equipment" in fields and op.to_equipment else o
        self.g.remove((s, p, o))
        self.g.add((ns, p, no))
        node = self.pg.iri(op.id)
        self.pg.ann.set((node, WB.source, ns))
        self.pg.ann.set((node, WB.target, no))
        if "label" in fields:
            self.pg.ann.remove((node, RDFS.label, None))
            if op.label:
                self.pg.ann.add((node, RDFS.label, RDFLiteral(op.label)))
        self.r.touch(op.id, *fields)

    def delete_connection(self, op) -> None:
        e = edge(self.pg, op.id)
        if e is not None:
            self.g.remove(e)
        self.pg.unregister(op.id)
        self.r.touch(op.id, "deleted")
