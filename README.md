# Knowledge-graph modeling workbench (alpha)

A local workbench for people who understand their physical systems but not ontologies:
load or upload what you know about a system, inspect the model as tables and a graph, select
what is wrong, describe the correction in plain language, review the proposed change, apply
or undo it, and export the result.

Each project targets one vocabulary, chosen when it is created: **WaTr** (water treatment, on
ASHRAE 223P), **ASHRAE 223P**, or **Brick**. Models are built and validated with
[BuildingMOTIF](https://github.com/NatLabRockies/BuildingMOTIF) (`gtf-buildingmotif`,
pinned at `78b304aa`), loading each vocabulary from the sources its agent skill prescribes. Inference runs through a local llama.cpp `llama-server`, any
OpenAI-compatible endpoint, or the Anthropic API.

**Status: local alpha.** The correction loop and source-backed point-list build are implemented.
The Sources pane uploads CSV point lists and diagrams, confirms one of three CSV layouts,
and lets you build a reviewable model proposal from confirmed records. Diagrams support
zoom, pan and region selection. Diagram interpretation, cross-source association and pilot
hardening remain. See `docs/branch-report.md` for
what the BuildingMOTIF branch provides and how the plan changed.

## Layout

```
backend/            Python (uv project): FastAPI app, BuildingMOTIF adapter, agent
  src/workbench/    api, project (mutation service), operations, projection, brick, vocabulary,
                    sources, agent/, llm/
  skill/            vendored BuildingMOTIF agent skill (UPSTREAM.yml records the commit)
  scripts/          integration_proof.py, build_sample.py
  tests/
frontend/           React + TypeScript (Vite)
samples/ro-train/   WaTr sample model, point lists, diagram
samples/hvac-mini/  small Brick model (tests)
docs/               branch-report.md, architecture.md, walkthrough.md
workbench.example.toml
```

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you), Node.js 20+,
`git` (uv fetches BuildingMOTIF from GitHub), and internet access the first time each vocabulary
is used: BuildingMOTIF downloads it and its imports once (about 30 s per vocabulary; the three
load one after another in the background at startup). After that everything loads from
`workbench-data/cache` in about a second and works offline.

### macOS / Linux

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh        # if uv is not installed
cd backend && uv sync && cd ..
cd frontend && npm ci && npm run build && cd ..
cp workbench.example.toml workbench.toml               # then pick a model provider
uv run --project backend workbench --config workbench.toml
# open http://127.0.0.1:8765
```

Intel Macs: `pyshifty` has no macOS x86_64 wheel; install a Rust toolchain
(`curl https://sh.rustup.rs -sSf | sh`) before `uv sync` so it can build.

