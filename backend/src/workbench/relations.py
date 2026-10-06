"""Relations between entities: what counts as one fact, property paths over the model, and
virtual relations.

A *virtual relation* names a path over ontology relations, e.g. ``virtual:adjacent`` for
``rec:adjacentElement/^rec:adjacentElement`` (two rooms sharing a wall). Everything that takes
a relation takes a virtual one too: ``relate``/``unrelate``, ``Vocabulary.relations_for``, the
relationships listed for an entity, and view column paths. Nothing virtual is stored; a virtual
relation with ``via`` (a class for the node in the middle of a two-step path) can be set, and
``expand`` rewrites that into the stored relations before anything else sees the operations,
so a proposal holds only ordinary operations. Definitions are ``[virtual.<id>]`` tables in
``views.toml`` (curated) and workbench.toml::

    [virtual.adjacent]
    label = "adjacent to"
    families = ["brick"]
    path = "rec:adjacentElement/^rec:adjacentElement"
    via = "rec:Wall"                           # created when the two share none
    via_label = "Wall: {subject} | {object}"
    via_copy = []                              # relations the middle copies from the subject

The interface: ``relationship_key``/``relationship_id``, ``facts``, ``find``, ``Path``,
``expand``, and ``install`` (called once per vocabulary when it loads).
"""

from __future__ import annotations

import hashlib
import tomllib
from collections.abc import Iterator
from pathlib import Path as FilePath
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, ValidationError
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, RDFS

from .vocabulary import Term, expand_term

if TYPE_CHECKING:
    from .graph import ProjectGraph
    from .vocabulary import Vocabulary

VIRTUAL = "urn:workbench:virtual#"
# RDF/RDFS/OWL/SKOS predicates (rdfs:subClassOf, rdfs:comment...) are usable in paths too.
STANDARD = ("http://www.w3.org/1999/02/22-rdf-syntax-ns#", "http://www.w3.org/2000/01/rdf-schema#",
            "http://www.w3.org/2002/07/owl#", "http://www.w3.org/2004/02/skos/core#")
CURATED = FilePath(__file__).with_name("views.toml")
PLACEHOLDER = "new:"

Step = tuple[str, bool]  # (relation IRI, inverse)


# ------------------------------------------------------------- fact identity

def relationship_key(vocab: Vocabulary, s, p, o) -> tuple[str, str, str]:
    """One key for a fact however it is stated: symmetric pairs and inverse pairs coincide."""
    t = vocab.term(str(p))
    if t is not None and t.inverse and str(p) > t.inverse:
        s, p, o = o, URIRef(t.inverse), s
        t = vocab.term(str(p))
    if t is not None and t.symmetric and str(o) < str(s):
        s, o = o, s
    return str(s), str(p), str(o)


def relationship_id(key: tuple[str, str, str]) -> str:
    return "rl-" + hashlib.sha1("|".join(key).encode()).hexdigest()[:8]


def facts(pg: ProjectGraph, vocab: Vocabulary) -> Iterator[tuple[URIRef, URIRef, URIRef]]:
    """Every relation statement between nodes: stored ones, then virtual ones (``p`` in VIRTUAL)."""
    for s, p, o in pg.model:
        if isinstance(s, URIRef) and isinstance(o, URIRef) and vocab.kind_of(str(p)) == "relation":
            yield s, p, o  # type: ignore[misc]
    for v in vocab.virtual.values():
        for a, b in v.pairs(pg.model):
            yield a, URIRef(v.iri), b


def find(pg: ProjectGraph, vocab: Vocabulary, rid: str) -> tuple[URIRef, URIRef, URIRef] | None:
    """The fact behind a relationship id."""
    return next((f for f in facts(pg, vocab) if relationship_id(relationship_key(vocab, *f)) == rid), None)


# --------------------------------------------------------------------- paths

class PathError(ValueError):
    pass


