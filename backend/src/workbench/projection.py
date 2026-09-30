"""Read-only projections of a revision's RDF into domain-facing views.

Tables and the graph view are projections of the model graph, recomputed from RDF for
every revision; nothing here is independently editable. Projection is structural (it
follows 223P relations), so it works for models the app minted and for imported models.

Domain mapping (223P):

* **Equipment** - instances of ``s223:Equipment`` subclasses, excluding sensors. Sensors
  are equipment in 223P, but they are shown as a property of the point they observe.
* **Point** - an ``s223:Property``; a sensor ``s223:observes`` it. The property is what
  a SCADA/BMS point reports or commands.
* **Connection** - an ``s223:Connection`` (pipe, duct, wire) whose ``s223:cnx``
  connection points belong to equipment; direction comes from outlet -> inlet.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from rdflib import URIRef
from rdflib.namespace import RDF, RDFS

from .graph import ProjectGraph
from .vocabulary import QUDT, S223, Vocabulary, local_name

POINT_KINDS = {
    "measurement": S223.QuantifiableObservableProperty,
    "setpoint": S223.QuantifiableActuatableProperty,
    "status": S223.EnumeratedObservableProperty,
    "command": S223.EnumeratedActuatableProperty,
}
POINT_KIND_LABELS = {
    "measurement": "Measurement",
    "setpoint": "Setpoint / command value",
    "status": "Status",
    "command": "On/off or mode command",
    "alarm": "Alarm",
    "parameter": "Parameter",
    "other": "Other property",
}
OBSERVABLE_KINDS = {"measurement", "status"}
ACTUATABLE_KINDS = {"setpoint", "command"}


@dataclass
class TermRef:
    iri: str | None
    label: str

    @classmethod
    def of(cls, vocab: Vocabulary, iri) -> "TermRef | None":
        return None if iri is None else cls(str(iri), vocab.label(str(iri)))


@dataclass
class EntityRef:
    id: str
    label: str


@dataclass
class EquipmentRow:
    id: str
    iri: str
    label: str
    type: TermRef | None
    process: TermRef | None
    contained_in: EntityRef | None
    point_count: int
    locked: list[str]
    evidence: list[str]
    kind: str = "equipment"


@dataclass
class PointRow:
    id: str
    iri: str
    label: str
    point_kind: str
    point_kind_label: str
    quantity_kind: TermRef | None
    unit: TermRef | None
    unit_symbol: str
    equipment: EntityRef | None
    medium: TermRef | None
    substance: TermRef | None
    sensor_type: TermRef | None
    locked: list[str]
    evidence: list[str]
    point_type: TermRef | None = None  # Brick point class
    kind: str = "point"


@dataclass
class ConnectionRow:
    id: str
    iri: str
    label: str
    type: TermRef | None
    from_equipment: EntityRef | None
    to_equipment: EntityRef | None
    medium: TermRef | None
    directed: bool
    locked: list[str]
    evidence: list[str]
    kind: str = "connection"


@dataclass
class ModelView:
    equipment: list[EquipmentRow] = field(default_factory=list)
    points: list[PointRow] = field(default_factory=list)
    connections: list[ConnectionRow] = field(default_factory=list)
    # containment edges: (container id, contained id)
    containment: list[tuple[str, str]] = field(default_factory=list)

    def rows(self) -> dict[str, object]:
        return {r.id: r for r in [*self.equipment, *self.points, *self.connections]}

    def to_dict(self) -> dict:
        return asdict(self)


def _first(g, s, p):
    return next(iter(g.objects(s, p)), None)


def most_specific(vocab: Vocabulary, types: list[str], kind: str | None = None) -> str | None:
    """Pick the most specific of several asserted classes (ignores unknown terms)."""
    known = [t for t in types if vocab.term(t) and (kind is None or vocab.kind_of(t) == kind)]
    for t in known:
        if not any(o != t and vocab.is_a(o, t) for o in known):
            return t
    return known[0] if known else None


class Classifier:
    """Decides which nodes are equipment / points / connections / sensors."""

    def __init__(self, vocab: Vocabulary):
        self.vocab = vocab

    def types(self, pg: ProjectGraph, node) -> list[str]:
        return [str(t) for t in pg.model.objects(node, RDF.type) if isinstance(t, URIRef)]

    def kind(self, pg: ProjectGraph, node) -> str | None:
        kinds = {self.vocab.kind_of(t) for t in self.types(pg, node)}
        for k in ("sensor", "connection", "equipment", "property"):
            if k in kinds:
                return "point" if k == "property" else k
        return None

    def point_kind(self, pg: ProjectGraph, node) -> str:
        types = self.types(pg, node)
        for name, cls in POINT_KINDS.items():
            if any(self.vocab.is_a(t, str(cls)) for t in types):
                return name
        return "other"


def owner_of_port(pg: ProjectGraph, port) -> URIRef | None:
    owner = _first(pg.model, port, S223.isConnectionPointOf)
    if owner is None:
        owner = next(iter(pg.model.subjects(S223.hasConnectionPoint, port)), None)
    return owner


def point_owner(pg: ProjectGraph, point) -> URIRef | None:
    """Equipment a point belongs to: hasProperty/actuatedByProperty, else its sensor's location."""
    g = pg.model
    for pred in (S223.hasProperty, S223.actuatedByProperty):
        for s in g.subjects(pred, point):
            return s  # type: ignore[return-value]
    for sensor in g.subjects(S223.observes, point):
        loc = _first(g, sensor, S223.hasObservationLocation)
        if loc is None:
            continue
        port_owner = owner_of_port(pg, loc)
        return port_owner if port_owner is not None else loc
    return None


def sensors_of(pg: ProjectGraph, point) -> list[URIRef]:
    return [s for s in pg.model.subjects(S223.observes, point) if isinstance(s, URIRef)]


