# What `gtf-buildingmotif` provides, and adjustments to the plan

Inspected: `NatLabRockies/BuildingMOTIF` branch `gtf-buildingmotif` at commit
`78b304aaec253e1d2d988fe834544e6904233693` (2026-09-02, "docs: record pyshifty 0.4.4 push
status"). The app depends on that exact commit (`backend/pyproject.toml`, `backend/uv.lock`);
the skill is vendored at the same commit in `backend/skill/` (`UPSTREAM.yml`).

## What the branch is

An integration branch combining unmerged feature branches (see its `BRANCHES.md`): OntoEnv-based
import resolution, the `pyshifty` 0.4.4 SHACL engine with algebraic validation and
soundness-gated repair, uv packaging, API cleanup/ergonomics, and an **agent skill**
(`.agents/skills/buildingmotif/`).

## The skill

`SKILL.md` + 14 reference files (~4,600 lines) + `scripts/inspect_ontology.py`. It is written
for a **coding agent that writes and runs Python**: "create one durable BuildingMOTIF script
early and use it as the executable record". Its load-bearing guidance:

| Area | Guidance used by the workbench |
|---|---|
| Evidence | "gap → evidence → user → apply"; never invent metadata; ask when ambiguous |
| Build | Prove a pattern on one representative instance, validate, then scale |
| Point lists | Map source tokens to verified terms; direct triples are the normal representation for heterogeneous point leaves (no template per CSV row) |
| 223P | Concrete connection points with media, joined by a Connection via `s223:cnx`; a sensor observes exactly one property, with an observation location; actuatable properties via `s223:actuatedByProperty`; enumerated properties need `s223:hasEnumerationKind` |
| WaTr | WaTr extends 223P; equipment needs its `watr:hasProcess`; media are values of `s223:hasMedium` |
| Terms | Verify every term; never rebuild an IRI from a local name; prefer non-deprecated terms (QUDT especially) |
| Validation | Never use `pyshacl`; report failures in domain language, keep the technical detail |
| Repair | Proposals are hypotheses; check `is_progress` and `reused_nodes`; minted nodes need evidence |

**Decision: wrapped functions, not Python execution.** The workbench does not let the model run
code. The skill's rules are compiled into typed domain operations (`operations.py`), its term
discovery becomes the `search_terms` / `units_for` / `describe_class` tools (the same
namespace-preserving logic as `inspect_ontology.py`), and its reference sections are
retrievable by topic (`agent/guidance.py`). Reasons: local models on llama-server are not
reliable code authors; arbitrary execution against the project store would bypass the
review/apply boundary; and the operations the UI needs are a small, closed set.

## BuildingMOTIF APIs used

- `BuildingMOTIF(...)` with a persistent `ontology_cache_path` and `graph_store_path`
- `Library.from_ontology(..., run_shacl_inference=False, infer_templates=False)`
- `bm.ontology_environment.closure_copy / graph_copy / missing_imports` (OntoEnv) to resolve the
  WaTr → 223P → QUDT closure
- the `pyshifty` engine (`shifty.PreparedValidator.validate_algebra`) for validation
- the `Model.create` convention for the model shell (`owl:Ontology` + `owl:imports`)

## Findings that changed the plan

1. **Validation cost.** `Model.validate` against the WaTr closure takes ~33–66 s per call
   (profiled: re-resolving imports twice, graph copies, re-serializing ~163k shape triples).
   Validation itself is ~0.5 s. The workbench resolves the closure once through BuildingMOTIF,
   caches it per vocabulary, and holds a `shifty.PreparedValidator`: **~0.5–0.7 s per
   validation**, fast enough to validate every candidate and every revision synchronously.
   Algebraic repair proposals (`ctx.witnesses[...].proposals()`) are not used yet for the same
   reason; see "Deferred".
2. **WaTr namespace moved.** The skill documents `urn:nawi-water-ontology#`; the current
   water-ontology (commit `41dcff2`, PR #45) uses `https://watermetadata.org/ontology/watr#`,
   and `https://watermetadata.org/water.ttl` returns 404. The app reads namespaces from the
   loaded ontology and never hardcodes either. The skill's WaTr examples are stale on this point.
3. **Imports.** 223P imports QUDT 3.2.1; WaTr imports QUDT 3.3.0. (An early version of the
   workbench vendored the whole closure; it now lets OntoEnv fetch it once — see item 9.)
4. **Duplicate QUDT versions.** Because both QUDT versions are in the closure, 434 terms exist
   only in the superseded version (e.g. `unit:MicroS-PER-CM` vs. current `unit:MicroS-PER-CentiM`).
   A real model picked the stale one in testing. The catalog now marks terms that only older
   members of a versioned ontology family define as superseded, and hides them from search.
5. **OntoEnv quirk.** `OntoEnv.connect()` does not scan `ontology_search_directories`; an explicit
   `env.update()` is required before loading. (Worth fixing upstream.)
6. **Sensors are equipment in 223P 1.0** (`s223:Sensor rdfs:subClassOf s223:Equipment`). The
   domain views treat the observed **property as the point** and show the sensor as an attribute
   of the point, not as an equipment row.
7. **Templates.** The WaTr template library (`water-ontology/libraries/templates`) is thin and has
   inconsistencies (e.g. `qudt:unit` vs `qudt:hasUnit`). Following the skill's own hybrid
   guidance, operations emit verified direct 223P triples rather than instantiating templates.
8. **CSV ingestion.** `CSVIngress` assumes a header row and drops coordinates, so the three CSV
   presets (slice 4) need their own deterministic parser; BuildingMOTIF's `label_parsing`
   combinators remain available for tag grammars.

## Findings from vocabulary profiles (later)

9. **Where each vocabulary comes from.** The workbench now loads vocabularies from the sources
   the skill names instead of a hand-assembled bundle: Brick from BuildingMOTIF's builtin
   `brick/Brick.ttl`, 223P from `https://open223.info/223p.ttl`, WaTr after 223P (the skill says
   to load 223P first; loaded alone, WaTr's import of 223P cannot be resolved online).
   The skill's WaTr URL `https://watermetadata.org/water.ttl` returns 404; the current release
   is `https://watermetadata.org/watr-0.2.ttl`. **Skill fix needed.**
10. **OntoEnv catalog/store mismatch (for ontoenv / BuildingMOTIF).** With a persistent
    `ontology_cache_path` but a fresh `graph_store_path`, a reopened environment reports its
    ontologies as present and does not re-fetch them, yet serves them as empty graphs. A WaTr
    closure then looks normal (18 graphs) but lacks all of 223P (125,450 vs 174,512 triples).
    The same cause makes `Library.from_ontology` name the library `urn:unnamed/` (the streamed
    graph has no `owl:Ontology`). Pure `ontoenv` without the external store behaves correctly.
    Suggested fixes: ontoenv re-fetches or raises when a catalog entry has no graph in the
    attached store; BuildingMOTIF defaults `graph_store_path` next to `ontology_cache_path`.
11. **223P drift.** The open223.info release differs from the 223P 1.0 modules shipped in the
    water-ontology repo: `EnumerationKind-RunStatus`/`-OnOff` are gone (`Binary-OnOff` remains),
    and new medium-compatibility rules flag equipment whose outlet medium differs from its inlet
    (e.g. an RO membrane: brackish in, freshwater and brine out). How WaTr should model
    medium-changing treatment equipment is an open question for the ontology.
12. **Brick imports not published online:** BACnet, Brick `ref`, REC `recimports`/`brickpatches`,
    QUDT 2.1 `usertest`/`shacl/datatype`. They ship only in BuildingMOTIF's repository
    (`libraries/brick/imports`); none define Brick classes or shapes, so they are skipped and
    reported in `/api/status`.

## Deferred / not yet used

- **Algebraic repair suggestions** as an agent tool. Needs a fast path comparable to the
  prepared validator (the current `AlgebraicValidationContext` construction re-resolves imports).
- **Knowledge service** (`bm.knowledge`, Docling/Qdrant): not needed for point lists and diagrams.
- **MCP**: the agent tools are plain typed functions (`agent/tools.py`) and can be exposed later.
