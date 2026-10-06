"""Declarative table views over the model: rows are instances of ontology classes, columns are
property paths. Curated views ship in ``views.toml``; ``[views.<id>]`` tables in workbench.toml
add or override them (a table with the same id replaces fields of the curated one).

Paths (relations.Path): prefixed names or <IRIs> joined by ``/`` (sequence) and ``|``
(alternative), with ``^`` for an inverse step, e.g. ``s223:hasDomainSpace/^s223:encloses``; a step
can be a virtual relation (``virtual:adjacent``, see relations.py). ``label``, ``type`` and
``relations`` (a count of the row's relationships) are special columns. A view with ``builtin``
set adds its columns to one of the typed tables (points, equipment, spaces, connections,
connection_points) instead of being a table of its own.

A ``relation`` column (a single step, forward or inverse, stored or virtual) is edited through
relate/unrelate; other columns are read-only.
"""

from __future__ import annotations

import tomllib
from pathlib import Path as FilePath
from typing import Any, Literal

from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import RDF

from .graph import ProjectGraph
from .projection import ModelView
from .relations import Path, PathError, relationship_id, relationship_key
from .vocabulary import VALUE_KINDS, Vocabulary, expand_term

CURATED = FilePath(__file__).with_name("views.toml")
BUILTINS = ("points", "equipment", "spaces", "connections", "connection_points")
SPECIAL = ("label", "type", "relations")


Typed = Literal["points", "equipment", "spaces", "connections", "connection_points"]


class ColumnSpec(BaseModel):
    key: str
    label: str
    path: str
    editor: Literal["label", "type", "relation", "none"] = "none"
    # "vocabulary": follow the path through the loaded ontologies (a term's parent, description...)
    source: Literal["model", "vocabulary"] = "model"
    only: str | None = None  # keep values (and edit candidates) of this class, e.g. s223:Connection


class ViewSpec(BaseModel):
    id: str
    label: str
    families: list[str] = Field(default_factory=list)  # empty = every family
    profiles: list[str] = Field(default_factory=list)  # empty = every profile
    exclude_profiles: list[str] = Field(default_factory=list)  # hidden for these profiles
    table: Typed | None = None  # this tab is a typed table (its own component)
    builtin: Typed | None = None  # these columns extend a typed table
    rows: list[str] = Field(default_factory=list)  # root classes; "entity" = every generic entity
    terms: str | None = None  # rows are the vocabulary terms under this class (e.g. watr:Process)
    columns: list[ColumnSpec] = Field(default_factory=list)

    def applies(self, family: str, profile: str) -> bool:
        return ((not self.families or family in self.families) and (not self.profiles or profile in self.profiles)
                and profile not in self.exclude_profiles)


@dataclass
class TermRow:
    """A vocabulary term as a view row (``terms`` views)."""
    id: str
    iri: str
    label: str
    kind: str = "term"


