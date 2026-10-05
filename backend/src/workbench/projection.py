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
* **Space** - an ``s223:PhysicalSpace``; spaces nest with ``parent s223:contains child`` and
  equipment is placed with ``s223:hasPhysicalLocation``.
* **Connection point** - an inlet, outlet or bidirectional ``s223:ConnectionPoint`` of a
  piece of equipment, with its medium, the connection that joins it, the point it is paired
  with (``s223:pairedConnectionPoint``) and the container's point it maps to (``s223:mapsTo``).
"""

from __future__ import annotations

import hashlib
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
PORT_CLASSES = {
    "inlet": S223.InletConnectionPoint,
    "outlet": S223.OutletConnectionPoint,
    "bidirectional": S223.BidirectionalConnectionPoint,
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
    location: EntityRef | None = None  # the space the equipment is in
    kind: str = "equipment"


@dataclass
class SpaceRow:
    id: str
    iri: str
    label: str
    type: TermRef | None
    part_of: EntityRef | None
    equipment_count: int
    locked: list[str]
    evidence: list[str]
    kind: str = "space"


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
    from_point: EntityRef | None = None
    to_point: EntityRef | None = None
    kind: str = "connection"


@dataclass
class ConnectionPointRow:
    id: str
    iri: str
    label: str
    equipment: EntityRef | None
    direction: str  # inlet / outlet / bidirectional
    medium: TermRef | None
    connection: EntityRef | None
    paired_with: EntityRef | None
    maps_to: EntityRef | None  # the containing equipment's connection point
    mapped_from: EntityRef | None  # a contained equipment's connection point mapping to this one
    locked: list[str]
    evidence: list[str]
    kind: str = "connection_point"


@dataclass
class ModelView:
    equipment: list[EquipmentRow] = field(default_factory=list)
    points: list[PointRow] = field(default_factory=list)
    connections: list[ConnectionRow] = field(default_factory=list)
    connection_points: list[ConnectionPointRow] = field(default_factory=list)
    spaces: list[SpaceRow] = field(default_factory=list)
    # containment edges: (container id, contained id)
    containment: list[tuple[str, str]] = field(default_factory=list)

    def rows(self) -> dict[str, object]:
        return {r.id: r for r in [*self.equipment, *self.points, *self.connections, *self.connection_points,
                                  *self.spaces]}

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
        for k in ("sensor", "connection", "location", "equipment", "property"):
            if k in kinds:
                return {"property": "point", "location": "space"}.get(k, k)
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


def ports(pg: ProjectGraph) -> set[URIRef]:
    """Every connection point in the model, however it is attached."""
    g = pg.model
    found = {o for o in g.objects(None, S223.hasConnectionPoint)}
    found |= {s for s in g.subjects(S223.isConnectionPointOf, None)}
    for cls in PORT_CLASSES.values():
        found |= set(g.subjects(RDF.type, cls))
    return {p for p in found if isinstance(p, URIRef)}


def is_port(pg: ProjectGraph, node) -> bool:
    g = pg.model
    return ((None, S223.hasConnectionPoint, node) in g or (node, S223.isConnectionPointOf, None) in g
            or any((node, RDF.type, cls) in g for cls in PORT_CLASSES.values()))


def port_id(pg: ProjectGraph, port) -> str:
    """A connection point's stable id. Ports minted before connection points had ids (and
    imported ones) are not registered; their id is derived from the IRI, so it is the same
    in every revision, and an operation that touches the port registers it under that id."""
    return pg.id_of(port) or "cp-" + hashlib.sha1(str(port).encode()).hexdigest()[:6]


def port_iri(pg: ProjectGraph, cp_id: str) -> URIRef | None:
    iri = pg.iri(cp_id)
    if iri is not None:
        return iri
    if not cp_id.startswith("cp-"):
        return None
    return next((p for p in ports(pg) if port_id(pg, p) == cp_id), None)


def entity_iri(pg: ProjectGraph, eid: str) -> URIRef | None:
    """The node for any entity id, including a connection point's derived id."""
    return pg.iri(eid) or (port_iri(pg, eid) if eid.startswith("cp-") else None)


def port_direction(pg: ProjectGraph, port) -> str:
    types = set(pg.model.objects(port, RDF.type))
    return next((d for d, cls in PORT_CLASSES.items() if cls in types), "bidirectional")


def port_connections(pg: ProjectGraph, port) -> list[URIRef]:
    g = pg.model
    found = set(g.subjects(S223.cnx, port)) | set(g.objects(port, S223.connectsThrough))
    return sorted((c for c in found if isinstance(c, URIRef) and not is_port(pg, c)), key=str)


def paired_port(pg: ProjectGraph, port) -> URIRef | None:
    g = pg.model
    return _first(g, port, S223.pairedConnectionPoint) or next(iter(g.subjects(S223.pairedConnectionPoint, port)), None)


