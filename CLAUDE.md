# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, single-user alpha "knowledge-graph modeling workbench": a FastAPI backend (Python, uv project in `backend/`) serving a React + TypeScript (Vite) app in `frontend/`. Users build/correct RDF models of physical systems in one of three vocabularies (WaTr-on-223P, ASHRAE 223P, Brick) via tables, a graph view, and an LLM assistant that proposes reviewable changes. Models are built and validated with BuildingMOTIF (pinned git commit `78b304aa` of the `gtf-buildingmotif` branch).

`docs/architecture.md` is the authoritative design reference (RDF domain mappings per vocabulary, revision/undo semantics, agent workflow, storage layout). `docs/branch-report.md` explains what the BuildingMOTIF branch provides; `docs/walkthrough.md` is the end-to-end user scenario built around `samples/ro-train/`.

## Commands

Use `uv` for everything Python (never pip).

```bash
# Backend
cd backend && uv sync
uv run --project backend workbench --config workbench.toml     # from repo root; serves http://127.0.0.1:8765
cd backend && uv run pytest                                     # all non-LLM tests
cd backend && uv run pytest tests/test_project.py::test_name    # single test
cd backend && WORKBENCH_CONFIG=../workbench.toml WORKBENCH_TEST_PROVIDER=openrouter uv run pytest -m llm   # real model

# Frontend
cd frontend && npm ci
npm run dev       # http://localhost:5173, proxies /api to 127.0.0.1:8765
npm run build     # tsc -b && vite build -> frontend/dist (what the backend serves in non-dev mode)
npm run lint      # oxlint
npm test          # vitest run; single file: npx vitest run src/store.test.ts
```

Scripts: `backend/scripts/build_sample.py` regenerates `samples/ro-train/*` deterministically (the sample model contains three deliberate mistakes used by the walkthrough — don't "fix" them). `backend/scripts/integration_proof.py` is a standalone BuildingMOTIF + WaTr smoke check.

Config: `workbench.toml` (gitignored; copy `workbench.example.toml`) or `WORKBENCH_CONFIG`. Env overrides: `WORKBENCH_DATA_DIR`, `WORKBENCH_PROVIDER`. The local LLM provider expects llama.cpp's server on port 8081 (`run_server.sh` is the author's local launch command).

First use of each vocabulary downloads it and its `owl:imports` (~30 s, needs network); afterwards it loads from `workbench-data/cache/`. The test session fixture loads the `watr` vocabulary, so the first test run is slow and needs network.

## Architecture (big picture)

Backend modules live in `backend/src/workbench/`. Load-bearing rules that span multiple files:

- **The RDF graph is authoritative.** Each revision is a full TriG snapshot (`workbench-data/projects/<id>/revisions/rev-N.trig`) with a model graph plus an annotation graph (`wb:` stable ids, locks, evidence). Tables/graph view are projections computed by `projection.py`; never edit projections directly. Project metadata, revisions, proposals, sources, observations, issues and agent runs live in `project.sqlite` (`store.py`).
- **One mutation path.** Direct cell edits, applied proposals, imports and source builds all go through `Project.build_candidate` → `Project.publish` (`project.py`). Publish checks the base is still head (else `StaleRevision` / HTTP 409), writes the snapshot atomically, and moves head in one SQLite transaction. The agent can only build candidates, never publish.
- **Typed operations** (`operations.py`): pydantic `create/update/delete × equipment/point/connection`. `resolve()` validates all ids and vocabulary terms and mints ids for `new:*` placeholders, reporting every problem at once. `apply_proposal` re-applies stored operations and refuses if the triple diff differs from the previewed diff (`ProposalMismatch`).
- **Stable ids** (`eq-…`, `pt-…`, `cx-…`, stored as `wb:id`) are what selections, proposals, evidence, layout and issues reference — never labels or row positions. Frontend selection logic (`frontend/src/selection.ts`) relies on this too.
- **Vocabulary families.** A project's profile (`watr`, `223p`, `brick`, or custom in `workbench.toml`) maps to a family (`s223` or `brick`) defined in `config.py`. `operations.py` and `projection.py` dispatch on family; the Brick implementation is in `brick.py`. Fields that don't exist in a family must be rejected with an explanation, not ignored.
- **Validation** (`vocabulary.py`): OntoEnv resolves imports into a shared persistent store, the merged closure + term catalog are cached per profile, and `shifty.PreparedValidator` (pyshifty) validates. Do not use pyshacl. `issues.py` maps SHACL findings to owning entities; issue text must be the validator's own message (only IRIs rendered as labels/prefixed names) — never hand-written or hardcoded phrasing per shape/path. Issue ids hash entity, shape, path and raw message; dismissals live in `issue_dismissals`.
- **Human locks.** Fields set by direct edit or confirmed via an applied proposal get `wb:locked`; agents/extraction must not silently overwrite them.

Agent/LLM layer:

- `agent/correction.py`: bounded loop (≤8 steps, ≤2 repairs). Each model reply is schema-constrained JSON: either a read-only tool call (`agent/tools.py`) or `propose`. Context includes selection, related entities, issues, evidence, and skill guidance (`agent/guidance.py`, which reads the vendored skill in `backend/skill/`).
- `agent/build.py`: builds a model from confirmed source records following the skill's point-list workflow (pattern → token mapping table → deterministic operations). Unmapped tokens are never guessed.
- `runs.py`: thread pool with cooperative cancellation; progress persisted on the run record and pushed over SSE (`events.py`).
- `llm/`: `complete_json(system, messages, schema, images?, cancel?)` with `openai_compat.py` (llama-server / OpenRouter, `response_format: json_schema`) and `anthropic_client.py` adapters.
- `sources.py` / `documents.py`: uploaded CSV/TSV, images, PDFs (pypdfium2; all PDFium access must hold `PDF_LOCK` — it is not thread-safe), .docx, and text. Observations from sources are not assertions; model entities link to them via `wb:evidence`.

`backend/skill/` is a vendored copy of the BuildingMOTIF agent skill (`UPSTREAM.yml` records the commit); treat it as upstream content, not app code.

Frontend: single zustand store (`frontend/src/store.ts`) holds project, model rows, selection, runs and the pending proposal; `api.ts` wraps the backend; components in `frontend/src/components/` (tables, `GraphView` on `@xyflow/react` + dagre, `AssistantPanel`, `SourcesPane`). `types.ts` mirrors backend `schemas.py` — keep them in sync when changing API shapes.

## Testing notes

- Backend tests use a scripted fake model for agent mechanics; `-m llm` tests need a real provider. Model quality is not tested.
- Fixtures in `backend/tests/conftest.py`: `workspace` (tmp project dir), `sample_project` (RO train imported into a `watr` project), `by_label` helper.
- Cross-platform matters (Windows/macOS/Linux): use `pathlib`, no fork/signals/Unix sockets, per-transaction SQLite connections, `Path.replace` for atomic writes.