def load_specs(config_views: dict[str, dict] | None = None) -> tuple[list[ViewSpec], list[str]]:
    """Curated views, then workbench.toml's [views.<id>] merged over them by id."""
    tables: dict[str, dict] = {k: dict(v) for k, v in tomllib.loads(CURATED.read_text(encoding="utf-8")).get("views", {}).items()}
    for vid, table in (config_views or {}).items():
        tables[vid] = {**tables.get(vid, {}), **table}
    specs, errors = [], []
    for vid, table in tables.items():
        try:
            specs.append(ViewSpec(id=vid, **table))
        except ValidationError as exc:
            errors.append(f"view {vid}: " + "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()))
    return specs, errors


def for_project(specs: list[ViewSpec], family: str, profile: str) -> list[ViewSpec]:
    """The views a project shows; every spec extending the same typed table becomes one."""
    out: list[ViewSpec] = []
    extending: dict[str, ViewSpec] = {}
    for spec in (s for s in specs if s.applies(family, profile)):
        if spec.builtin and spec.builtin in extending:
            extending[spec.builtin].columns += spec.columns
            continue
        spec = spec.model_copy(deep=True)
        if spec.builtin:
            extending[spec.builtin] = spec
        out.append(spec)
    return out


def check_spec(vocab: Vocabulary, spec: ViewSpec) -> list[str]:
    errors = []
    for root in [*spec.rows, *([spec.terms] if spec.terms else [])]:
        if root != "entity" and vocab.term(expand_term(vocab, root)) is None:
            errors.append(f"row class {root!r} is not in the loaded vocabulary")
    for col in spec.columns:
        if col.path in SPECIAL:
            continue
        try:
            path = Path.parse(vocab, col.path)
        except PathError as exc:
            errors.append(f"column {col.key}: {exc}")
            continue
        if col.only and vocab.term(expand_term(vocab, col.only)) is None:
            errors.append(f"column {col.key}: only {col.only!r} is not a class in the loaded vocabulary")
        if col.editor == "relation" and path.single_step is None:
            errors.append(f"column {col.key}: only a single (possibly inverse) relation can be edited")
    return errors


# --------------------------------------------------------------- evaluation

def evaluate(pg: ProjectGraph, vocab: Vocabulary, view: ModelView, spec: ViewSpec) -> dict[str, Any]:
    """Columns (with whether and how they can be edited) and rows with their cell values."""
    entities = {r.iri: r for r in view.rows().values()  # type: ignore[attr-defined]
                if r.kind != "relationship" and getattr(r, "iri", "")}  # type: ignore[attr-defined]
    roots = [expand_term(vocab, r) for r in spec.rows if r != "entity"]
    if spec.builtin or spec.table:
        rows = list(getattr(view, spec.builtin or spec.table))  # type: ignore[arg-type]
    elif spec.terms:
        root = expand_term(vocab, spec.terms)
        rows = [TermRow(t.iri, t.iri, t.label) for t in sorted(vocab.terms.values(), key=lambda t: t.label.lower())
                if t.iri != root and not t.deprecated and vocab.is_a(t.iri, root)]
    else:
        rows = [r for r in entities.values()
                if ("entity" in spec.rows and r.kind == "entity")
                or any(vocab.is_a(t, root) for t in _types(pg, URIRef(r.iri)) for root in roots)]
    # a term row's "type" is the term itself (a process class is what a hasProcess value must be)
    row_types = sorted({t for r in rows for t in ([r.iri] if isinstance(r, TermRow) else _types(pg, URIRef(r.iri)))}
                       | set(roots))  # type: ignore[attr-defined]
    counts: dict[str, int] = {}
    for rel in (r for r in view.relationships if not r.virtual):  # stored facts only
        for ref in (rel.subject, rel.object):
            if ref is not None:
                counts[ref.id] = counts.get(ref.id, 0) + 1

    columns, paths = [], {}
    for col in spec.columns:
        meta: dict[str, Any] = {"key": col.key, "label": col.label, "editor": "none"}
        if col.path in SPECIAL:
            meta["editor"] = col.editor if col.editor in ("label", "type") else "none"
        else:
            try:
                path = paths[col.key] = Path.parse(vocab, col.path)
            except PathError:
                columns.append(meta)
                continue
            step = path.single_step
            if col.editor == "relation" and step is not None and col.source == "model":
                pred, inverse = step
                candidates = _candidates(pg, vocab, entities, row_types, pred, inverse)
                meta.update(editor="relation", relation=pred, inverse=inverse,
                            relation_label=vocab.label(pred), relation_curie=vocab.curie(pred),
                            candidates=[c for c in candidates if _of_class(pg, vocab, c, col.only)])
        columns.append(meta)

    out_rows = []
    for r in rows:
        node = URIRef(r.iri)  # type: ignore[attr-defined]
        cells: dict[str, Any] = {}
        for col in spec.columns:
            if col.path == "label":
                cells[col.key] = [{"label": r.label}]  # type: ignore[attr-defined]
            elif col.path == "type":
                t = getattr(r, "type", None)  # (term rows have none)
                cells[col.key] = [{"label": t.label, "iri": t.iri}] if t else []
            elif col.path == "relations":
                cells[col.key] = [{"label": str(counts.get(r.id, 0))}]  # type: ignore[attr-defined]
            elif col.key in paths:
                graph = vocab.graph() if col.source == "vocabulary" else pg.model
                cells[col.key] = [v for v in _values(graph, vocab, entities, node, paths[col.key], col.source == "model")
                                  if _of_class(pg, vocab, v, col.only)]
        out_rows.append({"id": r.id, "kind": r.kind, "label": r.label, "iri": r.iri, "cells": cells})  # type: ignore[attr-defined]
    return {"id": spec.id, "label": spec.label, "builtin": spec.builtin, "columns": columns, "rows": out_rows}


def _types(pg: ProjectGraph, node) -> list[str]:
    return [str(t) for t in pg.model.objects(node, RDF.type) if isinstance(t, URIRef)]


def _values(graph, vocab: Vocabulary, entities: dict, node, path: Path, editable: bool = True) -> list[dict]:
    step = path.single_step if editable else None
    out = []
    for value in path.values(graph, node):
        item: dict[str, Any]
        if isinstance(value, RDFLiteral):
            item = {"label": str(value)}
        elif str(value) in entities:
            row = entities[str(value)]
            item = {"id": row.id, "label": row.label, "kind": row.kind}
        elif vocab.term(str(value)):
            item = {"iri": str(value), "label": vocab.label(str(value)), "curie": vocab.curie(str(value))}
        else:
            item = {"iri": str(value), "label": vocab.curie(str(value))}
        if step is not None and isinstance(value, URIRef):  # removable: the relationship behind it
            pred, inverse = step
            s, o = (value, node) if inverse else (node, value)
            item["relationship"] = relationship_id(relationship_key(vocab, s, URIRef(pred), o))
        out.append(item)
    return sorted(out, key=lambda i: i["label"].lower())


def _of_class(pg: ProjectGraph, vocab: Vocabulary, item: dict, only: str | None) -> bool:
    """Whether a cell value or candidate is of the column's ``only`` class (entities by their types)."""
    if not only:
        return True
    cls = expand_term(vocab, only)
    if item.get("id") and item.get("kind") != "value":
        node = pg.iri(item["id"])
        return node is not None and any(vocab.is_a(t, cls) for t in _types(pg, node))
    return bool(item.get("iri")) and vocab.is_a(item["iri"], cls)


def _candidates(pg: ProjectGraph, vocab: Vocabulary, entities: dict, row_types: list[str], pred: str,
                inverse: bool) -> list[dict]:
    """Entities (or vocabulary values) a relation column can point at; ``fits`` per the shapes."""
    def objects(subject_types: list[str]) -> list[str] | None:
        """The relation's object classes for such a subject ([] = anything), None if it has no such relation."""
        return next((r["objects"] for r in vocab.relations_for(subject_types) if r["relation"] == pred), None)

    def fits(types: list[str], classes: list[str] | None) -> bool:
        return classes is not None and (not classes or any(vocab.is_a(t, c) for t in types for c in classes))

    forward = objects(row_types)
    out = []
    for row in entities.values():  # every entity; validation reports misfits, so fitting ones come first
        types = _types(pg, URIRef(row.iri))
        ok = fits(row_types, objects(types)) if inverse else fits(types, forward)
        out.append({"id": row.id, "label": row.label, "kind": row.kind, "fits": ok})
    if not inverse and forward:
        out += [{"iri": t.iri, "label": t.label, "curie": vocab.curie(t.iri), "kind": "value", "fits": True}
                for t in vocab.terms.values() if t.kind in VALUE_KINDS and not t.deprecated
                and any(vocab.is_a(t.iri, o) for o in forward)][:200]
    return sorted(out, key=lambda i: (not i["fits"], i["label"].lower()))