class Path:
    """Prefixed names or <IRIs> joined by ``/`` (sequence) and ``|`` (alternative), ``^`` for an
    inverse step; any step may be a virtual relation."""

    def __init__(self, vocab: Vocabulary, alternatives: list[list[Step]]):
        self.vocab, self.alternatives = vocab, alternatives

    @classmethod
    def parse(cls, vocab: Vocabulary, text: str) -> Path:
        def step(t: str) -> Step:
            t = t.strip()
            inverse = t.startswith("^")
            name = t[1:].strip() if inverse else t
            iri = expand_term(vocab, name)
            if "://" not in iri and not iri.startswith("urn:"):
                raise PathError(f"unknown prefix in {name!r}")
            if vocab.term(iri) is None and vocab.kind_of(iri) is None and not iri.startswith(STANDARD):
                raise PathError(f"{vocab.curie(iri)} is not a relation in the loaded vocabulary")
            return iri, inverse

        alts = [[step(s) for s in alt.split("/") if s.strip()] for alt in text.split("|")]
        if not alts or any(not a for a in alts):
            raise PathError(f"empty path in {text!r}")
        return cls(vocab, alts)

    @property
    def single_step(self) -> Step | None:
        return self.alternatives[0][0] if len(self.alternatives) == 1 and len(self.alternatives[0]) == 1 else None

    @property
    def steps(self) -> list[Step]:
        return [s for alt in self.alternatives for s in alt]

    def values(self, g, node) -> list:
        """Nodes and literals the path reaches from ``node`` (never ``node`` itself), in a stable order."""
        found: dict = {}
        for alt in self.alternatives:
            nodes = [node]
            for pred, inverse in alt:
                nodes = list(dict.fromkeys(n for x in nodes for n in self._step(g, x, pred, inverse)))
            found.update(dict.fromkeys(nodes))
        found.pop(node, None)
        return sorted(found, key=str)

    def _step(self, g, node, pred: str, inverse: bool):
        v = self.vocab.virtual.get(pred)
        if v is not None:
            return v.ends(g, node, inverse)
        if inverse:
            return g.subjects(URIRef(pred), node)
        return g.objects(node, URIRef(pred)) if not isinstance(node, Literal) else ()


def _flip(steps: list[Step]) -> list[Step]:
    return [(p, not inv) for p, inv in reversed(steps)]


# ----------------------------------------------------------- virtual relations

class VirtualSpec(BaseModel):
    label: str
    inverse_label: str = ""
    description: str = ""
    families: list[str] = Field(default_factory=list)  # empty = every family
    profiles: list[str] = Field(default_factory=list)  # empty = every profile
    path: str
    via: str | None = None
    via_label: str = "{via}: {subject} | {object}"
    via_copy: list[str] = Field(default_factory=list)


