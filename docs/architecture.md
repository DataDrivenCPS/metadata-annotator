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
  `Project.build_candidate` → `Project.publish`. The agent can only build candidates; only
  auto-fix (below) applies one without a person, and only after its checks pass.
- **Stable identifiers.** Every equipment/point/connection/connection point/space has an
  app-managed id (`eq-…`, `pt-…`, `cx-…`, `cp-…`, `sp-…`) stored as `wb:id` on its IRI. Imported
  entities keep their IRIs and get ids. Connection points minted with a connection (and those
  in older snapshots or imports) are not registered until an operation touches them; until
  then their id is derived from the IRI (`cp-` + sha1), so it is the same in every revision.
  Selections, proposals, evidence links, layout and issues all refer to ids, never to labels,
  row numbers or positions.
- **Observations are not assertions.** Source observations live in the `observations` table;
  model entities point to them through `wb:evidence`. (Populated by ingestion, slice 4.)
- **Human corrections are locked.** A field a person set (direct edit) or confirmed (applied
  proposal) gets `wb:locked`; automatic fixes lock nothing. Proposals that change a locked field show "overrides earlier edit";
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
| `brick` | `brick` | Brick nightly `https://github.com/BrickSchema/Brick/releases/download/nightly/Brick.ttl` (1.5.x, with RealEstateCore); cached once under its URL — delete `cache/*brick*` to refresh |

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
| Space | instance of a RealEstateCore space class (`rec:Building`, `rec:Level`, `rec:Room`…; Brick's own locations are deprecated for these); "part of" = `child rec:isPartOf parent` (`rec:hasPart`, `brick:isPartOf` read too) |
| Equipment → space | `equipment brick:hasLocation space` (`brick:isLocationOf` read too); points take no location |
| Connection | `upstream brick:feeds downstream`. An edge has no node, so its stable id lives in the annotation graph (`wb:source`, `wb:predicate`, `wb:target`); the exported model contains only the Brick triple |

## Domain mapping: WaTr / 223P

| UI | RDF |
|---|---|
| Equipment | instance of a WaTr/223P equipment class (sensors excluded); `watr:hasProcess` for treatment equipment |
| Point | an `s223:Property` — Measurement = `QuantifiableObservableProperty`, Setpoint/command value = `QuantifiableActuatableProperty`, Status = `EnumeratedObservableProperty`, On/off or mode command = `EnumeratedActuatableProperty`; `qudt:hasQuantityKind`, `qudt:hasUnit`, `s223:ofMedium` |
| Point → equipment | `equipment s223:hasProperty point` (+ `s223:actuatedByProperty` for actuatable) |
| Space | an `s223:PhysicalSpace`; "part of" = `parent s223:contains child` |
| Equipment → space | `equipment s223:hasPhysicalLocation space` |
| Sensor type | a single-property sensor `<point>.sensor` that `s223:observes` the point, with `s223:hasObservationLocation` = the equipment |
| Connection ("Connected to") | an `s223:Pipe` (or other Connection) with `s223:cnx` to an outlet connection point on the upstream equipment and an inlet on the downstream one, each with `s223:hasMedium`. Without `from_point`/`to_point` the connection mints its own ports (`<cx>.out`, `<cx>.in`) |
| Connection point | `s223:InletConnectionPoint` / `OutletConnectionPoint` / `BidirectionalConnectionPoint` with `s223:hasMedium`, owned via `equipment s223:hasConnectionPoint port`; **paired with** = `s223:pairedConnectionPoint` (asserted both ways; same equipment, inlet ↔ outlet); **maps to** = `port s223:mapsTo container-port`, where the container `s223:contains` the port's equipment (one-to-one both ways). Not in Brick |

## Operations (`operations.py`)

Typed pydantic models: `create/update/delete` × `equipment/point/connection/connection_point/space`
(equipment also takes a `location` space).
Connections can join existing connection points (`from_point`/`to_point`); re-pointing an end
drops the old port only if it was minted for that connection and never became a connection
point of its own (id, pairing, mapsTo). Update operations carry only the fields that change
(explicit `null` clears). `resolve()` checks every id and
every vocabulary term (kind, abstract, existence) and mints ids for `new:*` placeholders,
reporting all problems at once. 223P's connection-point rules (pairing, mapsTo, one
connection per port) are checked on the result after all operations apply, so a proposal can
create and wire ports in one go. A resolved operation list applies deterministically, so
`apply_proposal` re-applies the stored operations and **refuses if the resulting triple diff
differs from the previewed diff**.