def port_label(pg: ProjectGraph, vocab: Vocabulary, port) -> str:
    """Its own label, else "<equipment> inlet from <upstream> (water)" and the like."""
    g = pg.model
    own = _first(g, port, RDFS.label)
    if own is not None:
        return str(own)

    def name(node) -> str:
        return str(_first(g, node, RDFS.label) or local_name(node)) if node is not None else "unattached"

    direction = port_direction(pg, port)
    text = f"{name(owner_of_port(pg, port))} {direction}"
    cx = next(iter(port_connections(pg, port)), None)
    if cx is not None:
        far = [o for p, o, _ in connection_ends(pg, cx)[0] if p != port]
        if far:
            text += f" {'from' if direction == 'inlet' else 'to' if direction == 'outlet' else 'with'} {name(far[0])}"
    medium = _first(g, port, S223.hasMedium)
    return text + (f" ({vocab.label(str(medium)).lower()})" if medium is not None else "")


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
    node = entity_iri(pg, eid)
    if node is None or ((node, None, None) not in pg.model and (None, None, node) not in pg.model):
        return None
    if is_port(pg, node):
        return "connection_point"
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

    def port_ref(port) -> EntityRef | None:
        return None if port is None else EntityRef(port_id(pg, port), port_label(pg, vocab, port))

    point_counts: dict[str, int] = {}
    located: dict[str, int] = {}  # space id -> equipment placed in it
    for eid in pg.entity_ids():
        node = pg.iri(eid)
        if node is None or (node, None, None) not in g:
            continue
        kind = cls.kind(pg, node)
        common = dict(id=eid, iri=str(node), label=label(node),
                      locked=sorted(pg.locked_fields(node)), evidence=pg.evidence(node))
        if kind == "equipment":
            parent = next((p for p in g.subjects(S223.contains, node) if cls.kind(pg, p) == "equipment"), None)
            watr = vocab.namespaces.get("watr")
            proc = _first(g, node, URIRef(watr + "hasProcess")) if watr else None
            place = _first(g, node, S223.hasPhysicalLocation)
            view.equipment.append(EquipmentRow(
                **common,
                type=TermRef.of(vocab, most_specific(vocab, cls.types(pg, node), "equipment")),
                process=TermRef.of(vocab, proc),
                contained_in=ref(parent),
                point_count=0,
                location=ref(place),
            ))
            if place is not None and pg.id_of(place):
                located[pg.id_of(place)] = located.get(pg.id_of(place), 0) + 1  # type: ignore[index]
            if parent is not None and pg.id_of(parent):
                view.containment.append((pg.id_of(parent), eid))  # type: ignore[arg-type]
        elif kind == "space":
            parent = next((p for p in g.subjects(S223.contains, node) if cls.kind(pg, p) == "space"), None)
            view.spaces.append(SpaceRow(
                **common, type=TermRef.of(vocab, most_specific(vocab, cls.types(pg, node), "location")),
                part_of=ref(parent), equipment_count=0))
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
            out_ports = [p for p, _, d in ends if d == "out"] or [p for p, _, d in ends if d == "bi"]
            in_ports = [p for p, _, d in ends if d == "in"] or [p for p, _, d in ends if d == "bi"][1:]
            if not directed and len(ends) >= 2:
                out_ports, in_ports = [ends[0][0]], [ends[1][0]]
            view.connections.append(ConnectionRow(
                **common,
                type=TermRef.of(vocab, most_specific(vocab, cls.types(pg, node), "connection")),
                from_equipment=ref(outs[0]) if outs else None,
                to_equipment=ref(ins[0]) if ins else None,
                medium=TermRef.of(vocab, _first(g, node, S223.hasMedium)),
                directed=directed,
                from_point=port_ref(out_ports[0]) if out_ports else None,
                to_point=port_ref(in_ports[0]) if in_ports else None,
            ))
    for port in ports(pg):
        cx = next(iter(port_connections(pg, port)), None)
        maps_to = _first(g, port, S223.mapsTo)
        view.connection_points.append(ConnectionPointRow(
            id=port_id(pg, port), iri=str(port), label=port_label(pg, vocab, port),
            equipment=ref(owner_of_port(pg, port)),
            direction=port_direction(pg, port),
            medium=TermRef.of(vocab, _first(g, port, S223.hasMedium)),
            connection=ref(cx) if cx is not None else None,
            paired_with=port_ref(paired_port(pg, port)),
            maps_to=port_ref(maps_to) if isinstance(maps_to, URIRef) else None,
            mapped_from=port_ref(next(iter(g.subjects(S223.mapsTo, port)), None)),
            locked=sorted(pg.locked_fields(port)), evidence=pg.evidence(port),
        ))
    for row in view.equipment:
        row.point_count = point_counts.get(row.id, 0)
    for space in view.spaces:
        space.equipment_count = located.get(space.id, 0)
    view.spaces.sort(key=lambda r: r.label.lower())
    view.equipment.sort(key=lambda r: r.label.lower())
    view.points.sort(key=lambda r: r.label.lower())
    view.connections.sort(key=lambda r: r.label.lower())
    view.connection_points.sort(key=lambda r: ((r.equipment.label.lower() if r.equipment else "~"), r.label.lower()))
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
        if kind in ("equipment", "point", "connection", "space"):
            pg.register(new_id(kind), node)
            added += 1
    return added
