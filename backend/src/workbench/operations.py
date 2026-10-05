"""Domain operations: the only way the model graph changes.

Direct cell edits, agent proposals, and (later) extraction all produce these typed
operations. ``resolve`` checks their structure against the current revision and the
vocabulary and replaces ``new:*`` placeholders with minted ids; ``apply`` compiles them
into 223P triples on a copy of the graph. A resolved operation list applies
deterministically, so the graph change shown in a proposal preview is exactly the change
made when it is applied.

Operations speak domain terms (equipment, point, connection, unit, ...) and compile per
model family: Brick (brick.py) or 223P/WaTr (below). The 223P patterns follow the
BuildingMOTIF skill's 223P/WaTr references:
connection points with media joined by a Connection via ``s223:cnx`` (paired with
``s223:pairedConnectionPoint``, mapped to a container's with ``s223:mapsTo``); a point is a
Property observed by a single-property Sensor at an observation location; actuatable
properties are linked with ``s223:actuatedByProperty``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_serializer
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import RDF, RDFS

from .graph import ProjectGraph, new_id
from .projection import (
    ACTUATABLE_KINDS,
    OBSERVABLE_KINDS,
    POINT_KINDS,
    PORT_CLASSES,
    Classifier,
    connection_ends,
    entity_iri,
    entity_kind,
    owner_of_port,
    paired_port,
    point_owner,
    port_connections,
    port_direction,
    port_id,
    port_iri,
    port_label,
    sensors_of,
)
from .vocabulary import QUDT, S223, Vocabulary

PLACEHOLDER = "new:"


class _Op(BaseModel):
    """Unset fields mean "leave unchanged" and explicit nulls mean "clear", so an operation
    serializes only the fields that were set; otherwise a stored proposal would reload
    with every omitted field turned into a clear."""

    model_config = ConfigDict(extra="forbid")

    @model_serializer(mode="wrap")
    def _only_set_fields(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if k == "op" or k in self.model_fields_set}

    def provided(self) -> set[str]:
        return set(self.model_fields_set) - {"op", "id"}


class CreateEquipment(_Op):
    op: Literal["create_equipment"] = "create_equipment"
    id: str | None = Field(None, description="Omit, or 'new:<name>' to reference it from later operations")
    label: str
    type: str = Field(description="Equipment class IRI (or prefixed name)")
    process: str | None = Field(None, description="Treatment process IRI (watr:Process-*)")
    contained_in: str | None = Field(None, description="Id of containing equipment")
    evidence: list[str] | None = Field(None, description="Source observation ids")


class UpdateEquipment(_Op):
    op: Literal["update_equipment"] = "update_equipment"
    id: str
    label: str | None = None
    type: str | None = None
    process: str | None = None
    contained_in: str | None = None


class DeleteEquipment(_Op):
    op: Literal["delete_equipment"] = "delete_equipment"
    id: str


class CreatePoint(_Op):
    op: Literal["create_point"] = "create_point"
    id: str | None = None
    label: str
    point_kind: Literal["measurement", "setpoint", "status", "command", "alarm", "parameter"] = "measurement"
    point_type: str | None = Field(None, description="Brick point class (Brick projects only)")
    quantity_kind: str | None = None
    unit: str | None = None
    equipment: str | None = Field(None, description="Id of the equipment the point belongs to")
    medium: str | None = None
    substance: str | None = None
    sensor_type: str | None = Field(None, description="Sensor class IRI for measurements/status")
    enumeration_kind: str | None = None
    evidence: list[str] | None = Field(None, description="Source observation ids")


class UpdatePoint(_Op):
    op: Literal["update_point"] = "update_point"
    id: str
    label: str | None = None
    point_kind: Literal["measurement", "setpoint", "status", "command", "alarm", "parameter"] | None = None
    point_type: str | None = None
    quantity_kind: str | None = None
    unit: str | None = None
    equipment: str | None = None
    medium: str | None = None
    substance: str | None = None
    sensor_type: str | None = None
    enumeration_kind: str | None = None


class DeletePoint(_Op):
    op: Literal["delete_point"] = "delete_point"
    id: str


class CreateConnection(_Op):
    op: Literal["create_connection"] = "create_connection"
    id: str | None = None
    label: str | None = None
    from_equipment: str | None = Field(None, description="Upstream equipment id (or give from_point)")
    to_equipment: str | None = Field(None, description="Downstream equipment id (or give to_point)")
    from_point: str | None = Field(None, description="Existing outlet connection point to join (223P/WaTr)")
    to_point: str | None = Field(None, description="Existing inlet connection point to join (223P/WaTr)")
    medium: str | None = Field(None, description="Medium IRI, e.g. s223:Fluid-Water (223P/WaTr: required unless a point gives it)")
    type: str | None = Field(None, description="Connection class IRI; default s223:Pipe")
    evidence: list[str] | None = None


class UpdateConnection(_Op):
    op: Literal["update_connection"] = "update_connection"
    id: str
    label: str | None = None
    from_equipment: str | None = None
    to_equipment: str | None = None
    from_point: str | None = None
    to_point: str | None = None
    medium: str | None = None
    type: str | None = None


class DeleteConnection(_Op):
    op: Literal["delete_connection"] = "delete_connection"
    id: str


Direction = Literal["inlet", "outlet", "bidirectional"]


class CreateConnectionPoint(_Op):
    op: Literal["create_connection_point"] = "create_connection_point"
    id: str | None = Field(None, description="Omit, or 'new:<name>' to reference it from later operations")
    label: str | None = None
    equipment: str = Field(description="Id of the equipment the connection point belongs to")
    direction: Direction
    medium: str = Field(description="Medium IRI, e.g. s223:Fluid-Air")
    paired_with: str | None = Field(None, description="Id of this equipment's connection point on the same flow path")
    maps_to: str | None = Field(None, description="Id of the containing equipment's connection point this one is")
    evidence: list[str] | None = None


class UpdateConnectionPoint(_Op):
    op: Literal["update_connection_point"] = "update_connection_point"
    id: str
    label: str | None = None
    equipment: str | None = None
    direction: Direction | None = None
    medium: str | None = None
    paired_with: str | None = None
    maps_to: str | None = None


class DeleteConnectionPoint(_Op):
    op: Literal["delete_connection_point"] = "delete_connection_point"
    id: str


Operation = Annotated[
    Union[
        CreateEquipment, UpdateEquipment, DeleteEquipment,
        CreatePoint, UpdatePoint, DeletePoint,
        CreateConnection, UpdateConnection, DeleteConnection,
        CreateConnectionPoint, UpdateConnectionPoint, DeleteConnectionPoint,
    ],
    Field(discriminator="op"),
]
OperationList = TypeAdapter(list[Operation])

# Field names a person can lock by setting/confirming them.
LOCKABLE_FIELDS = {
    "equipment": {"label", "type", "process", "contained_in"},
    "point": {"label", "point_kind", "point_type", "quantity_kind", "unit", "equipment", "medium",
              "substance", "sensor_type", "enumeration_kind"},
    "connection": {"label", "from_equipment", "to_equipment", "from_point", "to_point", "medium", "type"},
    "connection_point": {"label", "equipment", "direction", "medium", "paired_with", "maps_to"},
}

TERM_FIELDS = {
    "type": None,  # depends on entity kind
    "process": "process",
    "quantity_kind": "quantity_kind",
    "unit": "unit",
    "medium": "medium",
    "substance": "substance",
    "sensor_type": "sensor",
    "enumeration_kind": "enumeration",
    "point_type": "point_class",
}

# Fields that exist in each model family; anything else is rejected with an explanation.
FAMILY_FIELDS = {
    "brick": {
        "equipment": {"label", "type", "contained_in", "evidence"},
        "point": {"label", "point_kind", "point_type", "unit", "equipment", "evidence"},
        "connection": {"label", "from_equipment", "to_equipment", "evidence"},
    },
    "s223": {
        "equipment": {"label", "type", "process", "contained_in", "evidence"},
        "point": {"label", "point_kind", "quantity_kind", "unit", "equipment", "medium", "substance",
                  "sensor_type", "enumeration_kind", "evidence"},
        "connection": {"label", "from_equipment", "to_equipment", "from_point", "to_point", "medium", "type",
                       "evidence"},
        "connection_point": {"label", "equipment", "direction", "medium", "paired_with", "maps_to", "evidence"},
    },
}
FAMILY_NAMES = {"brick": "Brick", "s223": "223P/WaTr"}
REF_FIELDS = {"contained_in": "equipment", "equipment": "equipment",
              "from_equipment": "equipment", "to_equipment": "equipment",
              "from_point": "connection_point", "to_point": "connection_point",
              "paired_with": "connection_point", "maps_to": "connection_point"}


class OperationError(Exception):
    """Malformed operations: they cannot be applied at all."""

    def __init__(self, problems: list[str], unknown_terms: list[tuple[str, str, str | None]] | None = None):
        super().__init__("; ".join(problems))
        self.problems = problems
        # (field, value as written, expected term kind) for terms not in the vocabulary
        self.unknown_terms = unknown_terms or []


@dataclass
class ApplyResult:
    # entity id -> set of fields changed (or {"created"} / {"deleted"})
    changes: dict[str, set[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def touch(self, eid: str, *fields: str) -> None:
        self.changes.setdefault(eid, set()).update(fields)


def entity_kind_of_op(op) -> str:
    return op.op.split("_", 1)[1]  # create_connection_point -> connection_point


def expand_term(vocab: Vocabulary, value: str) -> str:
    """Accept full IRIs, <IRIs>, and known prefixed names (s223:Pipe, watr:Tank, unit:PSI)."""
    v = value.strip().strip("<>")
    if "://" in v or v.startswith("urn:"):
        return v
    if ":" in v:
        prefix, local = v.split(":", 1)
        known = {**vocab.namespaces, "s223": str(S223), "qudt": str(QUDT),
                 "unit": "http://qudt.org/vocab/unit/",
                 "quantitykind": "http://qudt.org/vocab/quantitykind/",
                 "qk": "http://qudt.org/vocab/quantitykind/"}
        if prefix in known:
            return known[prefix] + local
    return v


def resolve(pg: ProjectGraph, vocab: Vocabulary, ops: list) -> list:
    """Check structure; mint ids for new entities. Returns new, fully resolved operations.

    Raises OperationError listing every problem found (not just the first).
    """
    problems: list[str] = []
    unknown_terms: list[tuple[str, str, str | None]] = []
    placeholders: dict[str, str] = {}
    created: dict[str, str] = {}  # id -> kind, for entities created earlier in the list
    deleted: set[str] = set()
    out = []
    allowed = FAMILY_FIELDS[vocab.family]

    def exists(eid: str, kind: str) -> bool:
        if eid in deleted:
            return False
        if eid in created:
            return created[eid] == kind
        return entity_kind(pg, vocab, eid) == kind

    def later_placeholders(i: int) -> set[str]:
        return {o.id for o in ops[i + 1:] if o.op.startswith("create_") and (o.id or "").startswith(PLACEHOLDER)}

    for i, op in enumerate(ops):
        where = f"operation {i + 1} ({op.op})"
        data = op.model_dump(exclude_unset=True)
        data["op"] = op.op
        kind = entity_kind_of_op(op)
        if kind not in allowed:
            problems.append(f"{where}: {kind.replace('_', ' ')}s do not exist in {FAMILY_NAMES[vocab.family]} models")
            continue
        extra = sorted(set(data) - {"op", "id"} - allowed[kind])
        if extra:
            problems.append(f"{where}: {', '.join(extra)} do(es) not apply to {kind}s in "
                            f"{FAMILY_NAMES[vocab.family]} models")
            continue
        if vocab.family == "s223":
            if data.get("point_kind") in ("alarm", "parameter"):
                problems.append(f"{where}: point_kind {data['point_kind']!r} is only available in Brick models")
                continue
            if op.op == "create_connection" and not (data.get("medium") or data.get("from_point")
                                                     or data.get("to_point")):
                problems.append(f"{where}: medium is required (what the pipe or duct carries)")
                continue
        if op.op == "create_connection" and not (data.get("from_equipment") or data.get("from_point")):
            problems.append(f"{where}: give from_equipment" + (" or from_point" if vocab.family == "s223" else ""))
            continue
        if op.op == "create_connection" and not (data.get("to_equipment") or data.get("to_point")):
            problems.append(f"{where}: give to_equipment" + (" or to_point" if vocab.family == "s223" else ""))
            continue
        if vocab.family == "s223" and "process" in data and not vocab.namespaces.get("watr"):
            problems.append(f"{where}: treatment processes are only available in WaTr models")
            continue

        if op.op.startswith("create_"):
            raw = data.get("id")
            if raw and not raw.startswith(PLACEHOLDER):
                if pg.iri(raw) is not None or raw in created or entity_kind(pg, vocab, raw) is not None:
                    problems.append(f"{where}: id {raw!r} already exists")
                    continue
                eid = raw
            else:
                eid = new_id(kind)
                if raw:
                    placeholders[raw] = eid
            data["id"] = eid
            created[eid] = kind
        else:
            eid = placeholders.get(data["id"], data["id"])
            data["id"] = eid
            if not exists(eid, kind):
                problems.append(f"{where}: no {kind} with id {eid!r}")
                continue

        for fname, target_kind in REF_FIELDS.items():
            if fname in data and data[fname] is not None:
                ref = placeholders.get(data[fname], data[fname])
                data[fname] = ref
                if ref in later_placeholders(i):
                    problems.append(f"{where}: {fname} refers to {ref!r}, which a later operation creates; "
                                    "create it first, or set this in an update after both exist")
                elif not exists(ref, target_kind):
                    problems.append(f"{where}: {fname} refers to unknown {target_kind.replace('_', ' ')} {ref!r}")
                elif ref == eid:
                    problems.append(f"{where}: {fname} cannot refer to the entity itself")

        for fname, term_kind in TERM_FIELDS.items():
            if fname in data and data[fname] is not None:
                iri = expand_term(vocab, data[fname])
                data[fname] = iri
                expected = term_kind or {"equipment": "equipment", "connection": "connection"}.get(kind)
                t = vocab.term(iri)
                if t is None:
                    problems.append(f"{where}: {fname} {iri!r} is not a term in the loaded vocabulary")
                    unknown_terms.append((fname, iri, expected))
                elif expected and t.kind != expected:
                    problems.append(f"{where}: {fname} {iri!r} is a {t.kind}, expected a {expected}")
                elif t.abstract:
                    problems.append(f"{where}: {fname} {t.label!r} is abstract and cannot be instantiated")

        if "label" in data and data["label"] is not None and not str(data["label"]).strip():
            problems.append(f"{where}: label cannot be empty")
        if op.op.startswith("delete_"):
            deleted.add(eid)
        out.append(type(op).model_validate(data))

    if problems:
        raise OperationError(problems, unknown_terms)
    return out


class _Compiler:
    def __init__(self, pg: ProjectGraph, vocab: Vocabulary, result: ApplyResult):
        self.pg, self.vocab, self.r = pg, vocab, result
        self.g = pg.model
        watr = vocab.namespaces.get("watr")
        self.has_process = URIRef(watr + "hasProcess") if watr else None
        self.touched_ports: set[URIRef] = set()  # checked against 223P's port rules after all ops
        self.named_ports: set[str] = set()  # connection point ids the operations refer to; kept
        self.problems: list[str] = []

    def node(self, eid: str) -> URIRef:
        n = self.pg.iri(eid)
        if n is None:
            raise OperationError([f"{eid} no longer exists when this operation runs "
                                  "(an earlier operation in the proposal removed it)"])
        return n

    def gone(self, eid: str) -> bool:
        """Already removed by an earlier operation's cascade (deleting equipment takes its
        connections and connection points); deleting it again is then a no-op."""
        n = entity_iri(self.pg, eid)
        return n is None or ((n, None, None) not in self.g and (None, None, n) not in self.g)

    def set_one(self, s, p, value) -> None:
        self.g.remove((s, p, None))
        if value is not None:
            self.g.add((s, p, URIRef(value) if not isinstance(value, RDFLiteral) else value))

    def remove_node(self, n) -> None:
        self.g.remove((n, None, None))
        self.g.remove((None, None, n))

    # ------------------------------------------------------------ equipment

    def set_type(self, n, iri: str, kind: str) -> None:
        for t in list(self.g.objects(n, RDF.type)):
            if self.vocab.kind_of(str(t)) == kind:
                self.g.remove((n, RDF.type, t))
        self.g.add((n, RDF.type, URIRef(iri)))

    def create_equipment(self, op: CreateEquipment) -> None:
        n = self.pg.register(op.id)  # type: ignore[arg-type]
        self.g.add((n, RDF.type, URIRef(op.type)))
        self.g.add((n, RDFS.label, RDFLiteral(op.label)))
        if op.process and self.has_process:
            self.g.add((n, self.has_process, URIRef(op.process)))
        if op.contained_in:
            self.g.add((self.node(op.contained_in), S223.contains, n))
        self.pg.add_evidence(n, op.evidence or [])
        self.r.touch(op.id, "created", *op.provided())  # type: ignore[arg-type]

    def update_equipment(self, op: UpdateEquipment) -> None:
        n = self.node(op.id)
        fields = op.provided()
        if "label" in fields:
            self.set_one(n, RDFS.label, RDFLiteral(op.label or ""))
        if "type" in fields and op.type:
            self.set_type(n, op.type, "equipment")
        if "process" in fields and self.has_process:
            self.set_one(n, self.has_process, op.process)
        if "contained_in" in fields:
            self.g.remove((None, S223.contains, n))
            if op.contained_in:
                self.g.add((self.node(op.contained_in), S223.contains, n))
        self.r.touch(op.id, *fields)

    def delete_equipment(self, op: DeleteEquipment) -> None:
        n = self.node(op.id)
        # Connections attached to this equipment's ports go with it.
        for port in list(self.g.objects(n, S223.hasConnectionPoint)) + list(
                self.g.subjects(S223.isConnectionPointOf, n)):
            for cx in list(self.g.subjects(S223.cnx, port)) + list(self.g.objects(port, S223.connectsThrough)):
                cid = self.pg.id_of(cx)
                if cid and (cx, None, None) in self.g:
                    self.delete_connection(DeleteConnection(id=cid))
            self.remove_node(port)
            pid = self.pg.id_of(port)
            if pid:  # a connection point with an id: drop its annotations too
                self.pg.unregister(pid)
                self.r.touch(pid, "deleted")
        # Points stay, unassigned.
        orphaned = 0
        for p in list(self.g.objects(n, S223.hasProperty)) + list(self.g.objects(n, S223.actuatedByProperty)):
            orphaned += 1
            pid = self.pg.id_of(p)
            if pid:
                self.r.touch(pid, "equipment")
        for s in list(self.g.subjects(S223.hasObservationLocation, n)):
            self.g.remove((s, S223.hasObservationLocation, n))
        for child in list(self.g.objects(n, S223.contains)):
            cid = self.pg.id_of(child)
            if cid:
                self.r.touch(cid, "contained_in")
        self.remove_node(n)
        self.pg.unregister(op.id)
        if orphaned:
            self.r.notes.append(f"{orphaned} point(s) are no longer assigned to equipment")
        self.r.touch(op.id, "deleted")

    # --------------------------------------------------------------- points

    def _sensor_node(self, point_id: str) -> URIRef:
        return self.pg.ns[f"{point_id}.sensor"]

    def _attach(self, pid: str, p, kind: str, equipment: str | None, sensor_type: str | None) -> None:
        """(Re)build owner links and the sensor for a point of the given kind."""
        g = self.g
        g.remove((None, S223.hasProperty, p))
        g.remove((None, S223.actuatedByProperty, p))
        owner = self.node(equipment) if equipment else None
        if owner is not None:
            g.add((owner, S223.hasProperty, p))
            if kind in ACTUATABLE_KINDS:
                g.add((owner, S223.actuatedByProperty, p))
        sensors = sensors_of(self.pg, p)
        if kind in OBSERVABLE_KINDS:
            if not sensors:
                s = self._sensor_node(pid)
                g.add((s, RDF.type, URIRef(sensor_type) if sensor_type else S223.Sensor))
                g.add((s, S223.observes, p))
                g.add((s, RDFS.label, RDFLiteral(f"{self._label(p)} sensor")))
                sensors = [s]
            for s in sensors:
                g.remove((s, S223.hasObservationLocation, None))
                if owner is not None:
                    g.add((s, S223.hasObservationLocation, owner))
                if sensor_type:
                    self.set_type(s, sensor_type, "sensor")
        else:
            for s in sensors:
                if str(s).startswith(str(self._sensor_node(pid))):
                    self.remove_node(s)
                else:
                    g.remove((s, S223.observes, p))

    def _label(self, n) -> str:
        return str(next(iter(self.g.objects(n, RDFS.label)), ""))

    def create_point(self, op: CreatePoint) -> None:
        n = self.pg.register(op.id)  # type: ignore[arg-type]
        g = self.g
        g.add((n, RDF.type, POINT_KINDS[op.point_kind]))
        g.add((n, RDFS.label, RDFLiteral(op.label)))
        for pred, value in ((QUDT.hasQuantityKind, op.quantity_kind), (QUDT.hasUnit, op.unit),
                            (S223.ofMedium, op.medium), (S223.ofSubstance, op.substance),
                            (S223.hasEnumerationKind, op.enumeration_kind)):
            if value:
                g.add((n, pred, URIRef(value)))
        self._attach(op.id, n, op.point_kind, op.equipment, op.sensor_type)  # type: ignore[arg-type]
        self.pg.add_evidence(n, op.evidence or [])
        self._check_unit(op.quantity_kind, op.unit, op.label)
        self.r.touch(op.id, "created", *op.provided())  # type: ignore[arg-type]

    def update_point(self, op: UpdatePoint) -> None:
        n = self.node(op.id)
        g = self.g
        fields = op.provided()
        cls = Classifier(self.vocab)
        kind = cls.point_kind(self.pg, n)
        if "label" in fields:
            self.set_one(n, RDFS.label, RDFLiteral(op.label or ""))
        for fname, pred in (("quantity_kind", QUDT.hasQuantityKind), ("unit", QUDT.hasUnit),
                            ("medium", S223.ofMedium), ("substance", S223.ofSubstance),
                            ("enumeration_kind", S223.hasEnumerationKind)):
            if fname in fields:
                self.set_one(n, pred, getattr(op, fname))
        if "point_kind" in fields and op.point_kind and op.point_kind != kind:
            self.set_type(n, str(POINT_KINDS[op.point_kind]), "property")
            kind = op.point_kind
        if fields & {"point_kind", "equipment", "sensor_type"}:
            owner = point_owner(self.pg, n)
            equipment = op.equipment if "equipment" in fields else (self.pg.id_of(owner) if owner is not None else None)
            self._attach(op.id, n, kind, equipment, op.sensor_type if "sensor_type" in fields else None)
        if fields & {"quantity_kind", "unit"}:
            self._check_unit(
                str(next(iter(g.objects(n, QUDT.hasQuantityKind)), "")) or None,
                str(next(iter(g.objects(n, QUDT.hasUnit)), "")) or None,
                self._label(n),
            )
        self.r.touch(op.id, *fields)

    def _check_unit(self, qk: str | None, unit: str | None, label: str) -> None:
        if qk and unit:
            t = self.vocab.term(unit)
            if t and not self.vocab.unit_fits(unit, qk):
                self.r.notes.append(
                    f"{label}: unit {t.label} is not declared for {self.vocab.label(qk)} in QUDT")

    def delete_point(self, op: DeletePoint) -> None:
        n = self.node(op.id)
        for s in sensors_of(self.pg, n):
            if len(set(self.g.objects(s, S223.observes))) <= 1:
                self.remove_node(s)
        self.remove_node(n)
        self.pg.unregister(op.id)
        self.r.touch(op.id, "deleted")

    # ---------------------------------------------------------- connections

    def _port(self, cid: str, end: str, owner, medium) -> URIRef:
        port = self.pg.ns[f"{cid}.{end}"]
        self.g.add((port, RDF.type, S223.OutletConnectionPoint if end == "out" else S223.InletConnectionPoint))
        if medium is not None:
            self.g.add((port, S223.hasMedium, URIRef(medium)))
        self.g.add((owner, S223.hasConnectionPoint, port))
        return port

    def _disposable(self, cx, port) -> bool:
        """A port minted for this connection that nobody has made into a connection point of its
        own (id, pairing, mapsTo) goes with the connection; any other port is an equipment fact."""
        g = self.g
        return (str(port).startswith(str(cx) + ".") and self.pg.id_of(port) is None
                and port_id(self.pg, port) not in self.named_ports
                and not any((port, p, None) in g or (None, p, port) in g
                            for p in (S223.pairedConnectionPoint, S223.mapsTo)))

    def _detach(self, cx, port) -> None:
        """Unhook one end of a connection, dropping the port if it was only the connection's."""
        for t in ((cx, S223.cnx, port), (port, S223.cnx, cx), (port, S223.connectsThrough, cx),
                  (cx, S223.connectsAt, port)):
            self.g.remove(t)
        if self._disposable(cx, port):
            self.remove_node(port)

    def create_connection(self, op: CreateConnection) -> None:
        cid = op.id
        n = self.pg.register(cid)  # type: ignore[arg-type]
        self.pg.add_evidence(n, op.evidence or [])
        g = self.g
        g.add((n, RDF.type, URIRef(op.type) if op.type else S223.Pipe))
        a_port = self.port(op.from_point) if op.from_point else None
        b_port = self.port(op.to_point) if op.to_point else None
        self._check_ends(a_port, b_port, op.label or "new connection")
        a = self._end_owner(a_port, op.from_equipment, "from")
        b = self._end_owner(b_port, op.to_equipment, "to")
        medium = op.medium or next((str(m) for p in (a_port, b_port) if p is not None
                                    for m in g.objects(p, S223.hasMedium)), None)
        if medium is None:
            self.problems.append(f"{op.label or 'new connection'}: give a medium; the connection points have none")
        name = lambda owner, port: self._label(owner) if owner is not None else self._port_name(port)  # noqa: E731
        g.add((n, RDFS.label, RDFLiteral(op.label or f"{name(a, a_port)} → {name(b, b_port)}")))
        if medium is not None:
            g.add((n, S223.hasMedium, URIRef(medium)))
        g.add((n, S223.cnx, a_port if a_port is not None else self._port(cid, "out", a, medium)))  # type: ignore[arg-type]
        g.add((n, S223.cnx, b_port if b_port is not None else self._port(cid, "in", b, medium)))  # type: ignore[arg-type]
        self.r.touch(cid, "created", *op.provided())  # type: ignore[arg-type]

    def _check_ends(self, out_port, in_port, name: str) -> None:
        """The upstream end is an outlet (or bidirectional), the downstream end an inlet, and they differ."""
        if out_port is not None and out_port == in_port:
            self.problems.append(f"{name}: from_point and to_point are the same connection point")
        if out_port is not None and port_direction(self.pg, out_port) == "inlet":
            self.problems.append(f"{name}: from_point {self._port_name(out_port)} is an inlet; the upstream end must be an outlet")
        if in_port is not None and port_direction(self.pg, in_port) == "outlet":
            self.problems.append(f"{name}: to_point {self._port_name(in_port)} is an outlet; the downstream end must be an inlet")

    def _end_owner(self, port, equipment: str | None, end: str):
        """The equipment at one end: the given point's owner, which must agree with the equipment if both are given."""
        if port is None:
            return self.node(equipment)  # type: ignore[arg-type]
        owner = owner_of_port(self.pg, port)
        if equipment and owner != self.node(equipment):
            self.problems.append(f"{end}_point {self._port_name(port)} does not belong to "
                                 f"{self._label(self.node(equipment))}")
        return owner

    def update_connection(self, op: UpdateConnection) -> None:
        n = self.node(op.id)
        g = self.g
        fields = op.provided()
        if "label" in fields:
            self.set_one(n, RDFS.label, RDFLiteral(op.label or ""))
        if "type" in fields and op.type:
            self.set_type(n, op.type, "connection")
        ends, _ = connection_ends(self.pg, n)
        if "medium" in fields and op.medium:
            self.set_one(n, S223.hasMedium, op.medium)
            for port, _, _ in ends:
                self.set_one(port, S223.hasMedium, op.medium)
        medium = next(iter(g.objects(n, S223.hasMedium)), None)
        for pname, ename, direction in (("from_point", "from_equipment", "out"), ("to_point", "to_equipment", "in")):
            ends, _ = connection_ends(self.pg, n)
            old = next((p for p, _, d in ends if d == direction), None)
            if old is None:  # undirected/imported: take the end not owned by the other side
                idx = 0 if direction == "out" else 1
                old = ends[idx][0] if len(ends) > idx else None
            if pname in fields and getattr(op, pname):
                new = self.port(getattr(op, pname))
                other = next((p for p, _, _ in connection_ends(self.pg, n)[0] if p != old), None)
                self._check_ends(*((new, other) if direction == "out" else (other, new)), self._label(n))
                if ename in fields and getattr(op, ename):
                    self._end_owner(new, getattr(op, ename), pname.split("_")[0])
                if old is not None and old != new:
                    self._detach(n, old)
                g.add((n, S223.cnx, new))
            elif ename in fields and getattr(op, ename):
                new_owner = self.node(getattr(op, ename))
                if old is None:
                    continue
                if self._disposable(n, old):  # the connection's own port moves with it
                    old_owner = owner_of_port(self.pg, old)
                    if old_owner is not None:
                        g.remove((old_owner, S223.hasConnectionPoint, old))
                    g.remove((old, S223.isConnectionPointOf, None))
                    g.add((new_owner, S223.hasConnectionPoint, old))
                else:  # an equipment's own connection point stays with it; mint one on the new end
                    self._detach(n, old)
                    g.add((n, S223.cnx, self._port(op.id, direction, new_owner, medium)))
        self.r.touch(op.id, *fields)

    def delete_connection(self, op: DeleteConnection) -> None:
        if self.gone(op.id):
            return
        n = self.node(op.id)
        ends, _ = connection_ends(self.pg, n)
        for port, _, _ in ends:
            # Ports the app minted only for this connection go with it; ports that came from an
            # imported model or became connection points in their own right stay.
            if self._disposable(n, port):
                self.remove_node(port)
        self.remove_node(n)
        self.pg.unregister(op.id)
        self.r.touch(op.id, "deleted")

    # ----------------------------------------------------- connection points

    def _port_name(self, n) -> str:
        return port_label(self.pg, self.vocab, n)

    def port(self, cp_id: str) -> URIRef:
        """A connection point by id, registering a port that predates its id (see projection.port_id)."""
        n = port_iri(self.pg, cp_id)
        if n is None:
            raise OperationError([f"connection point {cp_id} no longer exists when this operation runs "
                                  "(an earlier operation in the proposal removed it)"])
        if self.pg.id_of(n) is None:
            self.pg.register(cp_id, n)
        self.touched_ports.add(n)
        return n

    def _pair(self, n, other) -> None:
        for p in (n, other):
            if p is None:
                continue
            old = paired_port(self.pg, p)
            if old is not None and old not in (n, other):
                self.r.touch(port_id(self.pg, old), "paired_with")
            self.g.remove((p, S223.pairedConnectionPoint, None))
            self.g.remove((None, S223.pairedConnectionPoint, p))
        if other is not None:
            # A symmetric relation in 223P; asserted both ways so validation without inference sees it.
            self.g.add((n, S223.pairedConnectionPoint, other))
            self.g.add((other, S223.pairedConnectionPoint, n))
            self.r.touch(port_id(self.pg, other), "paired_with")

    def _set_owner(self, n, equipment: str) -> None:
        self.g.remove((None, S223.hasConnectionPoint, n))
        self.g.remove((n, S223.isConnectionPointOf, None))
        self.g.add((self.node(equipment), S223.hasConnectionPoint, n))

    def create_connection_point(self, op: CreateConnectionPoint) -> None:
        n = self.pg.register(op.id)  # type: ignore[arg-type]
        self.touched_ports.add(n)
        self.g.add((n, RDF.type, PORT_CLASSES[op.direction]))
        if op.label:
            self.g.add((n, RDFS.label, RDFLiteral(op.label)))
        self.g.add((n, S223.hasMedium, URIRef(op.medium)))
        self._set_owner(n, op.equipment)
        if op.paired_with:
            self._pair(n, self.port(op.paired_with))
        if op.maps_to:
            self.g.add((n, S223.mapsTo, self.port(op.maps_to)))
        self.pg.add_evidence(n, op.evidence or [])
        self.r.touch(op.id, "created", *op.provided())  # type: ignore[arg-type]

    def update_connection_point(self, op: UpdateConnectionPoint) -> None:
        n = self.port(op.id)
        fields = op.provided()
        if "label" in fields:
            self.g.remove((n, RDFS.label, None))
            if op.label:
                self.g.add((n, RDFS.label, RDFLiteral(op.label)))
        if "equipment" in fields:
            if op.equipment:
                self._set_owner(n, op.equipment)
            else:
                self.problems.append(f"{self._port_name(n)}: a connection point must belong to equipment")
        if "direction" in fields and op.direction:
            for cls in PORT_CLASSES.values():
                self.g.remove((n, RDF.type, cls))
            self.g.add((n, RDF.type, PORT_CLASSES[op.direction]))
        if "medium" in fields and op.medium:
            self.set_one(n, S223.hasMedium, op.medium)
        if "paired_with" in fields:
            self._pair(n, self.port(op.paired_with) if op.paired_with else None)
        if "maps_to" in fields:
            self.g.remove((n, S223.mapsTo, None))
            if op.maps_to:
                self.g.add((n, S223.mapsTo, self.port(op.maps_to)))
        self.r.touch(op.id, *fields)

    def delete_connection_point(self, op: DeleteConnectionPoint) -> None:
        if self.gone(op.id):
            return
        n = self.port(op.id)
        for cx in port_connections(self.pg, n):
            cid = self.pg.id_of(cx)
            if cid and (cx, None, None) in self.g:
                self.delete_connection(DeleteConnection(id=cid))
        other = paired_port(self.pg, n)
        if other is not None:
            self.r.touch(port_id(self.pg, other), "paired_with")
        for child in self.g.subjects(S223.mapsTo, n):
            self.r.touch(port_id(self.pg, child), "maps_to")
        self.remove_node(n)
        self.pg.unregister(op.id)
        self.r.touch(op.id, "deleted")

    def check_ports(self) -> None:
        """223P's one-to-one rules for the connection points these operations touched."""
        g = self.g
        for n in sorted(self.touched_ports, key=str):
            if (n, None, None) not in g:
                continue  # deleted
            name = self._port_name(n)
            owner = owner_of_port(self.pg, n)
            other = paired_port(self.pg, n)
            if other is not None:
                partners = set(g.objects(other, S223.pairedConnectionPoint)) | set(
                    g.subjects(S223.pairedConnectionPoint, other))
                if owner_of_port(self.pg, other) != owner:
                    self.problems.append(f"{name} can only be paired with a connection point of the same equipment")
                elif {port_direction(self.pg, n), port_direction(self.pg, other)} != {"inlet", "outlet"}:
                    self.problems.append(f"{name}: pair an inlet with an outlet")
                elif len(partners) > 1:
                    self.problems.append(f"{self._port_name(other)} is already paired")
            target = next(iter(g.objects(n, S223.mapsTo)), None)
            if target is not None:
                container = owner_of_port(self.pg, target)
                if owner is None or container is None or (container, S223.contains, owner) not in g:
                    self.problems.append(f"{name} can only map to a connection point of the equipment that contains "
                                         f"{self._label(owner) if owner is not None else 'its equipment'} (maps_to goes "
                                         "from the inner equipment's point to its container's point)")
                elif len(set(g.subjects(S223.mapsTo, target))) > 1:
                    self.problems.append(f"{self._port_name(target)} already has a contained "
                                         "connection point mapped to it (mapsTo is one-to-one)")
            if len(port_connections(self.pg, n)) > 1:
                self.problems.append(f"{name} is already joined by another connection")


