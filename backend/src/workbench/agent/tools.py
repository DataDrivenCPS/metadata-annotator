"""Typed tool functions the agent can call. Read-only by construction: they can inspect the
revision the run is based on, the vocabulary, sources and guidance, but only the project
service can change the model.

These are plain Python functions so they can later be exposed over MCP unchanged.
"""

from __future__ import annotations

from typing import Any

from ..graph import ProjectGraph
from ..project import Project
from ..projection import (
    ConnectionPointRow, ConnectionRow, EntityRow, EquipmentRow, PointRow, RelationshipRow, SpaceRow, typed_field,
    project as project_view,
)
from ..vocabulary import S223, Vocabulary
from .guidance import FAMILY_TOPICS, SkillGuidance

PREFIXES = [
    ("unit:", "http://qudt.org/vocab/unit/"),
    ("quantitykind:", "http://qudt.org/vocab/quantitykind/"),
    ("s223:", str(S223)),
    ("brick:", "https://brickschema.org/schema/Brick#"),
]


def curie(vocab: Vocabulary, iri: str | None) -> str:
    if not iri:
        return "-"
    prefixes = [(f"{p}:", ns) for p, ns in vocab.namespaces.items() if p in ("watr",)] + PREFIXES
    for p, ns in prefixes:
        if iri.startswith(ns):
            return p + iri[len(ns):]
    short = vocab.curie(iri)  # other prefixes the ontologies declare, e.g. g36:
    return short if short != iri else f"<{iri}>"


def term_ref(vocab: Vocabulary, ref) -> str:
    """``label [curie]``, or just the curie when the label only respells its name."""
    if ref is None:
        return "-"
    short = curie(vocab, ref.iri)
    return short if _plain(ref.label) == _plain(short.rsplit(":", 1)[-1]) else f"{ref.label} [{short}]"


