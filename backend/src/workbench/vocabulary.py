"""Ontology-facing services: vocabulary closure, term catalog, and validation.

Each project targets one vocabulary profile (WaTr, 223P or Brick; see config.py). For a
profile, BuildingMOTIF loads the sources the skill prescribes (``Library.from_ontology``)
and its OntoEnv integration fetches the ``owl:imports`` closure into a persistent cache.
Resolving the closure takes ~30 s the first time, so the merged closure is cached on disk
per profile and everything else is derived from that cache:

* a term catalog (equipment classes, point property classes, units, media, ...) used by
  the UI and by the agent's term search, and
* a ``shifty.PreparedValidator`` holding the parsed shapes, so validating a small
  project graph takes well under a second instead of re-resolving imports per call.

See docs/architecture.md, "Validation path", for why validation bypasses
``Model.validate`` while still using BuildingMOTIF's engine (pyshifty) and closure.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SH, SKOS

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .config import ProfileConfig

S223 = Namespace("http://data.ashrae.org/standard223#")
BRICK = Namespace("https://brickschema.org/schema/Brick#")
QUDT = Namespace("http://qudt.org/schema/qudt/")
QK = Namespace("http://qudt.org/vocab/quantitykind/")
UNIT = Namespace("http://qudt.org/vocab/unit/")

CATALOG_VERSION = 10

# Resolve one closure at a time (each downloads its sources and imports).
_RESOLVE_LOCK = threading.Lock()


def local_name(iri: str) -> str:
    """Display-only local name; never used to rebuild an IRI."""
    v = str(iri).rstrip("/#")
    return v.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def humanize(name: str) -> str:
    """ReverseOsmosisMembrane -> Reverse Osmosis Membrane; Fluid-Water -> Fluid: Water."""
    name = name.replace("_", " ")
    if "-" in name:
        head, _, tail = name.partition("-")
        return f"{humanize(tail)}" if head in {"Process", "EnumerationKind", "Role"} else f"{humanize(head)}: {humanize(tail)}"
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name).strip()


@dataclass
class Term:
    iri: str
    label: str
    kind: str
    comment: str = ""
    deprecated: bool = False
    abstract: bool = False
    # units only: quantity kinds the unit applies to; symbol
    quantity_kinds: list[str] = field(default_factory=list)
    symbol: str = ""


@dataclass
class Finding:
    """One validation finding, still in ontology terms. See issues.py for translation."""

    focus: str
    severity: str  # Violation | Warning | Info
    shape: str | None
    path: str | None
    value: str | None
    message: str
    constraint_kind: str
    statement_id: int | None = None  # pyshifty's stable statement id; joins findings to repair witnesses


@dataclass
class ValidationRun:
    conforms: bool
    findings: list[Finding]
    duration_s: float


class Vocabulary:
    """One profile's vocabulary, loaded once per process. Thread-safe for validation."""

    def __init__(self, profile: "ProfileConfig", cache_dir: Path):
        self.profile = profile
        self.family = profile.family
        self.cache_dir = cache_dir
        self.namespaces: dict[str, str] = {}
        self.terms: dict[str, Term] = {}
        self.by_kind: dict[str, list[str]] = defaultdict(list)
        self.ancestors: dict[str, list[str]] = {}
        self.qk_broader: dict[str, list[str]] = {}
        self._search_index: dict[str, tuple[set[str], str]] = {}  # iri -> (search words, "label name")
        self.root_ontology: str = ""
        self.missing_imports: list[str] = []
        self._validator = None
        self._shapes_bytes: bytes | None = None
        self._graph: Graph | None = None
        self._graph_lock = threading.Lock()
        self._validate_lock = threading.Lock()
        self._repair_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self.ready = threading.Event()
        self.loading = False
        self.error: str | None = None

    # ------------------------------------------------------------------ loading

    def cache_key(self) -> str:
        from importlib.metadata import version

        h = hashlib.sha256(json.dumps({
            "sources": self.profile.sources, "family": self.family, "catalog": CATALOG_VERSION,
            "buildingmotif": version("buildingmotif"),
        }, sort_keys=True).encode())
        return f"{self.profile.name}-{h.hexdigest()[:12]}"

    def load(self) -> None:
        with self._load_lock:
            if self.ready.is_set():
                return
            self.loading, self.error = True, None
            try:
                self._load()
                self.ready.set()
            except Exception as exc:  # surfaced through /api/status
                log.exception("vocabulary %s failed to load", self.profile.name)
                self.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.loading = False

    def _load(self) -> None:
        t0 = time.perf_counter()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        key = self.cache_key()
        closure_path = self.cache_dir / f"closure-{key}.ttl"
        catalog_path = self.cache_dir / f"catalog-{key}.json"
        meta_path = self.cache_dir / f"meta-{key}.json"

        if not (closure_path.exists() and meta_path.exists()):
            log.info("resolving the %s closure through BuildingMOTIF (one-time)", self.profile.name)
            closure, meta = self._resolve_closure()
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            tmp = closure_path.with_suffix(".tmp")
            tmp.write_bytes(closure.serialize(format="turtle", encoding="utf-8"))
            tmp.replace(closure_path)
            self._graph = closure
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.namespaces = meta["namespaces"]
        self.root_ontology = meta["root_ontology"]
        self.missing_imports = meta["missing_imports"]
        self._shapes_bytes = closure_path.read_bytes()

        import shifty

        self._validator = shifty.PreparedValidator(self._shapes_bytes)

        if catalog_path.exists():
            data = json.loads(catalog_path.read_text(encoding="utf-8"))
        else:
            data = self._build_catalog(self.graph(), set(meta["superseded"]))
            tmp = catalog_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(catalog_path)
        self._install_catalog(data)
        log.info("vocabulary %s ready in %.1fs (%d terms)", self.profile.name,
                 time.perf_counter() - t0, len(self.terms))

    def _resolve_closure(self) -> tuple[Graph, dict]:
        """Load the profile's sources with BuildingMOTIF and resolve their import closure.

        Also finds terms that only older members of a versioned ontology family define.
        223P imports QUDT 3.2.1 while WaTr imports QUDT 3.3.0, so a WaTr closure holds both;
        a unit present only in the older one is marked superseded.
        """
        from buildingmotif import BuildingMOTIF
        from buildingmotif.dataclasses import Library

        # All profiles share one persistent OntoEnv catalog and graph store, so shared
        # imports (223P for both the 223P and WaTr profiles, QUDT) download once. The two
        # must persist together: a persisted catalog attached to a fresh graph store
        # reports its ontologies as present but serves them as empty graphs.
        work = Path(tempfile.mkdtemp(prefix="wb-closure-"))
        with _RESOLVE_LOCK, BuildingMOTIF(
            f"sqlite:///{(work / 'bm.db').as_posix()}",
            ontology_cache_path=self.cache_dir / "ontoenv",
            graph_store_path=self.cache_dir / "ontoenv-graphs",
        ) as bm:
            env = bm.ontology_environment
            root = ""
            for source in self.profile.sources:
                resolved = Library._resolve_builtin(source) or source  # e.g. brick/Brick.ttl
                root = env.add(resolved, fetch_imports=True, overwrite=False)
            if not root or root.startswith("urn:unnamed"):
                raise RuntimeError(f"could not determine the ontology loaded from {self.profile.sources[-1]}")
            lib = type("Root", (), {"name": root})()
            missing = env.missing_imports(lib.name)
            if missing:
                log.warning("%s: imports not available online (ignored): %s", self.profile.name, missing)
            closure, names = env.closure_copy(lib.name)
            namespaces: dict[str, str] = {}
            for name in reversed(names):  # the root's own prefixes win
                try:
                    namespaces.update(env.env.get_namespaces(name))
                except Exception:  # noqa: BLE001 - prefixes are a convenience
                    pass
            log.info("closure: %d graphs, %d triples", len(names), len(closure))
            families: dict[str, list[tuple[tuple[int, ...], str]]] = defaultdict(list)
            for name in names:
                m = re.search(r"/(\d+(?:\.\d+)+)/", name)
                if m:
                    version = tuple(int(x) for x in m.group(1).split("."))
                    families[name.replace(m.group(0), "/*/")].append((version, name))
            superseded: set[str] = set()
            for members in families.values():
                if len(members) < 2:
                    continue
                members.sort()
                newest = {str(x) for x in env.graph_copy(members[-1][1]).subjects(RDF.type, None)}
                for _, older in members[:-1]:
                    olds = {str(x) for x in env.graph_copy(older).subjects(RDF.type, None)}
                    superseded |= olds - newest
            log.info("%d terms are only defined by superseded ontology versions", len(superseded))
            for prefix, ns in namespaces.items():
                if prefix:
                    closure.bind(prefix, ns, override=True)
            meta = {"root_ontology": lib.name, "namespaces": {k: v for k, v in namespaces.items() if k},
                    "missing_imports": sorted(missing), "superseded": sorted(superseded)}
            return closure, meta

    def graph(self) -> Graph:
        """The full closure as an rdflib graph (parsed lazily; ~10 s the first time)."""
        with self._graph_lock:
            if self._graph is None:
                assert self._shapes_bytes is not None
                self._graph = Graph().parse(data=self._shapes_bytes, format="turtle")
            return self._graph

    # ------------------------------------------------------------------ catalog

    def _build_catalog(self, g: Graph, superseded: set[str] | None = None) -> dict:
        superseded = superseded or set()
        parents: dict[str, set[str]] = defaultdict(set)
        for s, o in g.subject_objects(RDFS.subClassOf):
            if isinstance(s, URIRef) and isinstance(o, URIRef):
                parents[str(s)].add(str(o))

        def ancestors_of(c: str) -> list[str]:
            seen, stack = set(), [c]
            while stack:
                for p in parents.get(stack.pop(), ()):
                    if p not in seen:
                        seen.add(p)
                        stack.append(p)
            return sorted(seen)

        children: dict[str, set[str]] = defaultdict(set)
        for c, ps in parents.items():
            for p in ps:
                children[p].add(c)

        def descendants(root: URIRef) -> set[str]:
            seen, stack = set(), [str(root)]
            while stack:
                for c in children.get(stack.pop(), ()):
                    if c not in seen:
                        seen.add(c)
                        stack.append(c)
            return seen

        def text(node, *preds) -> str:
            for p in preds:
                for v in g.objects(URIRef(node), p):
                    if isinstance(v, Literal) and (v.language in (None, "en")):
                        return str(v)
            return ""

        def truthy(node, pred) -> bool:
            return any(bool(getattr(v, "toPython", lambda: v)()) is True or str(v) == "true"
                       for v in g.objects(URIRef(node), pred))

        watr = self.namespaces.get("watr") if self.family == "s223" else None
        terms: dict[str, dict] = {}

        def add(iri: str, kind: str) -> None:
            if iri in terms:
                return
            deprecated = (truthy(iri, OWL.deprecated) or truthy(iri, QUDT.deprecated)
                          or iri in superseded)
            terms[iri] = asdict(Term(
                iri=iri,
                label=text(iri, RDFS.label, SKOS.prefLabel) or humanize(local_name(iri)),
                kind=kind,
                comment=(text(iri, RDFS.comment, SKOS.definition, QUDT.plainTextDescription) or "")[:400],
                deprecated=deprecated,
                abstract=truthy(iri, S223.abstract),
            ))

        # Order matters: a term lands in the first group that claims it.
        if self.family == "brick":
            groups = {
                "point_class": descendants(BRICK.Point) | {str(BRICK.Point)},
                "equipment": descendants(BRICK.Equipment) | {str(BRICK.Equipment)},
                "location": descendants(BRICK.Location) | {str(BRICK.Location)},
            }
        else:
            # Sensors are s223:Equipment subclasses in 223P, and media are substances.
            sensors = descendants(S223.Sensor) | {str(S223.Sensor)}
            media = descendants(S223["Substance-Medium"])
            groups = {
                "sensor": sensors,
                "connection": descendants(S223.Connection),
                "equipment": (descendants(S223.Equipment) | {str(S223.Equipment)}) - sensors,
                "property": descendants(S223.Property) | {str(S223.Property)},
                "medium": media,
                "substance": descendants(S223["EnumerationKind-Substance"]) - media,
                "role": descendants(S223["EnumerationKind-Role"]),
                "enumeration": descendants(S223.EnumerationKind),
            }
            if watr:
                groups["process"] = descendants(URIRef(watr + "Process")) | {watr + "Process"}
        for kind, members in groups.items():
            for iri in members:
                add(iri, kind)

        qk_broader: dict[str, list[str]] = {}
        for qk in set(g.subjects(RDF.type, QUDT.QuantityKind)):
            if isinstance(qk, URIRef) and str(qk).startswith(str(QK)):
                add(str(qk), "quantity_kind")
                qk_broader[str(qk)] = sorted(
                    {str(b) for b in g.objects(qk, SKOS.broader) if isinstance(b, URIRef)})
        for u in set(g.subjects(RDF.type, QUDT.Unit)):
            if not (isinstance(u, URIRef) and str(u).startswith(str(UNIT))):
                continue
            add(str(u), "unit")
            terms[str(u)]["quantity_kinds"] = sorted(
                {str(q) for q in g.objects(u, QUDT.hasQuantityKind) if isinstance(q, URIRef)}
            )
            terms[str(u)]["symbol"] = text(str(u), QUDT.symbol, QUDT.abbreviation)

        return {
            "root_ontology": self.root_ontology,
            "terms": terms,
            "ancestors": {iri: ancestors_of(iri) for iri in terms
                          if terms[iri]["kind"] not in ("unit", "quantity_kind")},
            "qk_broader": qk_broader,
        }

    def _install_catalog(self, data: dict) -> None:
        self.terms = {iri: Term(**t) for iri, t in data["terms"].items()}
        self.ancestors = data["ancestors"]
        self.qk_broader = data.get("qk_broader", {})
        self.by_kind = defaultdict(list)
        for iri, t in sorted(self.terms.items(), key=lambda kv: kv[1].label.lower()):
            self.by_kind[t.kind].append(iri)
        self._search_index = {iri: _search_tokens(iri, t) for iri, t in self.terms.items()}

    # ------------------------------------------------------------------ lookups

    def term(self, iri: str) -> Term | None:
        return self.terms.get(str(iri))

    def label(self, iri: str | None) -> str:
        if iri is None:
            return ""
        t = self.terms.get(str(iri))
        return t.label if t else humanize(local_name(iri))

    def curie(self, iri: str) -> str:
        """A prefixed name using the namespaces declared by the loaded ontologies, else the IRI."""
        best = max(((p, ns) for p, ns in self.namespaces.items() if p and iri.startswith(ns) and len(iri) > len(ns)),
                   key=lambda pn: len(pn[1]), default=None)
        return f"{best[0]}:{iri[len(best[1]):]}" if best else iri

    def is_a(self, cls: str, ancestor: str) -> bool:
        return cls == ancestor or ancestor in self.ancestors.get(cls, ())

    def kind_of(self, iri: str) -> str | None:
        t = self.terms.get(str(iri))
        return t.kind if t else None

    def search(self, query: str, kinds: list[str] | None = None, limit: int = 12,
               include_deprecated: bool = False) -> list[Term]:
        """Rank terms for a free-text query.

        Whole-word and prefix matches on labels and local names (split at camel case,
        underscores and hyphens) count most; a term needs at least one matching word, and
        matching every word earns a bonus, so multi-word and single-word queries both work.
        A prefixed name or IRI that names a term directly returns that term first.
        """
        q = query.strip()
        exact: list[Term] = []
        if ":" in q and " " not in q:
            prefix, _, local = q.partition(":")
            ns = {**self.namespaces, "s223": str(S223), "brick": str(BRICK), "unit": str(UNIT),
                  "quantitykind": str(QK), "qk": str(QK)}.get(prefix)
            direct = self.terms.get(q) or (self.terms.get(ns + local) if ns else None)
            if direct and (not kinds or direct.kind in kinds):
                exact = [direct]
            q = local if ns else q
        words = [w for w in re.split(r"[^a-z0-9]+", q.lower()) if w]
        if not words:
            return exact[:limit]
        scored = []
        for iri, t in self.terms.items():
            if kinds and t.kind not in kinds:
                continue
            if (t.deprecated and not include_deprecated) or t.abstract or (exact and t is exact[0]):
                continue
            name = local_name(iri)
            tokens, label_text = self._search_index.get(iri) or _search_tokens(iri, t)
            score = 0.0
            matched = 0
            for w in words:
                if w in tokens:
                    score += 3
                    matched += 1
                elif any(tok.startswith(w) for tok in tokens):
                    score += 2
                    matched += 1
                elif len(w) > 3 and w in label_text:
                    score += 0.5
                    matched += 1
                elif len(w) > 3 and w in t.comment.lower():
                    score += 0.2
            if matched == 0:
                continue
            if matched == len(words):
                score += 3
            joined = " ".join(words)
            if joined in (t.label.lower(), name.lower().replace("_", " "), t.symbol.lower()):
                score += 10
            score -= len(tokens) / 50  # prefer the more general of equally good matches
            scored.append((score, t))
        scored.sort(key=lambda st: -st[0])
        return (exact + [t for _, t in scored])[:limit]

    def qk_lineage(self, quantity_kind: str) -> set[str]:
        """The quantity kind and everything broader than it (skos:broader*)."""
        seen, stack = {quantity_kind}, [quantity_kind]
        while stack:
            for b in self.qk_broader.get(stack.pop(), ()):
                if b not in seen:
                    seen.add(b)
                    stack.append(b)
        return seen

    def unit_fits(self, unit: str, quantity_kind: str) -> bool:
        """QUDT declares units against general kinds (PSI -> ForcePerArea, not Pressure)."""
        t = self.terms.get(unit)
        if t is None or not t.quantity_kinds:
            return True
        return bool(self.qk_lineage(quantity_kind) & set(t.quantity_kinds))

    def units_for(self, quantity_kind: str) -> list[Term]:
        return [self.terms[u] for u in self.by_kind["unit"]
                if not self.terms[u].deprecated and self.terms[u].quantity_kinds
                and self.unit_fits(u, quantity_kind)]

    def describe_class(self, iri: str) -> dict:
        """Parents and direct SHACL property constraints, like the skill's inspect script."""
        g = self.graph()
        node = URIRef(iri)
        t = self.term(iri)
        shapes = {node} if (node, RDF.type, SH.NodeShape) in g else set()
        shapes.update(g.subjects(SH.targetClass, node))
        constraints = []
        for shape in shapes:
            for prop in g.objects(shape, SH.property):
                c = {}
                for key, pred in (("path", SH.path), ("class", SH["class"]), ("node", SH.node),
                                  ("min", SH.minCount), ("max", SH.maxCount),
                                  ("qualified_min", SH.qualifiedMinCount),
                                  ("qualified_max", SH.qualifiedMaxCount),
                                  ("has_value", SH.hasValue), ("message", SH.message),
                                  ("name", SH.name)):
                    v = g.value(prop, pred)
                    if v is not None and not isinstance(v, BNode):
                        c[key] = str(v)
                qvs = g.value(prop, SH.qualifiedValueShape)
                if qvs is not None:
                    for key, pred in (("qualified_class", SH["class"]), ("qualified_node", SH.node)):
                        v = g.value(qvs, pred)
                        if isinstance(v, URIRef):
                            c[key] = str(v)
                if c:
                    constraints.append(c)
        return {
            "iri": iri,
            "label": t.label if t else local_name(iri),
            "comment": t.comment if t else "",
            "parents": [p for p in self.ancestors.get(iri, [])][:12],
            "constraints": constraints,
        }

    # --------------------------------------------------------------- validation

    def validate(self, data: Graph) -> ValidationRun:
        """Validate a model graph against the vocabulary closure (union mode)."""
        if self._validator is None:
            raise RuntimeError("vocabulary not loaded")
        t0 = time.perf_counter()
        payload = data.serialize(format="turtle", encoding="utf-8")
        with self._validate_lock:
            result = self._validator.validate_algebra(
                payload, graph_mode="union", minimum_severity="violation"
            )
        findings = []
        for v in result.violations:
            for r in v.reasons or [None]:
                findings.append(Finding(
                    focus=_strip_iri(v.focus_node),
                    severity=_severity(r.severity if r else v.severity),
                    shape=_strip_iri(v.shape_name) if v.shape_name else None,
                    path=_strip_iri(r.path) if r and r.path else None,
                    value=_strip_iri(r.value) if r and r.value else None,
                    message=(r.author_message or r.message) if r else "",
                    constraint_kind=str(r.constraint_kind) if r else "",
                    statement_id=v.statement_id,
                ))
        return ValidationRun(bool(result.conforms), findings, time.perf_counter() - t0)

    def repair_session(self, data: Graph):
        """pyshifty's algebraic repair session for a model graph (the engine BuildingMOTIF's
        ``AlgebraicValidationContext`` wraps, called directly). Reusable for witnesses and gating."""
        if self._shapes_bytes is None:
            raise RuntimeError("vocabulary not loaded")
        import shifty

        payload = data.serialize(format="turtle", encoding="utf-8")
        with self._repair_lock:
            return shifty.RepairSession(self._shapes_bytes, payload)

    def repair_witnesses(self, session) -> list[dict]:
        """Per failing (focus, statement): the failing leaves, the missing edges, the offending
        values and the repair tree's edits. The engine's own output; IRIs are left for the caller."""
        with self._repair_lock:
            out = []
            for w in session.witnesses():
                tree = w.repair_tree()
                blocked = tree.is_blocked() if callable(tree.is_blocked) else tree.is_blocked
                out.append({
                    "focus": _strip_iri(str(w.focus)),
                    "statement_id": w.statement_id,
                    "shape": _strip_iri(w.shape_name) if w.shape_name else None,
                    "blocked": bool(blocked),
                    "atoms": [{"kind": getattr(a.kind, "name", str(a.kind)), "path": a.path, "value": a.value,
                               "detail": a.detail} for a in w.summary()],
                    "missing": [{"node": _strip_iri(o.node), "path": o.path, "missing": o.missing,
                                 "observed": o.observed_count, "required": o.required_count}
                                for o in w.missing_obligations()],
                    "offending": [str(v) for v in w.offending_values()],
                    "repair": tree.explain(),
                })
        return out

    def gate(self, session, after: Graph) -> dict:
        """The repair engine's soundness gate for changing the session's model into ``after``:
        re-validates G ⊕ ΔG and diffs the violations. Sound = introduces nothing; progress =
        fixes something.

        ΔG is taken between the two models *after* SHACL-AF inference (a session per side):
        gating the raw edit would judge new nodes without the triples 223P's rules infer, and
        report violations that full validation does not.
        """
        import shifty

        after_session = self.repair_session(after)
        side = lambda g: g.serialize(format="turtle") if len(g) else None  # noqa: E731
        with self._repair_lock:
            g, g2 = session.to_graph(), after_session.to_graph()
            outcome = session.gate(shifty.delta_from_graph(add=side(g2 - g), delete=side(g - g2)))
        violation = lambda v: {"focus": _strip_iri(str(v.focus_node)),  # noqa: E731
                               "shape": _strip_iri(v.shape_name) if v.shape_name else None}
        return {"sound": bool(outcome.is_sound), "progress": bool(outcome.is_progress),
                "fixed": [violation(v) for v in outcome.fixed],
                "introduced": [violation(v) for v in outcome.introduced]}


