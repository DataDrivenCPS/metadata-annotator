"""HTTP API. Thin: every mutation goes through Project (the mutation service)."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from dataclasses import asdict
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

from . import __version__
from .agent.correction import describe_selection
from .agent.guidance import SkillGuidance
from .config import Settings, load_settings
from .events import EventBus
from .llm import LLMError, make_client
from .operations import OperationError, OperationList
from .project import Project, ProposalMismatch, StaleRevision, Workspace
from .projection import POINT_KIND_LABELS, entity_iri, sensors_of
from .runs import ProviderUnavailable, RunManager
from .schemas import CsvImportConfig, SelectionScope
from .sources import IMAGE_TYPES, SourceError, preview, suggest_config
from .vocabulary import BRICK, QUDT, S223, Vocabulary, VocabularyRegistry

log = logging.getLogger(__name__)


class CreateProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    profile: str | None = None  # target vocabulary: watr, 223p, brick


class EditRequest(BaseModel):
    base_revision: str
    operations: list[Any]
    summary: str | None = None


class AssistRequest(BaseModel):
    base_revision: str
    selection: SelectionScope = Field(default_factory=SelectionScope)
    instruction: str = Field(min_length=1, max_length=4000)
    provider: str | None = None
    parent_run_id: str | None = None  # continue this run's conversation, e.g. answer its questions


class BuildRequest(BaseModel):
    base_revision: str
    source_ids: list[str] = Field(min_length=1)
    instruction: str = Field("", max_length=4000)
    provider: str | None = None
    source_pages: dict[str, list[int]] = Field(default_factory=dict)


class ProposalReplyRequest(BaseModel):
    instruction: str = Field(min_length=1, max_length=4000)
    provider: str | None = None
    parent_run_id: str | None = None


class LayoutRequest(BaseModel):
    positions: dict[str, list[float]]


class IssueStateRequest(BaseModel):
    state: str = Field(pattern="^(open|dismissed)$")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.getLogger("rdflib.term").setLevel(logging.ERROR)
    bus = EventBus()
    registry = VocabularyRegistry(settings.profiles, settings.cache_dir)
    guidance = SkillGuidance(settings.skill_dir)
    workspace = Workspace(settings.projects_dir, registry, bus)
    runs = RunManager(settings, guidance, bus)
    app = FastAPI(title="Knowledge-graph modeling workbench", version=__version__)
    app.state.settings, app.state.registry, app.state.workspace, app.state.runs = settings, registry, workspace, runs

    # Default vocabulary first; the others load right after (each is cached after first use).
    threading.Thread(target=_load_all, args=(registry, settings.default_profile), daemon=True,
                     name="vocab-load").start()

    # ------------------------------------------------------------ helpers

    def vocab_for(profile: str | None) -> Vocabulary:
        name = profile or settings.default_profile
        try:
            return registry.get(name, wait=120)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from None
        except TimeoutError:
            raise HTTPException(503, f"The {name} vocabulary is still loading; try again shortly.") from None
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from None

    def ready() -> None:
        vocab_for(None)

    def project(pid: str) -> Project:
        try:
            p = workspace.get(pid)
        except KeyError:
            raise HTTPException(404, f"No project {pid}") from None
        except (TimeoutError, RuntimeError) as exc:
            raise HTTPException(503, str(exc)) from None
        if not getattr(p, "_recovered", False):
            runs.recover(p)
            p._recovered = True  # type: ignore[attr-defined]
        return p

    def rev_or_head(p: Project, revision: str | None) -> str:
        rid = revision or p.head()
        try:
            p.revision(rid)
        except KeyError:
            raise HTTPException(404, f"No revision {rid}") from None
        return rid

    @app.exception_handler(StaleRevision)
    async def _stale(_: Request, exc: StaleRevision):
        return JSONResponse(status_code=409, content={
            "error": "stale", "detail": f"This was based on {exc.base}, but the model is now at {exc.head}.",
            "base": exc.base, "head": exc.head})

    @app.exception_handler(OperationError)
    async def _malformed(_: Request, exc: OperationError):
        return JSONResponse(status_code=422, content={"error": "malformed", "detail": "; ".join(exc.problems),
                                                      "problems": exc.problems})

    @app.exception_handler(ProposalMismatch)
    async def _mismatch(_: Request, exc: ProposalMismatch):
        return JSONResponse(status_code=409, content={"error": "mismatch", "detail": str(exc)})

    # ------------------------------------------------------------- status

    @app.get("/api/status")
    def status():
        providers = []
        for name, cfg in settings.providers.items():
            providers.append({"name": name, "kind": cfg.kind, "model": cfg.model, "base_url": cfg.base_url,
                              "supports_images": cfg.supports_images or cfg.kind == "anthropic", "default": name == settings.default_provider,
                              "has_key": bool(cfg.api_key) if cfg.api_key_env else None})
        vocabularies = registry.status()
        default = next(v for v in vocabularies if v["name"] == settings.default_profile)
        return {"version": __version__, "vocabulary": default, "vocabularies": vocabularies,
                "default_vocabulary": settings.default_profile,
                "skill_version": guidance.version, "providers": providers,
                "data_dir": str(settings.data_dir)}

    @app.get("/api/providers/{name}/health")
    def provider_health(name: str):
        try:
            client = make_client(settings.provider(name))
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, str(exc)) from None
        return client.health()  # type: ignore[attr-defined]

    # --------------------------------------------------------- vocabulary

    @app.get("/api/vocabulary/search")
    def vocab_search(q: str, kind: str | None = None, limit: int = 20, profile: str | None = None):
        vocab = vocab_for(profile)
        kinds = kind.split(",") if kind else None
        return [asdict(t) for t in vocab.search(q, kinds, min(limit, 100))]

    @app.get("/api/vocabulary/options/{kind}")
    def vocab_options(kind: str, quantity_kind: str | None = None, limit: int = 400, profile: str | None = None):
        """Options for dropdowns. Units can be narrowed to a quantity kind."""
        vocab = vocab_for(profile)
        if kind == "unit" and quantity_kind:
            terms = vocab.units_for(quantity_kind)
        else:
            terms = [vocab.terms[i] for i in vocab.by_kind.get(kind, [])
                     if not vocab.terms[i].deprecated and not vocab.terms[i].abstract]
        return [{"iri": t.iri, "label": t.label, "symbol": t.symbol} for t in terms[:limit]]

    @app.get("/api/vocabulary/point-kinds")
    def point_kinds():
        return [{"id": k, "label": v} for k, v in POINT_KIND_LABELS.items() if k != "other"]

    @app.get("/api/vocabulary/term")
    def vocab_term(iri: str, profile: str | None = None):
        vocab = vocab_for(profile)
        t = vocab.term(iri)
        if t is None:
            raise HTTPException(404, "unknown term")
        out = asdict(t)
        if t.kind in ("equipment", "sensor", "connection", "point_class"):
            out["shape"] = vocab.describe_class(iri)
        return out

    # ----------------------------------------------------------- projects

    @app.get("/api/projects")
    def list_projects():
        ready()
        return workspace.list()

    @app.post("/api/projects")
    def create_project(body: CreateProject):
        profile = body.profile or settings.default_profile
        vocab_for(profile)  # load it now (first use of a vocabulary can take ~30 s)
        return workspace.create(body.name, profile).info()

    @app.post("/api/projects/sample")
    def create_sample():
        """A project preloaded with samples/ro-train/model.ttl (see docs/walkthrough.md)."""
        ready()
        model = settings.samples_dir / "ro-train" / "model.ttl"
        if not model.exists():
            raise HTTPException(404, f"sample model not found at {model}")
        vocab_for("watr")
        p = workspace.create("RO train sample", "watr")
        p.import_model(model.read_bytes(), model.name)
        return p.info()

    @app.get("/api/projects/{pid}")
    def get_project(pid: str):
        return project(pid).info()

    @app.get("/api/projects/{pid}/model")
    def get_model(pid: str, revision: str | None = None):
        p = project(pid)
        rid = rev_or_head(p, revision)
        view = p.view(rid)
        rev = p.revision(rid)
        return {"revision": rev.model_dump(mode="json"), "head": p.head(), "info": p.info(),
                "view": view.to_dict(), "issues": [i.model_dump() for i in p.issues(rid)],
                "layout": p.layout()}

    @app.get("/api/projects/{pid}/revisions")
    def list_revisions(pid: str):
        return [r.model_dump(mode="json", exclude={"operations"}) | {"operation_count": len(r.operations)}
                for r in project(pid).revisions()]

    @app.get("/api/projects/{pid}/revisions/{rid}")
    def get_revision(pid: str, rid: str):
        p = project(pid)
        rev_or_head(p, rid)
        return p.revision(rid).model_dump(mode="json")

    @app.post("/api/projects/{pid}/import")
    async def import_model(pid: str, file: UploadFile = File(...)):
        p = project(pid)
        data = await file.read()
        try:
            rev = await asyncio.to_thread(p.import_model, data, file.filename or "model.ttl")
        except Exception as exc:  # parse errors are user errors here
            raise HTTPException(400, f"Could not read {file.filename}: {exc}") from None
        return rev.model_dump(mode="json")

    @app.post("/api/projects/{pid}/edits")
    async def edit(pid: str, body: EditRequest):
        p = project(pid)
        try:
            ops = OperationList.validate_python(body.operations)
        except Exception as exc:
            raise HTTPException(422, f"Malformed operations: {exc}") from None
        rev, cand = await asyncio.to_thread(p.edit, body.base_revision, ops, body.summary)
        return {"revision": rev.model_dump(mode="json"), "changes": [c.model_dump() for c in cand.changes],
                "notes": cand.result.notes}

    @app.post("/api/projects/{pid}/undo")
    def undo(pid: str):
        try:
            return {"head": project(pid).undo()}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/projects/{pid}/redo")
    def redo(pid: str):
        try:
            return {"head": project(pid).redo()}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.put("/api/projects/{pid}/layout")
    def save_layout(pid: str, body: LayoutRequest):
        project(pid).save_layout(body.positions)
        return {"ok": True}

    @app.get("/api/projects/{pid}/repairs")
    async def repairs(pid: str, revision: str | None = None):
        """Per-issue repair information from pyshifty's repair witnesses (computed on first use)."""
        p = project(pid)
        return await asyncio.to_thread(p.repairs, rev_or_head(p, revision))

    @app.put("/api/projects/{pid}/issues/{issue_id}")
    def set_issue(pid: str, issue_id: str, body: IssueStateRequest):
        project(pid).set_issue_state(issue_id, body.state)
        return {"ok": True}

    @app.get("/api/projects/{pid}/corrections")
    def corrections(pid: str, entity_id: str | None = None):
        return project(pid).corrections(entity_id)

    @app.get("/api/projects/{pid}/entities/{eid}")
    def inspect_entity(pid: str, eid: str, revision: str | None = None):
        p = project(pid)
        rid = rev_or_head(p, revision)
        pg = p.graph(rid)
        vocab = p.vocab
        node = entity_iri(pg, eid)
        if node is None or (node, None, None) not in pg.model:
            raise HTTPException(404, f"No entity {eid} in {rid}")
        row = p.view(rid).rows().get(eid)
        sub = entity_subgraph(pg, node)
        for prefix, ns in [*vocab.namespaces.items(), ("s223", str(S223)), ("brick", str(BRICK)),
                           ("qudt", str(QUDT)), ("unit", "http://qudt.org/vocab/unit/"),
                           ("quantitykind", "http://qudt.org/vocab/quantitykind/"), ("", pg.ns)]:
            if prefix in ("watr", "s223", "brick", "qudt", "unit", "quantitykind", ""):
                sub.bind(prefix, ns, override=True)
        sub.bind("rdfs", RDFS)
        evidence = [p.store.get_body("observations", o) for o in pg.evidence(node)]
        return {"id": eid, "iri": str(node), "revision": rid, "row": asdict(row) if row else None,
                "types": [{"iri": str(t), "label": vocab.label(str(t))} for t in pg.model.objects(node, RDF.type)],
                "turtle": sub.serialize(format="turtle"),
                "locked": sorted(pg.locked_fields(node)),
                "evidence": [e for e in evidence if e],
                "issues": [i.model_dump() for i in p.issues(rid) if eid in i.affected_ids],
                "history": p.corrections(eid)[:50]}

    # ------------------------------------------------------------ sources

    def source_or_404(p: Project, sid: str):
        try:
            return p.source(sid)
        except KeyError:
            raise HTTPException(404, f"No source {sid}") from None

    @app.post("/api/projects/{pid}/sources")
    async def upload_source(pid: str, file: UploadFile = File(...)):
        p = project(pid)
        data = await file.read()
        try:
            src = await asyncio.to_thread(p.add_source, file.filename or "upload", data)
        except SourceError as exc:
            raise HTTPException(400, str(exc)) from None
        return src.model_dump(mode="json")

    @app.get("/api/projects/{pid}/sources")
    def list_sources(pid: str):
        p = project(pid)
        counts: dict[str, int] = {}
        for o in p.observations():
            if o.status != "superseded":
                counts[o.source_id] = counts.get(o.source_id, 0) + 1
        modeled_by_source: dict[str, int] = {}
        modeled = p.evidence_map(p.head())
        for o in p.observations():
            if o.id in modeled:
                modeled_by_source[o.source_id] = modeled_by_source.get(o.source_id, 0) + 1
        return [s.model_dump(mode="json") | {"observation_count": counts.get(s.id, 0),
                                              "modeled_count": modeled_by_source.get(s.id, 0)} for s in p.sources()]

    @app.get("/api/projects/{pid}/sources/{sid}/file")
    def source_file(pid: str, sid: str):
        p = project(pid)
        src = source_or_404(p, sid)
        media = IMAGE_TYPES.get(Path(src.filename).suffix.lower(), "application/pdf" if src.kind == "pdf" else "text/csv" if src.kind == "csv" else None)
        return FileResponse(p.source_file(sid), media_type=media, filename=src.filename,
                            content_disposition_type="inline" if src.kind == "pdf" else "attachment")

    @app.get("/api/projects/{pid}/sources/{sid}/document-preview")
    def document_preview(pid: str, sid: str, page: int = 1):
        from .documents import document_text, pdf_page
        p = project(pid)
        src = source_or_404(p, sid)
        try:
            data = p.source_file(sid).read_bytes()
            if src.kind == "pdf":
                text, _ = pdf_page(data, page, render=False)
            elif src.kind == "document":
                text = document_text(data, src.filename)
            else:
                raise SourceError("This source is not a PDF or text document.")
            return {"text": text[:50000], "truncated": len(text) > 50000}
        except SourceError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/projects/{pid}/sources/{sid}/pages/{page}.png")
    def document_page_image(pid: str, sid: str, page: int):
        from .documents import pdf_page
        p = project(pid)
        src = source_or_404(p, sid)
        if src.kind != "pdf":
            raise HTTPException(400, "This source is not a PDF.")
        try:
            _, pixels = pdf_page(p.source_file(sid).read_bytes(), page)
            return Response(content=pixels, media_type="image/png")
        except SourceError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/projects/{pid}/sources/{sid}/grid")
    def source_grid(pid: str, sid: str, limit: int = 60, delimiter: str | None = None):
        p = project(pid)
        src = source_or_404(p, sid)
        try:
            rows, delim = p.source_grid(sid, delimiter)
        except (ValueError, SourceError) as exc:
            raise HTTPException(400, str(exc)) from None
        suggested, reason = suggest_config(rows, delim)
        return {"rows": rows[:max(1, min(limit, 500))], "total_rows": len(rows),
                "columns": max((len(r) for r in rows), default=0), "delimiter": delim,
                "config": (src.import_config or suggested).model_dump(), "suggested": suggested.model_dump(),
                "reason": reason, "confirmed": src.import_config is not None}

    @app.post("/api/projects/{pid}/sources/{sid}/preview")
    def source_preview(pid: str, sid: str, cfg: CsvImportConfig):
        p = project(pid)
        source_or_404(p, sid)
        try:
            rows, _ = p.source_grid(sid, cfg.delimiter)
            return preview(rows, cfg)
        except (ValueError, SourceError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.post("/api/projects/{pid}/sources/{sid}/confirm")
    def source_confirm(pid: str, sid: str, cfg: CsvImportConfig):
        p = project(pid)
        source_or_404(p, sid)
        try:
            return p.confirm_csv_mapping(sid, cfg)
        except (ValueError, SourceError) as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/projects/{pid}/sources/{sid}/observations")
    def source_observations(pid: str, sid: str, status: str | None = None, limit: int = 1000):
        """Records with the entity (in the current revision) that cites each, if any."""
        p = project(pid)
        source_or_404(p, sid)
        modeled = p.evidence_map(p.head())
        return [o.model_dump(mode="json") | {"entity_id": modeled.get(o.id)}
                for o in p.observations(sid, status)[:limit]]

    @app.get("/api/projects/{pid}/export.ttl")
    def export_ttl(pid: str, revision: str | None = None):
        p = project(pid)
        rid = rev_or_head(p, revision)
        return Response(p.export_turtle(rid), media_type="text/turtle", headers={
            "Content-Disposition": f'attachment; filename="{p.id}-{rid}.ttl"', "X-Revision": rid})

    @app.get("/api/projects/{pid}/export-points.csv")
    def export_points(pid: str, revision: str | None = None):
        p = project(pid)
        rid = rev_or_head(p, revision)
        return PlainTextResponse(p.export_points_csv(rid), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{p.id}-{rid}-points.csv"', "X-Revision": rid})

    # --------------------------------------------------- assistant / proposals

    @app.post("/api/projects/{pid}/selection/describe")
    def selection_summary(pid: str, body: SelectionScope, revision: str | None = None):
        p = project(pid)
        return {"summary": describe_selection(p, rev_or_head(p, revision), body)}

    @app.post("/api/projects/{pid}/assist")
    def assist(pid: str, body: AssistRequest):
        p = project(pid)
        try:
            run = runs.start_correction(p, body.base_revision, body.selection, body.instruction, body.provider,
                                        body.parent_run_id)
        except (KeyError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return run.model_dump(mode="json")

    @app.post("/api/projects/{pid}/build")
    def build(pid: str, body: BuildRequest):
        p = project(pid)
        for sid in body.source_ids:
            source_or_404(p, sid)
        try:
            run = runs.start_build(p, body.base_revision, body.source_ids, body.instruction, body.provider, body.source_pages)
        except SourceError as exc:
            raise HTTPException(400, str(exc)) from None
        except KeyError as exc:
            raise HTTPException(400, str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return run.model_dump(mode="json")

    @app.get("/api/projects/{pid}/runs")
    def list_runs(pid: str):
        return [r.model_dump(mode="json") for r in runs.list(project(pid))]

    @app.get("/api/projects/{pid}/runs/{run_id}")
    def get_run(pid: str, run_id: str):
        try:
            return runs.get(project(pid), run_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"No run {run_id}") from None

    @app.post("/api/projects/{pid}/runs/{run_id}/cancel")
    def cancel_run(pid: str, run_id: str):
        try:
            return runs.cancel(project(pid), run_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"No run {run_id}") from None

    @app.get("/api/projects/{pid}/proposals")
    def list_proposals(pid: str, status: str | None = None):
        return [pr.model_dump(mode="json") for pr in project(pid).proposals(status)]

    @app.get("/api/projects/{pid}/proposals/{prop_id}")
    def get_proposal(pid: str, prop_id: str):
        try:
            return project(pid).proposal(prop_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"No proposal {prop_id}") from None

    @app.post("/api/projects/{pid}/proposals/{prop_id}/reply")
    def reply_to_proposal(pid: str, prop_id: str, body: ProposalReplyRequest):
        instruction = body.instruction.strip()
        if not instruction:
            raise HTTPException(422, "Reply cannot be blank")
        p = project(pid)
        try:
            prop = p.proposal(prop_id)
            run = runs.start_revision(p, prop, instruction, body.provider, parent_run_id=body.parent_run_id)
        except KeyError:
            raise HTTPException(404, f"No proposal {prop_id}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None
        return run.model_dump(mode="json")

    @app.post("/api/projects/{pid}/proposals/{prop_id}/apply")
    async def apply_proposal(pid: str, prop_id: str):
        p = project(pid)
        try:
            rev = await asyncio.to_thread(p.apply_proposal, prop_id)
        except KeyError:
            raise HTTPException(404, f"No proposal {prop_id}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        return rev.model_dump(mode="json")

    @app.post("/api/projects/{pid}/proposals/{prop_id}/dismiss")
    def dismiss_proposal(pid: str, prop_id: str):
        try:
            return project(pid).dismiss_proposal(prop_id).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"No proposal {prop_id}") from None

    @app.post("/api/projects/{pid}/proposals/{prop_id}/regenerate")
    def regenerate(pid: str, prop_id: str, provider: str | None = Body(None, embed=True)):
        """Ask the assistant to reconsider a proposal against the current revision."""
        p = project(pid)
        try:
            prop = p.proposal(prop_id)
            instruction = (
                "Reconsider the original request against the current model revision. The model may "
                "have changed, or the issue may already be fixed. Inspect the current state and "
                "decide whether a change is still warranted. If it is already resolved, explain "
                "that and propose no change. Original request: " + prop.instruction
            )
            return runs.start_revision(p, prop, instruction, provider, reconsider=True).model_dump(mode="json")
        except KeyError:
            raise HTTPException(404, f"No proposal {prop_id}") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        except ProviderUnavailable as exc:
            raise HTTPException(503, str(exc)) from None

    # ------------------------------------------------------------- events

    @app.get("/api/projects/{pid}/events")
    async def events(pid: str, request: Request):
        project(pid)
        q = bus.subscribe(pid)

        async def stream():
            try:
                yield "retry: 2000\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ----------------------------------------------------------- frontend

    dist = settings.frontend_dist
    if dist is not None:
        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            target = (dist / path).resolve()
            if path and target.is_file() and dist.resolve() in target.parents:
                return FileResponse(target)
            return FileResponse(dist / "index.html")

    return app


def entity_subgraph(pg, node: URIRef) -> Graph:
    """Triples describing an entity and the sub-nodes that belong to it."""
    g = pg.model
    nodes = {node}
    nodes |= set(g.objects(node, S223.hasConnectionPoint)) | set(g.subjects(S223.isConnectionPointOf, node))
    nodes |= set(sensors_of(pg, node))
    nodes |= set(g.objects(node, S223.cnx))
    sub = Graph()
    for n in nodes:
        for t in g.triples((n, None, None)):
            sub.add(t)
    for s, p_ in g.subject_predicates(node):
        sub.add((s, p_, node))
    return sub


def _load_all(registry: VocabularyRegistry, first: str) -> None:
    for name in [first, *[n for n in registry.vocabularies if n != first]]:
        try:
            registry.get(name, wait=None)
        except Exception:  # recorded on the vocabulary and reported by /api/status
            pass


def main() -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser(description="Run the modeling workbench")
    ap.add_argument("--config", help="path to workbench.toml")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings(args.config)
    uvicorn.run(create_app(settings), host=args.host or settings.host, port=args.port or settings.port)