Malformed operations (unknown ids/terms, wrong kinds) cannot be applied. An incomplete model
(validation findings) is still a valid saved draft; findings become review issues.

### Generic entities and relationships

Anything the typed editors don't cover is still modelable, using only what the loaded
ontologies define:

- **Entities** (`en-…`): an instance of any ontology class, created with `create_entity`
  (`rec:Wall`, `s223:Zone`, `brick:System`, but also a `rec:HVACZone` or a piece of equipment).
  The node is whatever its class makes it (a space, equipment…), so later operations and the
  typed tables treat it that way; the typed operations remain shortcuts that also write the
  parts a pattern needs (a 223P point's sensor, connection points…). `update_entity` /
  `delete_entity` work on any node.
- **Relationships** (`rl-…`, derived from the triple like connection point ids, so stable without
  a registry): `relate {subject, relation, object}` / `unrelate {id}` for any relation of the
  loaded ontologies between any two entities, or to a vocabulary term (e.g. `s223:Domain-HVAC`).
  Symmetric and inverse statements are one fact. Only the relation itself must exist in the
  vocabulary.
- **Connect anything, validate after.** What may relate to what comes from SHACL property
  shapes (none of the vocabularies declares `rdfs:domain`/`range`); the catalog records each
  relation's shapes (subject class, object classes from `sh:class`/`sh:or`/`sh:node`/
  `sh:qualifiedValueShape`, `sh:maxCount`) and `relations_for(types)` lists them. They guide,
  never refuse: pickers offer fitting objects first (`fits`) and everything else after; a
  relation that does not fit gets a note on the proposal (with a bridge such as "Office 1
  reaches a s223:DomainSpace through s223:encloses"), the assistant sees those notes once, and
  SHACL validation reports actual violations. A `maxCount 1` relation replaces its value, with
  a note.
- **Typed fields** (`projection.TYPED_FIELDS`): relations the typed tables show as fields
  (`hasPoint`, `feeds`, `contains`, `hasLocation`, `rec:isPartOf`…). Display only: a fact is
  listed as a relationship unless a typed row already shows that pair (`shown_pairs`), so a
  room's second parent or a VAV feeding a zone appear as relationships while the room's level
  stays its `part_of`.
- The inspector's **Relationships** panel lists an entity's relations and adds any relation
  the vocabulary has for it. The assistant has the same operations and a `relations_for(entity_id)`
  tool.

### Virtual relations (`relations.py`)

`relations.py` is the one module for relations between entities: fact identity
(`relationship_key`/`relationship_id`), every fact in a model (`facts`, stored and virtual),
property paths (`Path.parse(vocab, text).values(graph, node)`), and virtual relations.

A **virtual relation** names a path over ontology relations and is used wherever a relation
is: `relate`/`unrelate`, `relations_for` (so the inspector and the assistant offer it), the
relationships of an entity (rows with `virtual: true`), view columns and the graph. Nothing
virtual is stored. Definitions are `[virtual.<id>]` tables (curated in `views.toml`, extended
or overridden in `workbench.toml`), installed on each vocabulary as terms of kind `virtual`
(`virtual:<id>`, IRI `urn:workbench:virtual#<id>`); which entities can take one comes from the
shapes of its path's first and last steps.

A two-step path with a `via` class can be set. `resolve` first calls `relations.expand`,
which rewrites relate/unrelate on virtual relations into ordinary operations, so a proposal
holds only stored-fact operations (and a resolved list resolves to itself):

- relate A→B: nothing if A and B already share a middle node; otherwise `create_entity` of the
  `via` class (labelled from `via_label`), the two relates, and relates copying the subject's
  `via_copy` relations onto the middle node.
- unrelate: both links, plus `delete_entity` of a middle node nothing else uses; a middle
  node that also links other entities is not split (the edit is refused).

Curated: `virtual:adjacent` (Brick/RealEstateCore: rooms are adjacent when both are
`rec:adjacentElement` of one element; `via = "rec:Wall"`) and `virtual:serves_space` (223P: a
zone `hasDomainSpace` a domain space the physical space `encloses`; `via = "s223:DomainSpace"`,
copying the zone's `s223:hasDomain`).

### Table views (`views.py`, `views.toml`)

A project's tabs are its views, in `views.toml` order, filtered by `families`, `profiles` and
`exclude_profiles` (Graph and Issues always follow). The typed tables are views too
(`table = "points"` etc.), so a profile can drop one: WaTr hides Spaces, Zones and Domain spaces
and shows Processes and Media; Brick has no Connection points.

Other views are declarative tables:
- **Rows** are instances of ontology classes (`rows = ["s223:Zone"]`, or `"entity"` for every
  generic entity), or the vocabulary terms under a class (`terms = "watr:Process"`: every
  process, used or not; a term row's id is its IRI, and the inspector shows the term).
- **Columns** are property paths (`s223:hasDomainSpace`, `^s223:encloses`,
  `s223:hasDomainSpace/^s223:encloses`, `a|b`, virtual relations such as
  `virtual:serves_space`, and RDF/RDFS/OWL/SKOS predicates; specials `label`, `type`,
  `relations`), followed through the model or, with `source = "vocabulary"`, through the
  ontologies (a process's parent `rdfs:subClassOf`, its `rdfs:comment`, a medium's
  `s223:composedOf/s223:ofConstituent`). `only = "<class>"` keeps the values (and edit
  candidates) of that class, e.g. the connections, not connection points, carrying a medium.
- A single-step (possibly inverse, possibly virtual) model `relation` column is edited with
  relate/unrelate — each cell value carries its relationship id, and the column lists
  candidate objects (those the shapes expect first). Processes' Equipment column
  (`^watr:hasProcess`) assigns equipment to a process from the process's row.
- A view with `builtin` adds its columns to a typed table; all such views for one table are
  merged (`views.for_project`).

`[views.<id>]` in `workbench.toml` adds views or overrides fields of curated ones (e.g.
`[views.spaces] exclude_profiles = []` brings Spaces back for WaTr). `GET /views` lists a
project's views with spec problems, `GET /views/{id}` evaluates one.

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
`skos:broader` lineage, deprecation/superseded flags and `brick:isReplacedBy` replacements) is
cached alongside. For Brick, RealEstateCore's `rec:Space` subtree is indexed as locations and
`rec:Asset` as equipment (Brick deprecates its own location classes in favour of REC's); a
search that matches a deprecated term returns its replacement. Validation uses a
`shifty.PreparedValidator` over the cached shapes in `union` graph mode: ~0.5–0.7 s per run.
`issues.py` maps each finding's focus node to the owning entity (port → equipment, sensor →
point); connection points named by a finding are added to its `affected_ids` after the owner,
so issue ids do not change. The issue text is the validator's own message (IRIs shown as model labels or prefixed
names from the loaded ontologies); nothing is paraphrased. An issue's id is a hash of the
entity, shape, path and raw message, so it is independent of labels and display wording.
Issues are rebuilt from each revision's stored findings when read.

Repair detail comes from pyshifty's algebraic repair engine, the same `RepairSession` that
BuildingMOTIF's `AlgebraicValidationContext` wraps, called directly (about 2 s per revision,
cached in memory; BuildingMOTIF's wrapper takes 30 s+ here). `Vocabulary.repair_witnesses`
returns each failing (focus, statement)'s failing leaves (`have 0, need 1`), missing edges,
offending values, whether it is blocked (opaque SPARQL), and the repair tree's edits.
`Project.repairs` joins witnesses to issues on (focus, statement id) and renders IRIs like issue
text; `GET /api/projects/{pid}/repairs` serves it and the agent's issue context includes it.

Every proposal with a model change also carries the engine's **soundness gate** (`Project.gate`,
stored as `proposal.gate`): sound = introduces no violation, progress = fixes at least one, with
the fixed/introduced violations. ΔG is taken between the before and after models *after*
SHACL-AF inference (one repair session per side, ~2 s); gating the raw edit judges new nodes
without the triples 223P's rules infer and reports violations full validation does not.
When the gate reports introduced violations, the correction agent gets one chance to respond
(revise, or re-propose and say why they are expected) before the proposal is saved, as in
BuildingMOTIF's gated repair loop. Source builds and replies to them are not nudged (new
equipment starts unconnected), and a reply is only asked about violations its pending
proposal did not already introduce.

