# Architecture

A local, single-user alpha: one Python process (FastAPI) serving a React app, a separate
inference server (llama.cpp `llama-server`, or a remote endpoint), and project directories on
disk.

```
Browser (React)                      Python backend (FastAPI, one process)
 ├ tables / graph / inspector  ──►   api.py ──► Project (mutation service) ──► store.py (SQLite + TriG files)
 ├ shared Selection            ──►     │            │  build_candidate → operations.resolve/apply
 └ assistant panel  ◄── SSE ───        │            │  validate → vocabulary.Vocabulary (pyshifty)
                                       │            └  publish (stale check, atomic snapshot, head move)
                                       └► runs.py (thread pool) ─► agent/correction.py ─► llm/* ─► llama-server / remote
                                                                      └ agent/tools.py (read-only) + agent/guidance.py (skill)
```

## Core rules

- **The RDF graph is authoritative.** Each revision is a full TriG snapshot holding the model
  graph (exported as-is) and an annotation graph (stable ids, human locks, evidence links).
  Tables and the graph view are projections (`projection.py`) recomputed per revision; they are
  never edited directly.
- **One mutation path.** Direct cell edits, accepted proposals and imports all go through
  `Project.build_candidate` → `Project.publish`. The agent can only build candidates.
- **Stable identifiers.** Every equipment/point/connection has an app-managed id (`eq-…`,
  `pt-…`, `cx-…`) stored as `wb:id` on its IRI. Imported entities keep their IRIs and get ids.
  Selections, proposals, evidence links, layout and issues all refer to ids, never to labels,
  row numbers or positions.
- **Observations are not assertions.** Source observations live in the `observations` table;
  model entities point to them through `wb:evidence`. (Populated by ingestion, slice 4.)
- **Human corrections are locked.** A field a person set (direct edit) or confirmed (applied
  proposal) gets `wb:locked`. Proposals that change a locked field show "overrides earlier edit";
  extraction (slice 4) must not overwrite locked fields silently.

## Vocabulary profiles

A project chooses its vocabulary when it is created (stored as `profile` in the project
metadata; older projects default to WaTr). Profiles are configured in `config.py` (and can be
extended in `workbench.toml`): a label, a **model family** that decides how equipment, points
and connections are written into RDF, and an ordered list of sources:

| Profile | Family | Sources (per the BuildingMOTIF skill) |
|---|---|---|
| `watr` | `s223` | `https://open223.info/223p.ttl`, then `https://watermetadata.org/watr-0.2.ttl` |
| `223p` | `s223` | `https://open223.info/223p.ttl` |
| `brick` | `brick` | BuildingMOTIF builtin `brick/Brick.ttl` |

Families share the operations API, the projection types and the UI; `projection.py` and
`operations.py` dispatch on the family, with the Brick implementation in `brick.py`.
Fields that do not exist in a family (medium or sensor type in Brick, point type in 223P) are
rejected with an explanation rather than ignored.

## Domain mapping: Brick

| UI | RDF |
|---|---|
| Equipment | instance of a `brick:Equipment` subclass; "part of" = `parent brick:hasPart child` |
| Point | instance of a `brick:Point` subclass (the **point type**, e.g. `brick:Zone_Air_Temperature_Sensor`); kind (sensor, setpoint, command, status, alarm, parameter) follows from the class; optional `brick:hasUnit` |
| Point → equipment | `equipment brick:hasPoint point` (`brick:isPointOf` is read too) |
| Connection | `upstream brick:feeds downstream`. An edge has no node, so its stable id lives in the annotation graph (`wb:source`, `wb:predicate`, `wb:target`); the exported model contains only the Brick triple |

## Domain mapping: WaTr / 223P

| UI | RDF |
|---|---|
| Equipment | instance of a WaTr/223P equipment class (sensors excluded); `watr:hasProcess` for treatment equipment |
| Point | an `s223:Property` — Measurement = `QuantifiableObservableProperty`, Setpoint/command value = `QuantifiableActuatableProperty`, Status = `EnumeratedObservableProperty`, On/off or mode command = `EnumeratedActuatableProperty`; `qudt:hasQuantityKind`, `qudt:hasUnit`, `s223:ofMedium` |
| Point → equipment | `equipment s223:hasProperty point` (+ `s223:actuatedByProperty` for actuatable) |
| Sensor type | a single-property sensor `<point>.sensor` that `s223:observes` the point, with `s223:hasObservationLocation` = the equipment |
| Connection ("Connected to") | an `s223:Pipe` (or other Connection) with `s223:cnx` to an outlet connection point on the upstream equipment and an inlet on the downstream one, each with `s223:hasMedium` |

## Operations (`operations.py`)