def connection_ends(pg: ProjectGraph, cx) -> tuple[list[tuple[URIRef, URIRef | None, str]], bool]:
    """[(port, owner, direction)] for each cnx port of a connection."""
    ends = []
    ports = set(pg.model.objects(cx, S223.cnx)) | set(pg.model.subjects(S223.connectsThrough, cx))
    for port in sorted(ports, key=str):
        types = {str(t) for t in pg.model.objects(port, RDF.type)}
        direction = ("out" if str(S223.OutletConnectionPoint) in types
                     else "in" if str(S223.InletConnectionPoint) in types else "bi")
        ends.append((port, owner_of_port(pg, port), direction))
    directed = any(d == "out" for *_, d in ends) and any(d == "in" for *_, d in ends)
    return ends, directed


def entity_kind(pg: ProjectGraph, vocab: Vocabulary, eid: str) -> str | None:
    """equipment / point / connection for an existing entity id, else None."""
    if vocab.family == "brick":
        from . import brick

        return brick.entity_kind(pg, vocab, eid)
    node = pg.iri(eid)
    if node is None or (node, None, None) not in pg.model:
        return None
    return Classifier(vocab).kind(pg, node)


def project(pg: ProjectGraph, vocab: Vocabulary) -> ModelView:
    if vocab.family == "brick":
        from . import brick

        return brick.project(pg, vocab)
    g = pg.model
    cls = Classifier(vocab)
    view = ModelView()

    def ref(node) -> EntityRef | None:
        if node is None:
            return None
        eid = pg.id_of(node)
        if eid is None:
            return None
        return EntityRef(eid, str(_first(g, node, RDFS.label) or local_name(node)))

    def label(node) -> str:
        return str(_first(g, node, RDFS.label) or local_name(node))

    point_counts: dict[str, int] = {}
    for eid in pg.entity_ids():
        node = pg.iri(eid)
        if node is None or (node, None, None) not in g:
            continue
        kind = cls.kind(pg, node)
        common = dict(id=eid, iri=str(node), label=label(node),
                      locked=sorted(pg.locked_fields(node)), evidence=pg.evidence(node))
        if kind == "equipment":
            parent = next(iter(g.subjects(S223.contains, node)), None)
            watr = vocab.namespaces.get("watr")
            proc = _first(g, node, URIRef(watr + "hasProcess")) if watr else None
            view.equipment.append(EquipmentRow(
                **common,
                type=TermRef.of(vocab, most_specific(vocab, cls.types(pg, node), "equipment")),
                process=TermRef.of(vocab, proc),
                contained_in=ref(parent),
                point_count=0,
            ))
            if parent is not None and pg.id_of(parent):
                view.containment.append((pg.id_of(parent), eid))  # type: ignore[arg-type]
        elif kind == "point":
            pk = cls.point_kind(pg, node)
            owner = point_owner(pg, node)
            owner_ref = ref(owner)
            if owner_ref:
                point_counts[owner_ref.id] = point_counts.get(owner_ref.id, 0) + 1
            unit = _first(g, node, QUDT.hasUnit)
            sensor = next(iter(sensors_of(pg, node)), None)
            sensor_type = (most_specific(vocab, cls.types(pg, sensor), "sensor")
                           if sensor is not None else None)
            view.points.append(PointRow(
                **common,
                point_kind=pk,
                point_kind_label=POINT_KIND_LABELS[pk],
                quantity_kind=TermRef.of(vocab, _first(g, node, QUDT.hasQuantityKind)),
                unit=TermRef.of(vocab, unit),
                unit_symbol=(vocab.term(str(unit)).symbol if unit is not None and vocab.term(str(unit)) else ""),
                equipment=owner_ref,
                medium=TermRef.of(vocab, _first(g, node, S223.ofMedium)),
                substance=TermRef.of(vocab, _first(g, node, S223.ofSubstance)),
                sensor_type=TermRef.of(vocab, sensor_type),
            ))
        elif kind == "connection":
            ends, directed = connection_ends(pg, node)
            outs = [o for _, o, d in ends if d == "out"] or [o for _, o, d in ends if d == "bi"]
            ins = [o for _, o, d in ends if d == "in"] or [o for _, o, d in ends if d == "bi"][1:]
            if not directed and len(ends) >= 2:
                outs, ins = [ends[0][1]], [ends[1][1]]
            view.connections.append(ConnectionRow(
                **common,
                type=TermRef.of(vocab, most_specific(vocab, cls.types(pg, node), "connection")),
                from_equipment=ref(outs[0]) if outs else None,
                to_equipment=ref(ins[0]) if ins else None,
                medium=TermRef.of(vocab, _first(g, node, S223.hasMedium)),
                directed=directed,
            ))
    for row in view.equipment:
        row.point_count = point_counts.get(row.id, 0)
    view.equipment.sort(key=lambda r: r.label.lower())
    view.points.sort(key=lambda r: r.label.lower())
    view.connections.sort(key=lambda r: r.label.lower())
    return view


def ensure_ids(pg: ProjectGraph, vocab: Vocabulary) -> int:
    """Assign stable ids to top-level entities in an imported graph. Returns count added."""
    from .graph import new_id

    if vocab.family == "brick":
        from . import brick

        return brick.ensure_ids(pg, vocab)

    cls = Classifier(vocab)
    added = 0
    subjects = {s for s in pg.model.subjects(RDF.type, None) if isinstance(s, URIRef)}
    for node in sorted(subjects, key=str):
        if pg.id_of(node) is not None:
            continue
        kind = cls.kind(pg, node)
        if kind in ("equipment", "point", "connection"):
            pg.register(new_id(kind), node)
            added += 1
    return added