def containment_cycles(pg: ProjectGraph, vocab: Vocabulary, equipment_ids: list[str]) -> list[str]:
    """Equipment that would end up inside itself (A in B, B in A, ...)."""
    if vocab.family == "brick":
        from .vocabulary import BRICK

        pred = BRICK.hasPart
    else:
        pred = S223.contains
    problems = []
    for eid in equipment_ids:
        start = pg.iri(eid)
        seen, cur = set(), start
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = next(iter(pg.model.subjects(pred, cur)), None)
            if cur == start:
                label = pg.model.value(start, RDFS.label) or eid
                problems.append(f"{label} would end up inside itself (its containers form a loop)")
                break
    return problems


def apply(pg: ProjectGraph, vocab: Vocabulary, resolved_ops: list, lock: bool = True) -> ApplyResult:
    """Apply resolved operations to ``pg`` in place. Use on a copy."""
    result = ApplyResult()
    if vocab.family == "brick":
        from .brick import BrickCompiler

        comp = BrickCompiler(pg, vocab, result)
    else:
        comp = _Compiler(pg, vocab, result)  # type: ignore[assignment]
    if isinstance(comp, _Compiler):
        # A port named anywhere in the proposal is one the author means to keep, even if an
        # earlier operation moves the connection it was minted for.
        comp.named_ports = {v for op in resolved_ops for k, v in op.model_dump(exclude_unset=True).items()
                            if isinstance(v, str) and v.startswith("cp-")
                            and (k in ("from_point", "to_point", "paired_with", "maps_to")
                                 or (k == "id" and op.op.endswith("_connection_point")))}
    for op in resolved_ops:
        for fname in TERM_FIELDS:
            value = getattr(op, fname, None)
            t = vocab.term(value) if isinstance(value, str) else None
            if t is not None and t.deprecated:
                instead = vocab.term(t.replaced_by) if t.replaced_by else None
                result.notes.append(f"{t.label} ({vocab.curie(value)}) is deprecated or superseded in the loaded vocabulary"
                                    + (f"; use {instead.label} ({vocab.curie(instead.iri)}) instead" if instead else ""))
        getattr(comp, op.op)(op)
    problems = list(getattr(comp, "problems", []))
    if isinstance(comp, _Compiler):
        comp.check_ports()
        problems = comp.problems
    problems += containment_cycles(pg, vocab, [eid for eid, f in result.changes.items() if "contained_in" in f])
    if problems:
        raise OperationError(problems)
    if lock:
        for eid, fields in result.changes.items():
            node = entity_iri(pg, eid)
            kind = entity_kind(pg, vocab, eid)
            if node is None or kind is None:
                continue
            pg.lock(node, fields & LOCKABLE_FIELDS.get(kind, set()))
    return result