Typed pydantic models: `create/update/delete` × `equipment/point/connection`. Update operations
carry only the fields that change (explicit `null` clears). `resolve()` checks every id and
every vocabulary term (kind, abstract, existence) and mints ids for `new:*` placeholders,
reporting all problems at once. A resolved operation list applies deterministically, so
`apply_proposal` re-applies the stored operations and **refuses if the resulting triple diff
differs from the previewed diff**.

Malformed operations (unknown ids/terms, wrong kinds) cannot be applied. An incomplete model
(validation findings) is still a valid saved draft; findings become review issues.

## Revisions, undo, stale proposals

- `publish` runs under the project lock: check the base is still the head, write the snapshot
  atomically (temp file + rename, safe on Windows), insert the revision row and move `head` in
  one SQLite transaction. Validation findings are stored with the revision.
- Undo moves `head` to the parent and pushes onto a redo stack; any new revision clears redo.
  History is append-only.
- A proposal names its base revision. If the head moved, it is shown as stale and apply
  returns 409; the UI offers **Regenerate** (re-run the same instruction and selection on the
  current head). No automatic merging.

## Validation path (`vocabulary.py`)

For each profile, BuildingMOTIF's ontology environment (OntoEnv) adds the profile's sources in
order and fetches their `owl:imports` closure (once, ~30 s). All profiles share one persistent
OntoEnv catalog and graph store (`workbench-data/cache/ontoenv*`), so common imports download
once; the two must persist together (see branch-report.md). The merged closure is cached as
`workbench-data/cache/closure-<profile>-<key>.ttl`, with its prefixes and missing imports in
`meta-…json`. A term catalog
(equipment/sensor/process/medium classes, quantity kinds, units with their quantity kinds and
`skos:broader` lineage, deprecation/superseded flags) is cached alongside. Validation uses a
`shifty.PreparedValidator` over the cached shapes in `union` graph mode: ~0.5–0.7 s per run.
`issues.py` maps each finding's focus node to the owning entity (port → equipment, sensor →
point). The issue text is the validator's own message (IRIs shown as model labels or prefixed
names from the loaded ontologies); nothing is paraphrased. An issue's id is a hash of the
entity, shape, path and raw message, so it is independent of labels and display wording.
Issues are rebuilt from each revision's stored findings when read.

Dismissals live in `issue_dismissals` (who, reason, proposal). A dismissal that came with a
published change records that revision and applies only while it is in the head's history,
so undoing the change reopens the issue; other dismissals hold until reopened.

## The correction agent (`agent/correction.py`)

Bounded workflow, identical for local and remote models:

1. **Context**: selected rows (compact one-line form with ids and prefixed terms), related
   objects (owner equipment, attached points, touching connections), all equipment, open issues
   on the selection, evidence, and vocabulary hints matched from the instruction.
2. **Steps**: every model reply is JSON constrained by a schema (llama-server compiles it to a
   grammar): either a read-only tool call — `search_terms`, `units_for`, `describe_class`,
   `find_entities`, `read_evidence`, `read_guidance` — or `propose` with explanation, operations
   and questions. At most 8 steps.
3. **Candidate**: operations are resolved and applied to a copy of the base revision and
   validated. Malformed operations go back to the model with the problems and the closest valid
   terms (up to 2 repairs).
4. **Proposal**: stored with before/after per entity (every entity whose projection changes,
   flagged when outside the selection), the triple diff, the validation delta (issues resolved
   and introduced), evidence (model rows, observations, skill sections, vocabulary terms) and
   the explanation. No operations + questions = the assistant needs input.

Runs execute in a thread pool with cooperative cancellation (checked between steps and
between streamed tokens; closing the stream aborts generation in llama-server). Progress is
persisted on the run record and pushed over Server-Sent Events. Runs interrupted by a restart
are marked failed on next open.

## Model providers (`llm/`)

`complete_json(system, messages, schema, images?, cancel?)` behind two adapters:

- `openai` — any OpenAI-compatible `/v1/chat/completions` (llama-server by default,
  `response_format: json_schema`, streamed).
- `anthropic` — official SDK, `output_config.format` JSON schema, streamed, server-side refusal
  fallbacks on by default (`request_options = { fallbacks = false }` to disable). Not exercised
  in testing (no key was available); see README "Tested configuration".

## Storage layout

```
workbench-data/
  cache/                         per-profile closure/catalog/meta caches; shared OntoEnv store
  projects/<project-id>/
    project.sqlite               meta, revisions, proposals, corrections, sources, observations,
                                 issues, issue_dismissals, agent_runs, layout
    revisions/rev-N.trig
    sources/<source-id>/…
```

## Cross-platform notes

Pure Python + wheels for all native dependencies on Windows x64, macOS arm64 and Linux x64
(`pyshifty` has no macOS x86_64 wheel: Intel Macs need a Rust toolchain to build it). Paths
use `pathlib`; no fork/signals/Unix sockets; SQLite connections are per-transaction; snapshot
writes use `Path.replace`. The inference server is always a separate process reached over HTTP.