A revision's stored summary is recounted on read (`issues.recount`) with the current issue
grouping, so older revisions, History and proposal "Model checks" agree with the issue list.

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

### Auto-fix (`autofix.py`)

Auto-fix works through issues without asking about each one. It is one background run
(`POST /autofix`, run kind `autofix`):

1. **Group** the chosen open issues (default: every open violation) by cause — severity,
   shape and path — so one decision covers every issue with the same cause (at most 8 per
   group). The RO-train sample's nine violations are three groups.
2. For each group, on the current head, the correction agent proposes a fix, asked to use only
   what the model, vocabulary and sources establish and to ask instead of guessing.
3. **Verify** — the checks, not the model's confidence, decide. A fix is obvious when it
   resolves the group's issues; introduces nothing (validation delta and the repair engine's
   gate); changes only the issues' own objects (new objects are fine); deletes nothing;
   overrides no locked field; has no notes or questions; and every vocabulary term it chooses
   is grounded — already used in the model, named by the issue, or in the evidence of the
   objects concerned.
4. An obvious fix is applied at once as an `autofix` revision ("Automatic fix: …") that locks
   no fields, since no person confirmed it.
5. A group that needs a decision gets **choices**: a question with options, each option an
   ordinary pending proposal checked like an automatic fix except that the person's choice is
   its grounding (it must still resolve issues, introduce none, stay in scope, delete nothing,
   be minimal). Options that do not work are dropped before they are shown. They come from
   the assistant (`choices` in its reply: options with complete operations, for alternatives
   nothing settles) and from **term substitution**: when a fix is held back only because one
   term it chose is ungrounded, the same fix is tried with each candidate term of that kind
   (those used in the model, and for units those valid for the quantity kind). Choosing an
   option applies its proposal, which locks what it sets, and discards the others.
