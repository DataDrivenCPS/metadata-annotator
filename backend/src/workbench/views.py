"""Declarative table views over the model: rows are instances of ontology classes, columns are
property paths. Curated views ship in ``views.toml``; ``[views.<id>]`` tables in workbench.toml
add or override them (a table with the same id replaces fields of the curated one).

Paths: prefixed names or <IRIs> joined by ``/`` (sequence) and ``|`` (alternative), with ``^``
for an inverse step, e.g. ``rec:adjacentElement/^rec:adjacentElement``. ``label``, ``type`` and
``relations`` (a count of the row's relationships) are special columns. A view with ``builtin``
set adds its columns to one of the typed tables (points, equipment, spaces, connections,
connection_points) instead of being a table of its own.

A ``relation`` column (a single step, forward or inverse) is edited through relate/unrelate; other
columns are read-only, as are relations a typed editor owns (see projection.OWNED).
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from rdflib import Literal as RDFLiteral
from rdflib import URIRef
from rdflib.namespace import RDF
from rdflib.paths import AlternativePath, InvPath, SequencePath

from .graph import ProjectGraph
from .operations import expand_term
from .projection import ModelView, owned_hint, relationship_id, relationship_key
from .vocabulary import VALUE_KINDS, Vocabulary

CURATED = Path(__file__).with_name("views.toml")
BUILTINS = ("points", "equipment", "spaces", "connections", "connection_points")
SPECIAL = ("label", "type", "relations")


class ColumnSpec(BaseModel):
    key: str
    label: str
    path: str
    editor: Literal["label", "type", "relation", "none"] = "none"


class ViewSpec(BaseModel):
    id: str
    label: str
    families: list[str] = Field(default_factory=list)  # empty = every family
    profiles: list[str] = Field(default_factory=list)  # empty = every profile
    builtin: Literal["points", "equipment", "spaces", "connections", "connection_points"] | None = None
    rows: list[str] = Field(default_factory=list)  # root classes; "entity" = every generic entity
    columns: list[ColumnSpec] = Field(default_factory=list)

    def applies(self, family: str, profile: str) -> bool:
        return (not self.families or family in self.families) and (not self.profiles or profile in self.profiles)


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


# ------------------------------------------------------------------- paths

class PathError(ValueError):
    pass


def parse_path(vocab: Vocabulary, text: str):
    """A path string -> an rdflib path (or URIRef), resolving prefixed names with the vocabulary."""
    def term(t: str):
        t = t.strip()
        inverse = t.startswith("^")
        name = t[1:].strip() if inverse else t
        iri = expand_term(vocab, name)
        if "://" not in iri and not iri.startswith("urn:"):
            raise PathError(f"unknown prefix in {name!r}")
        ref = URIRef(iri)
        return InvPath(ref) if inverse else ref

    alts = []
    for alt in text.split("|"):
        steps = [term(s) for s in alt.split("/") if s.strip()]
        if not steps:
            raise PathError(f"empty path in {text!r}")
        alts.append(steps[0] if len(steps) == 1 else SequencePath(*steps))
    return alts[0] if len(alts) == 1 else AlternativePath(*alts)


def single_step(path) -> tuple[URIRef, bool] | None:
    """(predicate, inverse) for a one-step path, else None."""
    if isinstance(path, URIRef):
        return path, False
    if isinstance(path, InvPath) and isinstance(path.arg, URIRef):
        return path.arg, True
    return None


def check_spec(vocab: Vocabulary, spec: ViewSpec) -> list[str]:
    errors = []
    for root in spec.rows:
        if root != "entity" and vocab.term(expand_term(vocab, root)) is None:
            errors.append(f"row class {root!r} is not in the loaded vocabulary")
    for col in spec.columns:
        if col.path in SPECIAL:
            continue
        try:
            path = parse_path(vocab, col.path)
        except PathError as exc:
            errors.append(f"column {col.key}: {exc}")
            continue
        for step in _steps(path):
            if vocab.kind_of(str(step)) is None and vocab.term(str(step)) is None:
                errors.append(f"column {col.key}: {vocab.curie(str(step))} is not a relation in the loaded vocabulary")
        if col.editor == "relation" and single_step(path) is None:
            errors.append(f"column {col.key}: only a single (possibly inverse) relation can be edited")
    return errors


def _steps(path) -> list[URIRef]:
    if isinstance(path, URIRef):
        return [path]
    if isinstance(path, InvPath):
        return _steps(path.arg)
    if isinstance(path, (SequencePath, AlternativePath)):
        return [s for a in path.args for s in _steps(a)]
    return []


# --------------------------------------------------------------- evaluation

def _types(pg: ProjectGraph, node) -> list[str]:
    return [str(t) for t in pg.model.objects(node, RDF.type) if isinstance(t, URIRef)]


def evaluate(pg: ProjectGraph, vocab: Vocabulary, view: ModelView, spec: ViewSpec) -> dict[str, Any]:
    """Columns (with whether and how they can be edited) and rows with their cell values."""
    rows_by_iri = {r.iri: r for r in view.rows().values()  # type: ignore[attr-defined]
                   if r.kind != "relationship" and getattr(r, "iri", "")}  # type: ignore[attr-defined]
    roots = [expand_term(vocab, r) for r in spec.rows]
    if spec.builtin:
        rows = [r for r in getattr(view, spec.builtin)]
    else:
        rows = [r for r in rows_by_iri.values()
                if ("entity" in spec.rows and r.kind == "entity")
                or any(vocab.is_a(t, root) for t in _types(pg, URIRef(r.iri)) for root in roots if root != "entity")]
    counts: dict[str, int] = {}
    for rel in view.relationships:
        for ref in (rel.subject, rel.object):
            if ref is not None:
                counts[ref.id] = counts.get(ref.id, 0) + 1

    columns, parsed = [], {}
    for col in spec.columns:
        meta: dict[str, Any] = {"key": col.key, "label": col.label, "editor": "none"}
        if col.path in SPECIAL:
            meta["editor"] = col.editor if col.editor in ("label", "type") else "none"
        else:
            try:
                path = parse_path(vocab, col.path)
            except PathError:
                columns.append(meta)
                continue
            parsed[col.key] = path
            step = single_step(path)
            if col.editor == "relation" and step is not None:
                pred, inverse = step
                kinds = {r.kind for r in rows}  # type: ignore[attr-defined]
                owned = any(owned_hint(vocab, str(pred), k) for k in kinds) if not inverse else False
                if not owned:
                    meta.update(editor="relation", relation=str(pred), inverse=inverse,
                                relation_label=vocab.label(str(pred)), relation_curie=vocab.curie(str(pred)),
                                candidates=_candidates(pg, vocab, rows_by_iri, roots, str(pred), inverse))
        columns.append(meta)

    out_rows = []
    for r in rows:
        node = URIRef(r.iri)  # type: ignore[attr-defined]
        cells: dict[str, Any] = {}
        for col in spec.columns:
            if col.path == "label":
                cells[col.key] = [{"label": r.label}]  # type: ignore[attr-defined]
            elif col.path == "type":
                t = getattr(r, "type", None)
                cells[col.key] = [{"label": t.label, "iri": t.iri}] if t else []
            elif col.path == "relations":
                cells[col.key] = [{"label": str(counts.get(r.id, 0))}]  # type: ignore[attr-defined]
            elif col.key in parsed:
                cells[col.key] = _values(pg, vocab, rows_by_iri, node, parsed[col.key])
        out_rows.append({"id": r.id, "kind": r.kind, "label": r.label, "iri": r.iri, "cells": cells})  # type: ignore[attr-defined]
    return {"id": spec.id, "label": spec.label, "builtin": spec.builtin, "columns": columns, "rows": out_rows}


def _values(pg: ProjectGraph, vocab: Vocabulary, rows_by_iri: dict, node, path) -> list[dict]:
    step = single_step(path)
    out, seen = [], set()
    for value in pg.model.objects(node, path):
        if value == node or value in seen:
            continue
        seen.add(value)
        item: dict[str, Any]
        if isinstance(value, RDFLiteral):
            item = {"label": str(value)}
        elif str(value) in rows_by_iri:
            row = rows_by_iri[str(value)]
            item = {"id": row.id, "label": row.label, "kind": row.kind}
        elif vocab.term(str(value)):
            item = {"iri": str(value), "label": vocab.label(str(value)), "curie": vocab.curie(str(value))}
        else:
            item = {"iri": str(value), "label": vocab.curie(str(value))}
        if step is not None and isinstance(value, URIRef):  # removable: the relationship behind it
            pred, inverse = step
            s, o = (value, node) if inverse else (node, value)
            item["relationship"] = relationship_id(relationship_key(vocab, s, pred, o))
        out.append(item)
    return sorted(out, key=lambda i: i["label"].lower())


def _candidates(pg: ProjectGraph, vocab: Vocabulary, rows_by_iri: dict, roots: list[str], pred: str,
                inverse: bool) -> list[dict]:
    """Entities (or vocabulary values) a relation column can point at, per the shapes."""
    def fits(subject_types: list[str], object_types: list[str]) -> bool:
        for r in vocab.relations_for(subject_types):
            if r["relation"] == pred:
                return not r["objects"] or any(vocab.is_a(t, o) for t in object_types for o in r["objects"])
        return False

    root_types = [r for r in roots if r != "entity"]
    out = []
    for row in rows_by_iri.values():
        types = _types(pg, URIRef(row.iri))
        ok = fits(types, root_types) if inverse else fits(root_types, types)
        if ok:
            out.append({"id": row.id, "label": row.label, "kind": row.kind})
    if not inverse:
        objects = next((r["objects"] for r in vocab.relations_for(root_types) if r["relation"] == pred), [])
        if objects:
            out += [{"iri": t.iri, "label": t.label, "curie": vocab.curie(t.iri), "kind": "value"}
                    for t in vocab.terms.values() if t.kind in VALUE_KINDS and not t.deprecated
                    and any(vocab.is_a(t.iri, o) for o in objects)][:200]
    return sorted(out, key=lambda i: i["label"].lower())