def _search_tokens(iri: str, t: Term) -> tuple[set[str], str]:
    """The words a search query matches against, and the text it falls back to."""
    name = local_name(iri)
    label_tokens = _tokens(t.label)
    tokens = set(label_tokens) | set(_tokens(name)) | ({t.symbol.lower()} if t.symbol else set())
    if len(label_tokens) > 2:  # initials, so "AHU" finds "Air handling unit"
        tokens.add("".join(w[0] for w in label_tokens))
    return tokens, f"{t.label} {name}".lower()


def _tokens(text: str) -> list[str]:
    """Words of a label or local name: camel case, underscores and hyphens split."""
    # split before an Upper+lower run only, so "pHSensor" -> "pH Sensor", "ROSkid" -> "RO Skid"
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if w]


def _strip_iri(s: str | None) -> str | None:
    if s is None:
        return None
    s = str(s)
    return s[1:-1] if s.startswith("<") and s.endswith(">") else s


def _severity(s: str) -> str:
    s = str(s).rsplit("#", 1)[-1].rstrip(">")
    return {"violation": "Violation", "warning": "Warning", "info": "Info"}.get(s.lower(), s)


class VocabularyRegistry:
    """All configured profiles; each vocabulary loads on first use (or in the background)."""

    def __init__(self, profiles: dict[str, "ProfileConfig"], cache_dir: Path):
        self.vocabularies = {name: Vocabulary(p, cache_dir) for name, p in profiles.items()}

    def get(self, name: str, wait: float | None = 180) -> Vocabulary:
        """The loaded vocabulary. Loads it now if needed; raises if it failed or times out."""
        if name not in self.vocabularies:
            raise KeyError(f"unknown vocabulary {name!r}; configured: {sorted(self.vocabularies)}")
        v = self.vocabularies[name]
        if not v.ready.is_set():
            if not v.loading and v.error is None:
                v.load()
            elif not v.ready.wait(wait):
                raise TimeoutError(f"the {name} vocabulary is still loading")
        if v.error:
            raise RuntimeError(f"the {name} vocabulary failed to load: {v.error}")
        return v

    def load_in_background(self, name: str) -> None:
        v = self.vocabularies.get(name)
        if v is not None and not v.ready.is_set() and not v.loading:
            threading.Thread(target=_quiet_load, args=(v,), daemon=True, name=f"vocab-{name}").start()

    def status(self) -> list[dict]:
        return [{"name": n, "label": v.profile.label, "family": v.family,
                 "description": v.profile.description, "sources": v.profile.sources,
                 "ready": v.ready.is_set(), "loading": v.loading, "error": v.error,
                 "terms": len(v.terms), "root": v.root_ontology, "missing_imports": v.missing_imports}
                for n, v in self.vocabularies.items()]


def _quiet_load(v: Vocabulary) -> None:
    try:
        v.load()
    except Exception:  # recorded on v.error
        pass
