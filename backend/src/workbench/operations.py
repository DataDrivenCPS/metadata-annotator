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
connection points with media joined by a Connection via ``s223:cnx``; a point is a
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
    Classifier,
    connection_ends,
    entity_kind,
    owner_of_port,
    point_owner,
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
    from_equipment: str
    to_equipment: str
    medium: str | None = Field(None, description="Medium IRI, e.g. s223:Fluid-Water (223P/WaTr: required)")
    type: str | None = Field(None, description="Connection class IRI; default s223:Pipe")
    evidence: list[str] | None = None


class UpdateConnection(_Op):
    op: Literal["update_connection"] = "update_connection"
    id: str
    label: str | None = None
    from_equipment: str | None = None
    to_equipment: str | None = None
    medium: str | None = None
    type: str | None = None


class DeleteConnection(_Op):
    op: Literal["delete_connection"] = "delete_connection"
    id: str


Operation = Annotated[
    Union[
        CreateEquipment, UpdateEquipment, DeleteEquipment,
        CreatePoint, UpdatePoint, DeletePoint,
        CreateConnection, UpdateConnection, DeleteConnection,
    ],
    Field(discriminator="op"),
]
OperationList = TypeAdapter(list[Operation])

# Field names a person can lock by setting/confirming them.
LOCKABLE_FIELDS = {
    "equipment": {"label", "type", "process", "contained_in"},
    "point": {"label", "point_kind", "point_type", "quantity_kind", "unit", "equipment", "medium",
              "substance", "sensor_type", "enumeration_kind"},
    "connection": {"label", "from_equipment", "to_equipment", "medium", "type"},
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
        "connection": {"label", "from_equipment", "to_equipment", "medium", "type", "evidence"},
    },
}
FAMILY_NAMES = {"brick": "Brick", "s223": "223P/WaTr"}
REF_FIELDS = {"contained_in": "equipment", "equipment": "equipment",
              "from_equipment": "equipment", "to_equipment": "equipment"}


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
    return op.op.split("_", 1)[1]


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

    for i, op in enumerate(ops):
        where = f"operation {i + 1} ({op.op})"
        data = op.model_dump(exclude_unset=True)
        data["op"] = op.op
        kind = entity_kind_of_op(op)
        extra = sorted(set(data) - {"op", "id"} - allowed[kind])
        if extra:
            problems.append(f"{where}: {', '.join(extra)} do(es) not apply to {kind}s in "
                            f"{FAMILY_NAMES[vocab.family]} models")
            continue
        if vocab.family == "s223":
            if data.get("point_kind") in ("alarm", "parameter"):
                problems.append(f"{where}: point_kind {data['point_kind']!r} is only available in Brick models")
                continue
            if op.op == "create_connection" and not data.get("medium"):
                problems.append(f"{where}: medium is required (what the pipe or duct carries)")
                continue
        if vocab.family == "s223" and "process" in data and not vocab.namespaces.get("watr"):
            problems.append(f"{where}: treatment processes are only available in WaTr models")
            continue

        if op.op.startswith("create_"):
            raw = data.get("id")
            if raw and not raw.startswith(PLACEHOLDER):
                if pg.iri(raw) is not None or raw in created:
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
                if not exists(ref, target_kind):
                    problems.append(f"{where}: {fname} refers to unknown {target_kind} {ref!r}")
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

    def node(self, eid: str) -> URIRef:
        n = self.pg.iri(eid)
        assert n is not None, eid
        return n

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

    def _port(self, cid: str, end: str, owner, medium: str) -> URIRef:
        port = self.pg.ns[f"{cid}.{end}"]
        self.g.add((port, RDF.type, S223.OutletConnectionPoint if end == "out" else S223.InletConnectionPoint))
        self.g.add((port, S223.hasMedium, URIRef(medium)))
        self.g.add((owner, S223.hasConnectionPoint, port))
        return port

    def create_connection(self, op: CreateConnection) -> None:
        cid = op.id
        n = self.pg.register(cid)  # type: ignore[arg-type]
        self.pg.add_evidence(n, op.evidence or [])
        g = self.g
        g.add((n, RDF.type, URIRef(op.type) if op.type else S223.Pipe))
        a, b = self.node(op.from_equipment), self.node(op.to_equipment)
        g.add((n, RDFS.label, RDFLiteral(op.label or f"{self._label(a)} → {self._label(b)}")))
        g.add((n, S223.hasMedium, URIRef(op.medium)))
        g.add((n, S223.cnx, self._port(cid, "out", a, op.medium)))  # type: ignore[arg-type]
        g.add((n, S223.cnx, self._port(cid, "in", b, op.medium)))  # type: ignore[arg-type]
        self.r.touch(cid, "created", *op.provided())  # type: ignore[arg-type]

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
        for fname, direction in (("from_equipment", "out"), ("to_equipment", "in")):
            if fname not in fields or not getattr(op, fname):
                continue
            new_owner = self.node(getattr(op, fname))
            port = next((p for p, _, d in ends if d == direction), None)
            if port is None:  # undirected/imported: take the end not owned by the other side
                idx = 0 if direction == "out" else 1
                port = ends[idx][0] if len(ends) > idx else None
            if port is None:
                continue
            old_owner = owner_of_port(self.pg, port)
            if old_owner is not None:
                g.remove((old_owner, S223.hasConnectionPoint, port))
            g.remove((port, S223.isConnectionPointOf, None))
            g.add((new_owner, S223.hasConnectionPoint, port))
        self.r.touch(op.id, *fields)

    def delete_connection(self, op: DeleteConnection) -> None:
        n = self.node(op.id)
        ends, _ = connection_ends(self.pg, n)
        for port, _, _ in ends:
            # Ports the app minted for this connection go with it; ports that came from an
            # imported model are equipment facts and stay.
            if str(port).startswith(str(n) + "."):
                self.remove_node(port)
        self.remove_node(n)
        self.pg.unregister(op.id)
        self.r.touch(op.id, "deleted")


def apply(pg: ProjectGraph, vocab: Vocabulary, resolved_ops: list, lock: bool = True) -> ApplyResult:
    """Apply resolved operations to ``pg`` in place. Use on a copy."""
    result = ApplyResult()
    if vocab.family == "brick":
        from .brick import BrickCompiler

        comp = BrickCompiler(pg, vocab, result)
    else:
        comp = _Compiler(pg, vocab, result)  # type: ignore[assignment]
    for op in resolved_ops:
        for fname in TERM_FIELDS:
            value = getattr(op, fname, None)
            t = vocab.term(value) if isinstance(value, str) else None
            if t is not None and t.deprecated:
                result.notes.append(f"{t.label} ({value}) is deprecated or superseded in the loaded vocabulary")
        getattr(comp, op.op)(op)
    if lock:
        for eid, fields in result.changes.items():
            node = pg.iri(eid)
            kind = entity_kind(pg, vocab, eid)
            if node is None or kind is None:
                continue
            pg.lock(node, fields & LOCKABLE_FIELDS.get(kind, set()))
    return result
