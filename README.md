# Knowledge-graph modeling workbench

Turn diagrams, point lists, and documents into a model of your physical system.
The assistant helps identify equipment, sensors, and connections; you inspect the
result in tables and a graph, review changes, and export the model. This is a local
alpha application.

## Install and run

You'll need Git and Node.js with npm, plus [uv](https://docs.astral.sh/uv/getting-started/installation/)
to install Python and the backend dependencies.

**1. Install uv** using the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/).

**2. Download or clone this repository** and open a terminal in its folder. Build
the browser interface once:

```bash
npm --prefix frontend ci
npm --prefix frontend run build
```

**3. Configure your model.** Copy [workbench.example.toml](workbench.example.toml)
to `workbench.toml`. Set `[llm]` → `default` to the provider name you want to use
and edit its settings under `[llm.providers.<name>]`. You can
use a local llama.cpp server, an OpenAI-compatible service, Anthropic, or another
provider supported by LiteLLM. Existing `openai` and `anthropic` provider kinds
remain supported. For native routes, set `kind = "litellm"` and a provider-prefixed
model such as `gemini/gemini-2.5-flash`; the example configuration includes a
Gemini entry. For a remote service, set the API key environment variable named
in the configuration.
Use a model with image support to extract from diagrams or scanned PDFs.
Replies default to 32,768 output tokens; smaller context windows and known native
model output limits reduce that budget.

**4. Start the app:**

```bash
uv run --project backend workbench --config workbench.toml
```

**5. Open [http://127.0.0.1:8765](http://127.0.0.1:8765) in your browser.** Keep the
terminal running while you use the app. The first launch downloads dependencies
and ontologies, so give it a little time; later launches reuse the cache.

## Build and improve your model

1. **Create a project and choose an ontology.** An ontology supplies the types and
   relationships used to describe your system. Choose **WaTr** for water treatment,
   **ASHRAE 223P** for building and plant topology, or **Brick** for building systems
   and sensor/control points.
2. **Upload sources.** Add CSV/TSV point lists, diagrams, PDFs, Word documents, or
   text in **Sources**. For a CSV, check and confirm the layout first. Try the files
   in [samples/ro-train](samples/ro-train/) if you want an example.
3. **Ask the assistant to build a model.** Choose **Build model** for a document or
   diagram, or **Build model from these records** for a point list. Explain what
   you want to extract, such as “Identify the tanks, pumps, and pipes” or “These
   point names belong to the air handlers.” You can also leave the hint blank and
   see what it finds.
4. **Review and apply.** Inspect the proposed changes and their source evidence.
   Answer questions or ask for adjustments in chat, then choose **Apply** when
   you're happy with the draft.
5. **Inspect and fix.** Explore the tables and graph. Use **Auto-fix** to work
   through validation issues, or select specific entities and describe a change:
   “This pump feeds Tank 2” or “These sensors measure temperature.” Auto-fix applies
   fixes that pass its checks and leaves choices or uncertain changes for you to
   review. Fields you've confirmed are protected from automatic replacement.
6. **Keep refining, then export.** Add more sources, edit fields directly, or
   continue the conversation. Use **Undo** or revision history to revisit changes,
   and export the resulting RDF model when you're ready.

Uploaded sources provide evidence; the model records the equipment, points, and
relationships you've accepted. Ordinary assistant proposals stay as drafts until
you apply them.

## More detail

- [Walkthrough](docs/walkthrough.md): work through a sample project.
- [Architecture](docs/architecture.md): how the application works internally.
- [Configuration example](workbench.example.toml): model providers and other settings.
