"""Revision content: the model graph plus the application's annotation graph.

The model graph is the authoritative, exportable RDF. The annotation graph holds
application bookkeeping about model nodes that must travel with each revision but is
not part of the exported model:

* ``wb:id`` - the application-managed stable identifier of a top-level entity
  (equipment, point, connection, connection point; see ``projection.port_id`` for
  connection points that predate their ids). Entities minted by the app use ``<ns><id>`` as their
  IRI; imported entities keep their original IRI and get an id here.
* ``wb:locked`` - a field a person has set or confirmed; later extraction must not
  silently overwrite it (see ``operations.LOCKABLE_FIELDS``).
* ``wb:evidence`` - observation ids supporting the entity.

Both graphs are stored together in one TriG snapshot per revision.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable

from rdflib import Dataset, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS

from .vocabulary import S223

WB = Namespace("urn:workbench:ann#")
MODEL_GRAPH = URIRef("urn:workbench:graph:model")
ANN_GRAPH = URIRef("urn:workbench:graph:annotations")

ID_PREFIX = {"equipment": "eq", "point": "pt", "connection": "cx", "connection_point": "cp", "space": "sp",
             "entity": "en", "relationship": "rl"}


def new_id(kind: str) -> str:
    return f"{ID_PREFIX[kind]}-{secrets.token_hex(3)}"


class ProjectGraph:
    def __init__(self, namespace: str, model: Graph | None = None, ann: Graph | None = None):
        self.ns = Namespace(namespace)
        self.model = model if model is not None else Graph()
        self.ann = ann if ann is not None else Graph()
        self.model.bind("s223", S223)
        self.model.bind("rdfs", RDFS)
        self.model.bind("", self.ns)
        self._index: tuple[dict[str, URIRef], dict[URIRef, str]] | None = None

    # ---------------------------------------------------------------- identity

    def _ids(self) -> tuple[dict[str, URIRef], dict[URIRef, str]]:
        if self._index is None:
            by_id: dict[str, URIRef] = {}
            by_iri: dict[URIRef, str] = {}
            for s, o in self.ann.subject_objects(WB.id):
                by_id[str(o)] = s  # type: ignore[assignment]
                by_iri[s] = str(o)  # type: ignore[index]
            self._index = (by_id, by_iri)
        return self._index

    def invalidate(self) -> None:
        self._index = None

    def iri(self, entity_id: str) -> URIRef | None:
        return self._ids()[0].get(entity_id)

    def id_of(self, iri: URIRef) -> str | None:
        return self._ids()[1].get(iri)

    def entity_ids(self) -> list[str]:
        return list(self._ids()[0])

    def register(self, entity_id: str, iri: URIRef | None = None) -> URIRef:
        iri = iri if iri is not None else self.ns[entity_id]
        old = self.id_of(iri)
        self.ann.set((iri, WB.id, Literal(entity_id)))
        by_id, by_iri = self._ids()  # keep the index current instead of rebuilding it
        if old is not None:
            by_id.pop(old, None)
        by_id[entity_id] = iri
        by_iri[iri] = entity_id
        return iri

    def unregister(self, entity_id: str) -> None:
        iri = self.iri(entity_id)
        if iri is not None:
            self.ann.remove((iri, None, None))
            by_id, by_iri = self._ids()
            by_id.pop(entity_id, None)
            by_iri.pop(iri, None)

    # ------------------------------------------------------------- annotations

    def locked_fields(self, iri: URIRef) -> set[str]:
        return {str(o) for o in self.ann.objects(iri, WB.locked)}

    def lock(self, iri: URIRef, fields: Iterable[str]) -> None:
        for f in fields:
            self.ann.add((iri, WB.locked, Literal(f)))

    def evidence(self, iri: URIRef) -> list[str]:
        return sorted(str(o) for o in self.ann.objects(iri, WB.evidence))

    def add_evidence(self, iri: URIRef, observation_ids: Iterable[str]) -> None:
        for o in observation_ids:
            self.ann.add((iri, WB.evidence, Literal(o)))

    # ------------------------------------------------------------ persistence

    def copy(self) -> "ProjectGraph":
        m, a = Graph(), Graph()
        for t in self.model:
            m.add(t)
        for t in self.ann:
            a.add(t)
        for p, n in self.model.namespaces():
            m.bind(p, n, override=True)
        return ProjectGraph(str(self.ns), m, a)

    def to_trig(self) -> bytes:
        ds = Dataset()
        for p, n in self.model.namespaces():
            ds.bind(p, n, override=True)
        ds.bind("wb", WB)
        gm = ds.graph(MODEL_GRAPH)
        for t in self.model:
            gm.add(t)
        ga = ds.graph(ANN_GRAPH)
        for t in self.ann:
            ga.add(t)
        return ds.serialize(format="trig", encoding="utf-8")

    @classmethod
    def from_trig(cls, namespace: str, data: bytes) -> "ProjectGraph":
        ds = Dataset()
        ds.parse(data=data, format="trig")
        m, a = Graph(), Graph()
        for t in ds.graph(MODEL_GRAPH):
            m.add(t)
        for t in ds.graph(ANN_GRAPH):
            a.add(t)
        for p, n in ds.namespaces():
            if p != "wb":
                m.bind(p, n, override=True)
        return cls(namespace, m, a)

    def export_turtle(self) -> bytes:
        return self.model.serialize(format="turtle", encoding="utf-8")


def model_shell(namespace: str, root_ontology: str, name: str) -> ProjectGraph:
    """An empty model with the ``owl:Ontology`` declaration BuildingMOTIF's
    ``Model.create`` produces, importing the configured vocabulary."""
    pg = ProjectGraph(namespace)
    onto = URIRef(namespace.rstrip("/#:"))
    pg.model.add((onto, RDF.type, OWL.Ontology))
    pg.model.add((onto, RDFS.label, Literal(name)))
    pg.model.add((onto, OWL.imports, URIRef(root_ontology)))
    return pg