### Windows (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # if needed
cd backend; uv sync; cd ..
cd frontend; npm ci; npm run build; cd ..
Copy-Item workbench.example.toml workbench.toml
uv run --project backend workbench --config workbench.toml
```

### Development

Run the backend as above and, in another terminal, `cd frontend && npm run dev`
(<http://localhost:5173>, proxies `/api` to the backend).

## Model endpoints

Configure providers in `workbench.toml` (`workbench.example.toml` has all three):

- **Local (default)** — run llama.cpp's server yourself. With [llama](https://llama.app)
  (packages llama.cpp for macOS, Windows and Linux):
  `llama serve -hf <user>/<model-GGUF>:<quant> --port 8081 -c 32768`
  (or `-m model.gguf`; add `--mmproj` / use a vision GGUF for diagram extraction later).
  A plain `llama-server` binary from <https://github.com/ggml-org/llama.cpp/releases> works the
  same (`--jinja` must be on; it is the default in current builds). The app expects port 8081
  so it doesn't collide with anything on llama.cpp's default 8080. Use a capable instruction model —
  ~30B class (e.g. Gemma 4 31B, Qwen3 30B-A3B). Small models (≤ 4B) fail safely but rarely
  produce correct changes.
- **OpenAI-compatible remote** — `kind = "openai"`, `base_url`, `model`, `api_key_env`.
- **Anthropic** — `kind = "anthropic"`, `model = "claude-opus-5-5"`, reads `ANTHROPIC_API_KEY`.

The provider can also be chosen per request in the assistant panel.

## Tests

```bash
cd backend && uv run pytest              # service, API, agent mechanics (scripted model)
WORKBENCH_CONFIG=../workbench.toml WORKBENCH_TEST_PROVIDER=openrouter uv run pytest -m llm   # real model
cd frontend && npm test                  # selection/sort/filter logic
```

Covered: selection stays correct after sorting/filtering; duplicate labels stay distinct;
stale proposals are rejected; apply/undo restores the exact graph; save/reopen fidelity
(graph, ids, locks, layout, history); the applied graph equals the previewed triple diff;
out-of-scope changes are flagged; malformed operations are rejected with all problems; export
matches the revision; source-backed build preview and apply preserve evidence links and leave
fields unlocked. Model quality is not part of these tests.

## Build a model from a point list

Create a project with the intended vocabulary, then upload a CSV in **Sources**. Check the
suggested layout, point-name column and metadata columns, and confirm it. Open the resulting
**Records** view and choose **Build model from these records**. The optional hint can explain
site-specific names or abbreviations. A configured model provider must be available for the
build run.

The assistant follows the vendored BuildingMOTIF point-list workflow: it identifies the naming
pattern, maps distinct point tokens to verified vocabulary terms, and classifies equipment from
its points. Review the coverage and mapping summary before **Apply**. Expand **Individual
changes** for each entity and **Technical detail** for operations and RDF. Unmapped Brick
tokens appear as generic points with review issues; unmapped 223P/WaTr records remain in
Records for later modeling. Applying the proposal populates the tables and graph and links
each point to its source record. **Undo** returns to the previous revision; **Build the rest**
can process records still outside the model.

While a proposal is pending, type a reply in the Assistant box above it and choose **Reply to
proposal** (or Ctrl/Cmd+Enter). The assistant uses the pending draft as context and proposes
an updated version. The draft remains available if the reply run fails; nothing changes in the
model until **Apply**. The conversation is shown with the revised proposal.
For a source-build draft, a reply about a repeated point token can revise all points carrying
that token in one proposal.

## Tested configuration (2026-09-30)

| Component | Version / setting | Result |
|---|---|---|
| OS | Linux x86_64 (Ubuntu), Python 3.12.3 via uv, Node 26 | all tests pass |
| BuildingMOTIF | `gtf-buildingmotif@78b304aa`, pyshifty 0.4.4, ontoenv 0.6.4 | validation 0.5–0.7 s per revision |
| WaTr | open223.info `223p.ttl` + watermetadata.org `watr-0.2.ttl` | 18 graphs, 174,512 triples |
| 223P | open223.info `223p.ttl` | 5 graphs |
| Brick | BuildingMOTIF builtin `brick/Brick.ttl` (1.4.1) | 21 graphs; 6 imports not published online (BACnet, Brick `ref`, REC helpers, QUDT usertest/datatype) are skipped |
| Remote model | OpenRouter `google/gemma-4-31b-it` (OpenAI-compatible adapter) | all walkthrough corrections proposed correctly, 15–20 s, 2–3 steps |
| Local model | llama.cpp `llama-server` `b11295` CPU build, Qwen2.5-1.5B-Instruct Q4_K_M | protocol verified (grammar-constrained JSON, streaming, usage); model too small — invalid terms rejected, ended with questions |
| Anthropic adapter | `claude-opus-5-5` | **not run** (no API key available) |
| Browser | Chromium (headless, Playwright) | walkthrough steps 1–3, 5–7; Sources pane (4 CSV layouts, diagram regions); Brick project importing a 789-point site model with a point-type correction |

Not tested on macOS or Windows hardware yet.