class Virtual:
    """One virtual relation, bound to a vocabulary."""

    def __init__(self, vocab: Vocabulary, vid: str, spec: VirtualSpec):
        self.vocab, self.id, self.spec = vocab, vid, spec
        self.iri = VIRTUAL + vid
        self.path = Path.parse(vocab, spec.path)
        if any(p in vocab.virtual for p, _ in self.path.steps):
            raise PathError("a virtual relation's path cannot use other virtual relations")
        steps = self.path.alternatives[0]
        self.settable = spec.via is not None and len(self.path.alternatives) == 1 and len(steps) == 2
        self.symmetric = self.settable and steps[0][0] == steps[1][0] and steps[0][1] != steps[1][1]
        self.via = expand_term(vocab, spec.via) if spec.via else None
        if self.via and (vocab.term(self.via) is None or vocab.kind_of(self.via) != "class"):
            raise PathError(f"via {spec.via!r} is not a class an entity can have")
        if spec.via and not self.settable:
            raise PathError("only a two-step path can have a via class")
        self.via_copy = [expand_term(vocab, c) for c in spec.via_copy]

    # reading
    def ends(self, g, node, inverse: bool = False) -> list:
        path = Path(self.vocab, [_flip(alt) for alt in self.path.alternatives]) if inverse else self.path
        return path.values(g, node)

    def pairs(self, g) -> Iterator[tuple[URIRef, URIRef]]:
        starts: set = set()
        for alt in self.path.alternatives:
            pred, inverse = alt[0]
            starts |= set(g.objects(None, URIRef(pred)) if inverse else g.subjects(URIRef(pred), None))
        for a in sorted((s for s in starts if isinstance(s, URIRef)), key=str):
            for b in self.path.values(g, a):
                if isinstance(b, URIRef):
                    yield a, b

    def classes(self) -> tuple[list[str], list[str]]:
        """Classes a subject and an object can have, from the shapes of the path's end steps."""
        def side(step: Step, start: bool) -> set[str]:
            t = self.vocab.term(step[0])
            subject_side = start != step[1]  # a forward first step starts at the shape's subject
            return {c for sh in (t.shapes if t else []) for c in ([sh["subject"]] if subject_side else sh["objects"])}

        subj = set().union(*(side(alt[0], True) for alt in self.path.alternatives))
        obj = set().union(*(side(alt[-1], False) for alt in self.path.alternatives))
        return sorted(subj), sorted(obj)

    # writing (two-step paths through a ``via`` node)
    def middles(self, g, a, b) -> list:
        first, second = self.path.alternatives[0]
        return sorted(set(Path(self.vocab, [[first]]).values(g, a))
                      & set(Path(self.vocab, [_flip([second])]).values(g, b)), key=str)

    def links(self, a, m, b) -> list[tuple]:
        """The two stored triples that make a -> b through m."""
        (p1, inv1), (p2, inv2) = self.path.alternatives[0]
        return [(m, URIRef(p1), a) if inv1 else (a, URIRef(p1), m),
                (b, URIRef(p2), m) if inv2 else (m, URIRef(p2), b)]

    def others(self, g, m, a, b) -> list:
        """Nodes other than a and b the middle node relates through this path."""
        first, second = self.path.alternatives[0]
        back = Path(self.vocab, [_flip([first])]).values(g, m)
        forward = Path(self.vocab, [[second]]).values(g, m)
        return sorted({*back, *forward} - {a, b}, key=str)