def _plain(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def entity_line(vocab: Vocabulary, row) -> str:
    if isinstance(row, EquipmentRow):
        parent = f" | inside: {row.contained_in.id}" if row.contained_in else ""
        place = f" | in space: {row.location.id} (\"{row.location.label}\")" if row.location else ""
        process = f" | process: {term_ref(vocab, row.process)}" if row.process else ""
        return (f"{row.id} | equipment \"{row.label}\" | type: {term_ref(vocab, row.type)}{process} | "
                f"points: {row.point_count}{parent}{place}")
    if isinstance(row, EntityRow):
        return f"{row.id} | {term_ref(vocab, row.type)} \"{row.label}\" | relationships: {row.relation_count}"
    if isinstance(row, RelationshipRow):
        target = f"{row.object.id} (\"{row.object.label}\")" if row.object else term_ref(vocab, row.value)
        return (f"{row.id} | {row.subject.id} (\"{row.subject.label}\") {curie(vocab, row.relation.iri)} {target}")
    if isinstance(row, SpaceRow):
        parent = f" | part of: {row.part_of.id} (\"{row.part_of.label}\")" if row.part_of else ""
        return (f"{row.id} | space \"{row.label}\" | type: {term_ref(vocab, row.type)}{parent} | "
                f"equipment in it: {row.equipment_count}")
    if isinstance(row, PointRow):
        eq = f"{row.equipment.id} (\"{row.equipment.label}\")" if row.equipment else "UNASSIGNED"
        extra = ""
        if row.medium:
            extra += f" | medium: {term_ref(vocab, row.medium)}"
        if row.substance:
            extra += f" | substance: {term_ref(vocab, row.substance)}"
        if row.point_type is not None or vocab.family == "brick":
            return (f"{row.id} | point \"{row.label}\" | type: {term_ref(vocab, row.point_type)} | kind: {row.point_kind} "
                    f"| unit: {term_ref(vocab, row.unit)} | equipment: {eq}")
        return (f"{row.id} | point \"{row.label}\" | kind: {row.point_kind} | measures: "
                f"{term_ref(vocab, row.quantity_kind)} | unit: {term_ref(vocab, row.unit)} | "
                f"equipment: {eq}" + (f" | sensor: {term_ref(vocab, row.sensor_type)}" if row.sensor_type else "") + extra)
    if isinstance(row, ConnectionRow):
        a = f"{row.from_equipment.id} (\"{row.from_equipment.label}\")" if row.from_equipment else "?"
        b = f"{row.to_equipment.id} (\"{row.to_equipment.label}\")" if row.to_equipment else "?"
        if row.from_point or row.to_point:
            a += f" at {row.from_point.id if row.from_point else '?'}"
            b += f" at {row.to_point.id if row.to_point else '?'}"
        return (f"{row.id} | connection \"{row.label}\" | from {a} to {b} | "
                f"medium: {term_ref(vocab, row.medium)} | type: {term_ref(vocab, row.type)}")
    if isinstance(row, ConnectionPointRow):
        eq = f"{row.equipment.id} (\"{row.equipment.label}\")" if row.equipment else "no equipment"
        extra = "".join(f" | {name}: {ref.id}" for name, ref in (
            ("paired with", row.paired_with), ("maps to", row.maps_to), ("mapped from", row.mapped_from)) if ref)
        return (f"{row.id} | connection point \"{row.label}\" | {row.direction} of {eq} | "
                f"medium: {term_ref(vocab, row.medium)} | connection: {row.connection.id if row.connection else 'none'}"
                + extra)
    return str(row)


class AgentTools:
    KINDS = ["equipment", "point_class", "location", "sensor", "connection", "process", "medium",
             "substance", "quantity_kind", "unit", "enumeration", "role", "class", "relation"]

    def __init__(self, project: Project, revision: str, guidance: SkillGuidance,
                 preview: ProjectGraph | None = None):
        self.project = project
        self.revision = revision
        self.vocab = project.vocab
        self.guidance = guidance
        self.preview = preview
        self.preview_view = project_view(preview, self.vocab) if preview is not None else None

    def _view(self):
        return self.preview_view if self.preview_view is not None else self.project.view(self.revision)

    def search_terms(self, query: str, kind: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        """Find ontology terms (classes, units, quantity kinds, media...) by text."""
        kinds = [kind] if kind in self.KINDS else None
        results = self.vocab.search(query, kinds, limit)
        return [{"term": curie(self.vocab, t.iri), "label": t.label, "kind": t.kind,
                 **({"symbol": t.symbol} if t.symbol else {}),
                 **({"about": t.comment[:120]} if t.comment else {})} for t in results]

    def units_for(self, quantity_kind: str, limit: int = 25) -> list[dict[str, Any]]:
        from ..operations import expand_term

        qk = expand_term(self.vocab, quantity_kind)
        return [{"term": curie(self.vocab, t.iri), "label": t.label, "symbol": t.symbol}
                for t in self.vocab.units_for(qk)[:limit]]

    def describe_class(self, term: str) -> dict[str, Any]:
        """Parents and required properties (SHACL) of an equipment/sensor class."""
        from ..operations import expand_term

        iri = expand_term(self.vocab, term)
        if self.vocab.term(iri) is None:
            return {"error": f"{term} is not a known term"}
        d = self.vocab.describe_class(iri)
        return {
            "term": curie(self.vocab, iri), "label": d["label"], "about": d["comment"][:300],
            "parents": [curie(self.vocab, p) for p in d["parents"]
                        if not p.endswith(("Concept", "Resource", "Connectable"))],
            "requires": [{k: (curie(self.vocab, v) if isinstance(v, str) and "://" in v else v)
                          for k, v in c.items()} for c in d["constraints"]][:12],
        }

    def find_entities(self, query: str, limit: int = 25) -> list[str]:
        """Search the model's equipment/points/connections/connection points by name."""
        q = query.lower().strip()
        view = self._view()
        hits = [r for r in view.rows().values() if q in r.label.lower() or q in r.id]
        out = []
        for r in hits[:limit]:
            out.append(entity_line(self.vocab, r))
            if isinstance(r, EquipmentRow) and self.vocab.family == "s223":
                cps = [c.id for c in view.connection_points if c.equipment and c.equipment.id == r.id]
                out.append(f"  connection points of {r.id}: {', '.join(cps) if cps else 'none'}")
        return out or [f"(nothing in the model matches {query!r})"]

    def relations_for(self, entity_id: str) -> dict[str, Any]:
        """Relations the vocabulary's shapes allow for this entity, what they point to, and its current ones."""
        view = self._view()
        row = view.rows().get(entity_id)
        if row is None or row.kind == "relationship":  # type: ignore[attr-defined]
            return {"error": f"no entity {entity_id}"}
        pg = self.preview if self.preview is not None else self.project.graph(self.revision)
        from rdflib import URIRef
        from rdflib.namespace import RDF

        types = [str(t) for t in pg.model.objects(URIRef(row.iri), RDF.type)]  # type: ignore[attr-defined]
        allowed = [f"{curie(self.vocab, r['relation'])} -> {', '.join(curie(self.vocab, o) for o in r['objects']) or 'anything'}"
                   + (" (one value)" if r["max"] == 1 else "")
                   + (f" (also shown as {hint})" if (hint := typed_field(self.vocab, r["relation"])) else "")
                   for r in self.vocab.relations_for(types)]
        current = [entity_line(self.vocab, r) for r in view.relationships
                   if r.subject.id == entity_id or (r.object and r.object.id == entity_id)]
        return {"entity": entity_line(self.vocab, row), "allowed_relations": allowed, "current": current}

    def read_guidance(self, topic: str) -> str:
        """Modeling guidance from the BuildingMOTIF skill for one topic."""
        return self.guidance.topic(topic)

    def read_evidence(self, observation_id: str) -> dict[str, Any]:
        """The source observation (point-list row, diagram label...) behind an entity."""
        body = self.project.store.get_body("observations", observation_id)
        return body or {"error": f"no observation {observation_id}"}

    @staticmethod
    def catalog(family: str = "s223") -> str:
        kinds = (["equipment", "point_class", "location", "class", "relation", "unit", "quantity_kind"] if family == "brick"
                 else [k for k in AgentTools.KINDS if k != "point_class"])
        return (
            "search_terms(query, kind?) - find ontology terms; kind one of "
            + ", ".join(kinds)
            + "\nunits_for(quantity_kind) - list units valid for a quantity kind"
            + "\ndescribe_class(term) - parents and required properties of an equipment/sensor class"
            + ("\nfind_entities(query) - find equipment/points/connections in the model by name" if family == "brick"
               else "\nfind_entities(query) - find equipment/points/connections/connection points by name"
                    " (an equipment's name lists its connection points too)")
            + "\nrelations_for(entity_id) - relations the vocabulary allows for an entity, and its current ones"
            + "\nread_evidence(observation_id) - the source record behind an entity"
            + "\nread_guidance(topic) - modeling guidance; topics: " + ", ".join(FAMILY_TOPICS[family])
        )
