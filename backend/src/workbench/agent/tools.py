"""Typed tool functions the agent can call. Read-only by construction: they can inspect the
revision the run is based on, the vocabulary, sources and guidance, but only the project
service can change the model.

These are plain Python functions so they can later be exposed over MCP unchanged.
"""

from __future__ import annotations

from typing import Any

from ..project import Project
from ..projection import ConnectionRow, EquipmentRow, PointRow
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
    return f"<{iri}>"


def term_ref(vocab: Vocabulary, ref) -> str:
    return "-" if ref is None else f"{ref.label} [{curie(vocab, ref.iri)}]"


def entity_line(vocab: Vocabulary, row) -> str:
    if isinstance(row, EquipmentRow):
        parent = f" | inside: {row.contained_in.id}" if row.contained_in else ""
        return (f"{row.id} | equipment \"{row.label}\" | type: {term_ref(vocab, row.type)} | "
                f"process: {term_ref(vocab, row.process)} | points: {row.point_count}{parent}")
    if isinstance(row, PointRow):
        eq = f"{row.equipment.id} (\"{row.equipment.label}\")" if row.equipment else "UNASSIGNED"
        extra = ""
        if row.medium:
            extra += f" | medium: {term_ref(vocab, row.medium)}"
        if row.substance:
            extra += f" | substance: {term_ref(vocab, row.substance)}"
        if row.point_type is not None or vocab.family == "brick":
            eq = f"{row.equipment.id} (\"{row.equipment.label}\")" if row.equipment else "UNASSIGNED"
            return (f"{row.id} | point \"{row.label}\" | type: {term_ref(vocab, row.point_type)} | kind: {row.point_kind} "
                    f"| unit: {term_ref(vocab, row.unit)} | equipment: {eq}")
        return (f"{row.id} | point \"{row.label}\" | kind: {row.point_kind} | measures: "
                f"{term_ref(vocab, row.quantity_kind)} | unit: {term_ref(vocab, row.unit)} | "
                f"equipment: {eq} | sensor: {term_ref(vocab, row.sensor_type)}{extra}")
    if isinstance(row, ConnectionRow):
        a = f"{row.from_equipment.id} (\"{row.from_equipment.label}\")" if row.from_equipment else "?"
        b = f"{row.to_equipment.id} (\"{row.to_equipment.label}\")" if row.to_equipment else "?"
        return (f"{row.id} | connection \"{row.label}\" | from {a} to {b} | "
                f"medium: {term_ref(vocab, row.medium)} | type: {term_ref(vocab, row.type)}")
    return str(row)


class AgentTools:
    KINDS = ["equipment", "point_class", "location", "sensor", "connection", "process", "medium",
             "substance", "quantity_kind", "unit", "enumeration", "role"]

    def __init__(self, project: Project, revision: str, guidance: SkillGuidance):
        self.project = project
        self.revision = revision
        self.vocab = project.vocab
        self.guidance = guidance

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

    def find_entities(self, query: str, limit: int = 15) -> list[str]:
        """Search the model's equipment/points/connections by name."""
        q = query.lower()
        rows = self.project.view(self.revision).rows().values()
        hits = [r for r in rows if q in r.label.lower() or q == r.id]
        return [entity_line(self.vocab, r) for r in hits[:limit]]

    def read_guidance(self, topic: str) -> str:
        """Modeling guidance from the BuildingMOTIF skill for one topic."""
        return self.guidance.topic(topic)

    def read_evidence(self, observation_id: str) -> dict[str, Any]:
        """The source observation (point-list row, diagram label...) behind an entity."""
        body = self.project.store.get_body("observations", observation_id)
        return body or {"error": f"no observation {observation_id}"}

    @staticmethod
    def catalog(family: str = "s223") -> str:
        kinds = (["equipment", "point_class", "location", "unit", "quantity_kind"] if family == "brick"
                 else [k for k in AgentTools.KINDS if k not in ("point_class", "location")])
        return (
            "search_terms(query, kind?) - find ontology terms; kind one of "
            + ", ".join(kinds)
            + "\nunits_for(quantity_kind) - list units valid for a quantity kind"
            + "\ndescribe_class(term) - parents and required properties of an equipment/sensor class"
            + "\nfind_entities(query) - find equipment/points/connections in the model by name"
            + "\nread_evidence(observation_id) - the source record behind an entity"
            + "\nread_guidance(topic) - modeling guidance; topics: " + ", ".join(FAMILY_TOPICS[family])
        )