def load_specs(config: dict[str, dict] | None = None) -> tuple[dict[str, VirtualSpec], list[str]]:
    """Curated [virtual.<id>] tables, then workbench.toml's merged over them by id."""
    tables = {k: dict(v) for k, v in tomllib.loads(CURATED.read_text(encoding="utf-8")).get("virtual", {}).items()}
    for vid, table in (config or {}).items():
        tables[vid] = {**tables.get(vid, {}), **table}
    specs, errors = {}, []
    for vid, table in tables.items():
        try:
            specs[vid] = VirtualSpec(**table)
        except ValidationError as exc:
            errors.append(f"virtual {vid}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                          for e in exc.errors()))
    return specs, errors


def install(vocab: Vocabulary, config: dict[str, dict] | None = None) -> None:
    """Give a loaded vocabulary the virtual relations for its family and profile, as terms of
    kind ``virtual`` (so labels, prefixed names and ``relations_for`` cover them)."""
    specs, errors = load_specs(config)
    vocab.namespaces.setdefault("virtual", VIRTUAL)
    vocab.virtual = {}
    for vid, spec in specs.items():
        if (spec.families and vocab.family not in spec.families) or (spec.profiles and vocab.profile.name not in spec.profiles):
            continue
        try:
            v = Virtual(vocab, vid, spec)
        except PathError as exc:
            errors.append(f"virtual {vid}: {exc}")
            continue
        vocab.virtual[v.iri] = v
        vocab.terms[v.iri] = Term(iri=v.iri, label=spec.label, kind="virtual", comment=spec.description,
                                  symmetric=v.symmetric)
        vocab.by_kind["virtual"].append(v.iri)
    vocab.virtual_errors = errors


# ----------------------------------------------------------------- expansion

def expand(pg: ProjectGraph, vocab: Vocabulary, ops: list[dict]) -> tuple[list[tuple[int, dict]], list[str]]:
    """Rewrite relate/unrelate on virtual relations into stored-relation operations.

    Returns (original operation index, operation) pairs and the problems found. Relating two
    entities that already share a middle node adds nothing; otherwise a ``via`` entity is
    created and linked. Unrelating removes both links and the middle node when nothing else
    uses it; a middle node that also links other entities is not split.
    """
    from .projection import entity_iri

    g = pg.model
    labels = {o["id"]: o.get("label") for o in ops if o["op"].startswith("create_") and o.get("id")}
    done: set[tuple[str, str, str]] = set()
    out: list[tuple[int, dict]] = []
    problems: list[str] = []

    def name(eid: str, node) -> str:
        return str(labels.get(eid) or (g.value(node, RDFS.label) if node is not None else None) or eid)

    for i, op in enumerate(ops):
        where = f"operation {i + 1} ({op['op']})"
        if op["op"] == "relate":
            v = vocab.virtual.get(expand_term(vocab, op["relation"]))
            if v is None:
                out.append((i, op))
                continue
            a, b = op["subject"], op["object"]
            na, nb = (None if x.startswith(PLACEHOLDER) else entity_iri(pg, x) for x in (a, b))
            missing = [x for x, n in ((a, na), (b, nb)) if n is None and x not in labels]
            if not v.settable:
                problems.append(f"{where}: virtual:{v.id} is read from the path {v.spec.path} and cannot be set; "
                                "relate the underlying relations instead")
                continue
            if missing or a == b:
                problems.append(f"{where}: virtual:{v.id} links two different entities in the model"
                                + (f"; not found: {', '.join(missing)}" if missing else ""))
                continue
            key = relationship_key(vocab, a, v.iri, b)
            if key in done or (na is not None and nb is not None and v.middles(g, na, nb)):
                continue  # already so
            done.add(key)
            mid = f"{PLACEHOLDER}~{v.id}-{len(done)}"
            label = v.spec.via_label.format(subject=name(a, na), object=name(b, nb), via=vocab.label(v.via))
            out.append((i, {"op": "create_entity", "id": mid, "label": label, "type": v.via}))
            for s, p, o in v.links(a, mid, b):
                out.append((i, {"op": "relate", "subject": str(s), "relation": str(p), "object": str(o)}))
            for rel in v.via_copy:  # e.g. a zone's domain onto its new domain space
                values = [str(x) for x in (g.objects(na, URIRef(rel)) if na is not None else ())]
                values += [o["object"] for o in ops[:i] if o["op"] == "relate" and o["subject"] == a
                           and expand_term(vocab, o["relation"]) == rel]
                for value in dict.fromkeys(values):
                    out.append((i, {"op": "relate", "subject": mid, "relation": rel, "object": value}))
        elif op["op"] == "unrelate" and (found := find(pg, vocab, op["id"])) and str(found[1]) in vocab.virtual:
            a, p, b = found
            v = vocab.virtual[str(p)]
            if not v.settable:
                problems.append(f"{where}: virtual:{v.id} is read from the path {v.spec.path}; "
                                "unrelate the underlying relations instead")
                continue
            for m in v.middles(g, a, b):
                others = v.others(g, m, a, b)
                if others:
                    problems.append(f"{where}: {name('', m)} also links {', '.join(name('', o) for o in others)}; "
                                    f"edit {name('', m)} itself instead")
                    continue
                links = v.links(a, m, b)
                for s, lp, o in links:
                    out.append((i, {"op": "unrelate", "id": relationship_id(relationship_key(vocab, s, lp, o))}))
                own = {RDF.type, RDFS.label, *map(URIRef, v.via_copy)}
                rest = [t for t in g.triples((m, None, None)) if t[1] not in own and t not in links]
                rest += [t for t in g.triples((None, None, m)) if t not in links]
                mid = pg.id_of(m)
                if not rest and mid and any(vocab.kind_of(str(t)) == "class" for t in g.objects(m, RDF.type)):
                    out.append((i, {"op": "delete_entity", "id": mid}))
        else:
            out.append((i, op))
    return out, problems