6. Otherwise the group is left as a pending proposal ("review", with the failed checks as
   reasons), as questions ("input"), or "failed"; later groups still run. Proposals made
   stale by later fixes rebase when applied.

The run's outcome lists the groups and the automatic revisions; the interface shows progress,
then the report: choices as buttons (with "Something else…" to discuss in chat), what to
review (opens the proposal), what needs input (discuss in chat), what was fixed, and "Undo
automatic fixes" while those revisions are still the newest.

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

Each client reports its context window (`context_tokens()`; `llm.context_window` falls back
to 32768): the provider's `context_tokens` setting, else what the endpoint says (llama-server
`/props` per-slot `n_ctx`, Lemonade `/health` loaded `ctx_size`, a `/models` entry's
`context_length`/`max_model_len`/`max_context_window`, Anthropic's `max_input_tokens`),
remembered per endpoint and model. The correction agent turns it into a character budget for
the first message (`correction.context_budget`: a quarter of the window for tool steps, room
for the reply, system prompt, sources, history and images) and `build_context` fills its
sections by priority — selection, issues, related objects, term hints, equipment, spaces,
points, connections, connection points, entities, relationships — counting what it leaves out
so the model can look it up with `find_entities`.
Within a run, replies ask for `llm.reply_tokens(window)` output tokens (an eighth of a small
window; adapters keep their own default from 64k up), since providers count prompt +
`max_tokens` against the window. When the messages outgrow the window, the oldest tool results
(all but the latest two) are shortened to their first lines and their calls may be made again.
Follow-ups carry the last eight conversation turns, each clipped. Tool results are compact
(terms one per line, JSON without indentation), entity lines leave out empty optional fields
and labels that only respell a term's name, and issue lines leave out the repair engine's note
when only opaque (SPARQL) constraints failed.
The reply schema (`correction.step_schema`) offers only the operations and fields `resolve`
accepts in the project's family (`operations.allowed_fields`), the `evidence` field only in runs
over sources, and `token_updates` only while revising a source build.

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
