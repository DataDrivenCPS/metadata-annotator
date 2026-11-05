"""The source index: passages of uploaded documents in ``project.sqlite``, kept current.

Tables (created on first use; a project without them simply has nothing indexed):

* ``source_index``  - one row per source: status, the file hash and embedding model it reflects.
* ``source_chunks`` - passages with their location (page, box, text range) and section path.
* ``source_chunks_fts`` - FTS5 keyword index over them (exact tags such as ``RO-1_FIT_101``).
* ``chunk_vectors`` - one float32 vector per passage and embedding model.

The ``Indexer`` reacts to source events: every new or changed PDF, image or text document is
parsed and embedded in the background, one at a time, and its status is published as a
``source_index`` event. Removed sources lose their passages. Statuses are recomputed from the
store when a project is opened, so work interrupted by a restart is resumed.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np

from ..config import Settings
from ..llm import Cancelled, CancelToken, LLMError
from ..schemas import Source, now
from .chunking import Chunk, docling_chunks, text_chunks
from .describe import Describer, description_chunk
from .docling import DoclingFailed, DoclingService, DoclingUnavailable
from .embed import Embedder, EmbeddingError, make_embedder

if TYPE_CHECKING:
    from ..project import Project, Workspace

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_index (source_id TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_chunks (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    page INTEGER,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS source_chunks_by_source ON source_chunks(source_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS source_chunks_fts USING fts5(
    chunk_id UNINDEXED, source_id UNINDEXED, headings, text);
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id TEXT NOT NULL,
    model TEXT NOT NULL,
    source_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model)
);
CREATE INDEX IF NOT EXISTS chunk_vectors_by_source ON chunk_vectors(model, source_id);
"""

INDEXED_KINDS = {"pdf", "image", "document"}
DOCLING_DOCUMENTS = {".docx"}  # other text documents are read as text, with exact offsets
MARKDOWN = {".md"}
RRF_K = 60  # reciprocal rank fusion constant


# ------------------------------------------------------------------ storage

_schema_ready: set[Path] = set()
_schema_lock = threading.Lock()


def ensure_schema(project: "Project") -> None:
    path = project.store.db_path
    if path in _schema_ready:
        return
    with _schema_lock, project.store.tx() as db:
        db.executescript(SCHEMA)
        _schema_ready.add(path)


def statuses(project: "Project") -> dict[str, dict[str, Any]]:
    ensure_schema(project)
    with project.store.tx() as db:
        return {r["source_id"]: json.loads(r["body"]) for r in db.execute("SELECT * FROM source_index")}


def status(project: "Project", sid: str) -> dict[str, Any] | None:
    ensure_schema(project)
    with project.store.tx() as db:
        row = db.execute("SELECT body FROM source_index WHERE source_id=?", (sid,)).fetchone()
    return json.loads(row["body"]) if row else None


def _put_status(project: "Project", sid: str, body: dict[str, Any]) -> None:
    body = {**body, "updated_at": now()}
    with project.store.tx() as db:
        db.execute("INSERT INTO source_index(source_id, body) VALUES(?, ?) "
                   "ON CONFLICT(source_id) DO UPDATE SET body=excluded.body", (sid, json.dumps(body)))
    project.bus.publish(project.id, {"type": "source_index", "source_id": sid, "index": body})


def forget(project: "Project", sid: str, *, keep_status: bool = False) -> None:
    """Remove a source's passages and vectors (and, unless ``keep_status``, its status)."""
    ensure_schema(project)
    with project.store.tx() as db:
        db.execute("DELETE FROM source_chunks_fts WHERE source_id=?", (sid,))
        db.execute("DELETE FROM source_chunks WHERE source_id=?", (sid,))
        db.execute("DELETE FROM chunk_vectors WHERE source_id=?", (sid,))
        if not keep_status:
            db.execute("DELETE FROM source_index WHERE source_id=?", (sid,))
    VECTORS.invalidate(project.store.db_path)


def _add_chunks(project: "Project", sid: str, chunks: list[Chunk], first_seq: int) -> list[str]:
    ids = []
    with project.store.tx() as db:
        for seq, chunk in enumerate(chunks, first_seq):
            cid = f"chk-{sid.removeprefix('src-')}-{seq}"
            ids.append(cid)
            body = {"kind": chunk.kind, "text": chunk.text, "headings": chunk.headings, **chunk.location(),
                    **({"labels": chunk.labels} if chunk.labels else {})}
            db.execute("INSERT OR REPLACE INTO source_chunks(id, source_id, seq, kind, page, body) VALUES(?,?,?,?,?,?)",
                       (cid, sid, seq, chunk.kind, chunk.page, json.dumps(body)))
            db.execute("INSERT INTO source_chunks_fts(chunk_id, source_id, headings, text) VALUES(?,?,?,?)",
                       (cid, sid, " ".join(chunk.headings), chunk.text))
    return ids


def _add_vectors(project: "Project", sid: str, model: str, ids: list[str], vectors: np.ndarray) -> None:
    with project.store.tx() as db:
        db.executemany("INSERT OR REPLACE INTO chunk_vectors(chunk_id, model, source_id, vector) VALUES(?,?,?,?)",
                       [(cid, model, sid, vectors[i].astype(np.float32).tobytes()) for i, cid in enumerate(ids)])
    VECTORS.invalidate(project.store.db_path)


def chunks(project: "Project", ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    ids = list(ids)
    if not ids:
        return {}
    ensure_schema(project)
    with project.store.tx() as db:
        rows = db.execute(f"SELECT * FROM source_chunks WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: {"id": r["id"], "source_id": r["source_id"], "seq": r["seq"], **json.loads(r["body"])} for r in rows}


class _VectorCache:
    """Vectors of a project's passages per embedding model, loaded once and reused for queries."""

    def __init__(self, size: int = 4):
        self._items: OrderedDict[tuple[Path, str], tuple[list[str], np.ndarray, np.ndarray]] = OrderedDict()
        self._lock = threading.Lock()
        self.size = size

    def invalidate(self, db: Path) -> None:
        with self._lock:
            for key in [k for k in self._items if k[0] == db]:
                del self._items[key]

    def get(self, project: "Project", model: str) -> tuple[list[str], np.ndarray, np.ndarray]:
        key = (project.store.db_path, model)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        with project.store.tx() as db:
            rows = db.execute("SELECT chunk_id, source_id, vector FROM chunk_vectors WHERE model=?", (model,)).fetchall()
        ids = [r["chunk_id"] for r in rows]
        sources = np.array([r["source_id"] for r in rows], dtype=object)
        matrix = np.vstack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 0), np.float32)
        with self._lock:
            self._items[key] = (ids, sources, matrix)
            while len(self._items) > self.size:
                self._items.popitem(last=False)
        return ids, sources, matrix


VECTORS = _VectorCache()


# ------------------------------------------------------------------- search

_QUERY_TERMS = re.compile(r"[\w][\w./-]*", re.UNICODE)
_STOP = {"the", "a", "an", "of", "to", "in", "on", "and", "or", "is", "are", "for", "with", "what", "which",
         "this", "that", "these", "those", "it", "be", "by", "as", "at", "from", "does", "do", "how"}


def keyword_query(text: str) -> str | None:
    """An FTS5 query matching any term; each term is a phrase, so a tag matches as a unit."""
    terms = [t for t in dict.fromkeys(_QUERY_TERMS.findall(text)) if t.lower() not in _STOP and len(t) > 1]
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms[:40]) or None


@dataclass
class Hit:
    chunk_id: str
    source_id: str
    score: float
    keyword_rank: int | None = None
    vector_rank: int | None = None


def search(project: "Project", query: str, *, source_ids: list[str] | None = None, k: int = 8,
           embedder: Embedder | None = None) -> list[Hit]:
    """Passages for ``query``: keyword (BM25) and vector rankings fused by reciprocal rank."""
    ensure_schema(project)
    depth = max(k * 4, 20)
    keyword: list[tuple[str, str]] = []
    if fts := keyword_query(query):
        where, args = "source_chunks_fts MATCH ?", [fts]
        if source_ids:
            where += f" AND source_id IN ({','.join('?' * len(source_ids))})"
            args += source_ids
        with project.store.tx() as db:
            keyword = [(r["chunk_id"], r["source_id"]) for r in db.execute(
                f"SELECT chunk_id, source_id FROM source_chunks_fts WHERE {where} "
                f"ORDER BY bm25(source_chunks_fts, 0, 0, 0.5, 1.0) LIMIT ?", [*args, depth])]
    vector: list[tuple[str, str]] = []
    if embedder is not None and query.strip():
        ids, sources, matrix = VECTORS.get(project, embedder.model_id)
        if len(ids):
            mask = np.isin(sources, source_ids) if source_ids else np.ones(len(ids), bool)
            if mask.any():
                try:
                    q = embedder.query(query)
                except EmbeddingError as exc:
                    log.warning("vector search skipped: %s", exc)
                else:
                    scores = np.where(mask, matrix @ q, -np.inf)
                    top = np.argsort(-scores)[:depth]
                    vector = [(ids[i], str(sources[i])) for i in top if np.isfinite(scores[i])]
    fused: dict[str, Hit] = {}
    for rank, (cid, sid) in enumerate(keyword):
        hit = fused.setdefault(cid, Hit(cid, sid, 0.0))
        hit.score += 1 / (RRF_K + rank + 1)
        hit.keyword_rank = rank + 1
    for rank, (cid, sid) in enumerate(vector):
        hit = fused.setdefault(cid, Hit(cid, sid, 0.0))
        hit.score += 1 / (RRF_K + rank + 1)
        hit.vector_rank = rank + 1
    return sorted(fused.values(), key=lambda h: -h.score)[:k]


# ------------------------------------------------------------------ indexer

def indexable(source: Source) -> bool:
    return source.kind in INDEXED_KINDS


_ACTIVE: dict[Path, "Indexer"] = {}  # projects directory -> the indexer keeping it current


def active(project: "Project") -> "Indexer | None":
    """The indexer for this project's workspace (None without one, e.g. in a bare script)."""
    return _ACTIVE.get(project.root.parent.resolve())


class Indexer:
    """Keeps every project's source index current, in the background."""

    def __init__(self, settings: Settings, workspace: "Workspace"):
        self.cfg = settings.retrieval
        self.root = settings.data_dir / "retrieval"
        self.workspace = workspace
        self.docling = DoclingService(self.cfg, self.root / "docling-serve") if self.cfg.parser == "docling" else None
        self.embedder = make_embedder(self.cfg.embedding, self.root / "fastembed")
        self.describer = Describer(settings)
        self._work = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index")
        self._maintain = ThreadPoolExecutor(max_workers=1, thread_name_prefix="index-maintain")
        self._lock = threading.Lock()
        self._queued: dict[tuple[str, str], CancelToken] = {}  # (project, source) -> its job's token
        self._maintaining: set[str] = set()
        self._closed = False
        _ACTIVE[workspace.dir.resolve()] = self

    # ----------------------------------------------------------- events

    def on_event(self, project_id: str, event: dict[str, Any]) -> None:
        """Bus listener: any change to the set of sources re-checks that project's index."""
        if event.get("type") == "sources" and self.cfg.enabled:
            self.schedule_maintenance(project_id)

    def schedule_maintenance(self, project_id: str) -> None:
        with self._lock:
            if self._closed or project_id in self._maintaining:
                return
            self._maintaining.add(project_id)
        self._maintain.submit(self._run_maintenance, project_id)

    def _run_maintenance(self, project_id: str) -> None:
        with self._lock:
            self._maintaining.discard(project_id)
        try:
            self.maintain(self.workspace.get(project_id))
        except KeyError:
            pass
        except Exception:  # noqa: BLE001 - never kill the maintenance thread
            log.exception("index maintenance failed for %s", project_id)

    # ------------------------------------------------------ maintenance

    def maintain(self, project: "Project") -> None:
        """Queue what is missing or out of date; drop passages of sources that no longer exist."""
        if not self.cfg.enabled:
            return
        ensure_schema(project)
        sources = {s.id: s for s in project.sources()}
        known = statuses(project)
        with project.store.tx() as db:
            orphaned = {r["source_id"] for r in db.execute("SELECT DISTINCT source_id FROM source_chunks")} - set(sources)
        for sid in set(known) - set(sources) | orphaned:
            self._cancel(project.id, sid)
            forget(project, sid)
        model = self.embedder.model_id if self.embedder else None
        describe_now: bool | None = None  # asked once per pass, only if a source needs it
        for sid, source in sources.items():
            if not indexable(source):
                continue
            state = known.get(sid)
            queued = (project.id, sid) in self._queued
            if state is None or state.get("sha256") != source.sha256:
                if queued and state is not None and state.get("sha256") != source.sha256:
                    self._cancel(project.id, sid)  # a new version replaced the file being indexed
                    queued = False
                if not queued:
                    self.enqueue(project, sid)
            elif state.get("status") in ("queued", "indexing") and not queued:
                self.enqueue(project, sid)  # interrupted by a restart
            elif not queued and (
                    # drawings skipped because no vision model was available
                    (state.get("status") == "ready" and state.get("note") and state.get("drawings")
                     and not state.get("description_model"))
                    # an image nothing could read (no Docling, no vision model)
                    or (state.get("status") == "unsupported" and state.get("parser") == "none"
                        and source.kind == "image")):
                # Describe them once a vision model is available (e.g. supports_images was fixed or
                # the server now serves one).
                if describe_now is None:
                    describe_now = self.cfg.describe != "off" and self.describer.available
                if describe_now:
                    self.enqueue(project, sid)
            elif state.get("status") == "ready" and model and state.get("embedding") != model and not queued:
                self.enqueue(project, sid, embed_only=True)

    def enqueue(self, project: "Project", sid: str, *, embed_only: bool = False) -> None:
        key = (project.id, sid)
        with self._lock:
            if self._closed or key in self._queued:
                return
            token = self._queued[key] = CancelToken()
        if not embed_only:
            source = project.source(sid)
            _put_status(project, sid, {"status": "queued", "sha256": source.sha256, "message": "Waiting to be indexed"})
        self._work.submit(self._job, project, sid, embed_only, token)

    def reindex(self, project: "Project", sid: str) -> None:
        self._cancel(project.id, sid)
        forget(project, sid)
        self.enqueue(project, sid)

    def _cancel(self, project_id: str, sid: str) -> None:
        with self._lock:
            token = self._queued.pop((project_id, sid), None)
        if token is not None:
            token.cancel()

    def shutdown(self) -> None:
        if _ACTIVE.get(self.workspace.dir.resolve()) is self:
            del _ACTIVE[self.workspace.dir.resolve()]
        with self._lock:
            self._closed = True
            tokens = list(self._queued.values())
        for token in tokens:
            token.cancel()
        if self.docling:
            self.docling.stop()
        self._work.shutdown(wait=False, cancel_futures=True)
        self._maintain.shutdown(wait=False, cancel_futures=True)

    # -------------------------------------------------------------- job

    def _job(self, project: "Project", sid: str, embed_only: bool, token: CancelToken) -> None:
        try:
            if token.cancelled:
                return
            if embed_only:
                self._embed_missing(project, sid, token)
            else:
                self._index(project, sid, token)
        except Cancelled:
            pass
        except Exception as exc:  # noqa: BLE001 - recorded on the source
            log.exception("indexing %s failed", sid)
            if not token.cancelled and _exists(project, sid):
                _put_status(project, sid, {**(status(project, sid) or {}), "status": "failed",
                                           "error": f"{type(exc).__name__}: {exc}", "message": "Indexing failed"})
        finally:
            with self._lock:
                if self._queued.get((project.id, sid)) is token:
                    del self._queued[(project.id, sid)]

    def _index(self, project: "Project", sid: str, token: CancelToken) -> None:
        source = project.source(sid)
        data = project.source_file(sid).read_bytes()
        forget(project, sid, keep_status=True)
        state: dict[str, Any] = {"status": "indexing", "sha256": source.sha256, "chunks": 0, "message": "Reading"}
        if source.kind == "pdf":
            state |= {"pages_total": source.page_count, "pages_done": 0}
        _put_status(project, sid, state)

        def progress(message: str) -> None:
            if not token.cancelled:
                state["message"] = message
                _put_status(project, sid, state)

        parser, warning = self._parser_for(source, progress, token)
        state |= {"parser": parser, **({"warning": warning} if warning else {})}
        if parser == "none":
            _put_status(project, sid, {**state, "status": "unsupported", "message": warning or "Not indexed"})
            return
        seq = 0
        embed_error = None
        describing: dict[str, Any] = {"failed": None, "skipped": 0}
        for label, batch in self._parse(source, data, parser, progress, token):
            token.check()
            batch = batch + self._describe(source, data, batch, label, state, describing, progress, token)
            if not _current(project, sid, source.sha256):
                raise Cancelled()
            ids = _add_chunks(project, sid, batch, seq)
            seq += len(batch)
            if ids and self.embedder and embed_error is None:
                try:
                    _add_vectors(project, sid, self.embedder.model_id, ids,
                                 self.embedder.documents([c.embedding_text() for c in batch]))
                except EmbeddingError as exc:
                    embed_error = str(exc)
            state["chunks"] = seq
            if source.kind == "pdf" and label:
                state["pages_done"] = label
            progress(f"Indexed {seq} passage{'s' if seq != 1 else ''}"
                     + (f" from {state['pages_done']} of {source.page_count} pages" if source.kind == "pdf" else ""))
        token.check()
        final = {**state, "status": "ready", "embedding": self.embedder.model_id if self.embedder and not embed_error else None,
                 "message": f"{seq} passage{'s' if seq != 1 else ''}"}
        notes = [warning]
        if describing["failed"]:
            notes.append(f"Some drawing pages were not described ({describing['failed']}); reindex to try again")
        if describing["skipped"]:
            notes.append(f"{describing['skipped']} more drawing page{'s were' if describing['skipped'] != 1 else ' was'} "
                         f"not described (retrieval.describe_max_pages = {self.cfg.describe_max_pages})")
        if embed_error:
            notes.append(f"Keyword search only: {embed_error}")
        if any(notes):
            final["warning"] = "; ".join(filter(None, notes))
        if state.get("drawings") and not self.describer.available and self.cfg.describe != "off":
            final["note"] = self.describer.reason
        _put_status(project, sid, final)

    def _parser_for(self, source: Source, progress, token: CancelToken) -> tuple[str, str | None]:
        """docling, basic, or none (with the reason)."""
        suffix = Path(source.filename).suffix.lower()
        if source.kind == "document" and suffix not in DOCLING_DOCUMENTS:
            return "basic", None
        if self.docling is None:
            if source.kind != "image":
                return "basic", None
            if self.describer.available:
                return "describe", "Described only: labels are not read without the Docling parser"
            return "none", "Images need the Docling parser (retrieval.parser = \"docling\") or a vision model."
        try:
            self.docling.ensure(progress, token)
            return "docling", None
        except DoclingUnavailable as exc:
            if source.kind == "image":
                if self.describer.available:
                    return "describe", f"Described only, labels not read: {exc}"
                return "none", f"Not indexed: {exc}"
            return "basic", f"Embedded text only (no OCR, tables or drawings): {exc}"

    def _parse(self, source: Source, data: bytes, parser: str, progress, token: CancelToken):
        """(last page done, passages) per batch."""
        suffix = Path(source.filename).suffix.lower()
        if source.kind == "pdf":
            total = source.page_count or 0
            step = self.cfg.pages_per_batch
            for first in range(1, total + 1, step):
                last = min(total, first + step - 1)
                token.check()
                if parser == "docling":
                    doc = self._docling(data, source.filename, (first, last), progress, token)
                    yield last, [c for c in docling_chunks(doc) if c.page is None or first <= c.page <= last]
                else:
                    from ..documents import pdf_page
                    batch: list[Chunk] = []
                    for page in range(first, last + 1):
                        text, _ = pdf_page(data, page, render=False)
                        batch.extend(text_chunks(text, page=page))
                    yield last, batch
        elif source.kind == "image" and parser == "describe":
            yield None, []  # the description is the only passage
        elif source.kind == "image":
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(data)) as image:
                size = ImageOps.exif_transpose(image).size
            yield None, docling_chunks(self._docling(data, source.filename, None, progress, token), image_size=size)
        elif parser == "docling":
            yield None, docling_chunks(self._docling(data, source.filename, None, progress, token))
        else:
            from ..documents import document_text
            yield None, text_chunks(document_text(data, source.filename), markdown=suffix in MARKDOWN)

    def drawing_pages(self, source: Source, batch: list[Chunk], last_page: int | None) -> list[int | None]:
        """Pages of this batch to describe: every image; PDF pages that are scans (little embedded
        text) or hold a drawing (Docling found figure labels on them)."""
        if source.kind == "image":
            return [None]
        if source.kind != "pdf" or last_page is None:
            return []
        first = last_page - (last_page - 1) % self.cfg.pages_per_batch
        text = {page: 0 for page in range(first, last_page + 1)}
        drawings = set()
        for chunk in batch:
            if chunk.page in text:
                text[chunk.page] += len(chunk.text) if chunk.kind != "figure" else 0
                if chunk.kind == "figure" and chunk.labels:
                    drawings.add(chunk.page)
        return [page for page in text if page in drawings or text[page] < self.cfg.describe_min_text]

    def _describe(self, source: Source, data: bytes, batch: list[Chunk], last_page: int | None,
                  state: dict[str, Any], describing: dict[str, Any], progress, token: CancelToken) -> list[Chunk]:
        """Description passages for the drawing pages of a batch (none once a call has failed)."""
        pages = self.drawing_pages(source, batch, last_page)
        state["drawings"] = state.get("drawings", 0) + len(pages)
        if not pages or not self.describer.available or describing["failed"]:
            return []
        from ..documents import region_png
        from ..sources import SourceError
        out = []
        for page in pages:
            if state.get("described", 0) >= self.cfg.describe_max_pages:
                describing["skipped"] += 1
                continue
            token.check()
            where = f"page {page} of {source.filename}" if page else source.filename
            progress(f"Describing {where}")
            ocr = "\n".join(c.text for c in batch if c.page == page)
            try:
                png, _ = region_png(data, source.kind, page)
                description, labels, tokens = self.describer.describe(png, where, ocr, token)
            except (LLMError, SourceError) as exc:
                describing["failed"] = str(exc)[:200]
                log.warning("describing %s failed: %s", where, exc)
                break
            out.append(description_chunk(description, labels, self.describer.model_id, page))
            state["described"] = state.get("described", 0) + 1
            state["description_tokens"] = state.get("description_tokens", 0) + sum(tokens)
            state["description_input_tokens"] = state.get("description_input_tokens", 0) + tokens[0]
            state["description_output_tokens"] = state.get("description_output_tokens", 0) + tokens[1]
            state["description_model"] = self.describer.model_id
        return out

    def _docling(self, data: bytes, filename: str, pages, progress, token: CancelToken) -> dict[str, Any]:
        assert self.docling is not None
        progress("Parsing" + (f" pages {pages[0]}–{pages[1]}" if pages else ""))
        try:
            return self.docling.convert(data, filename, pages, progress, token)
        except DoclingFailed as exc:
            raise DoclingFailed(f"{filename}: {exc}") from None

    def _embed_missing(self, project: "Project", sid: str, token: CancelToken) -> None:
        if self.embedder is None:
            return
        model = self.embedder.model_id
        with project.store.tx() as db:
            rows = db.execute("SELECT c.id, c.body FROM source_chunks c LEFT JOIN chunk_vectors v "
                              "ON v.chunk_id = c.id AND v.model = ? WHERE c.source_id = ? AND v.chunk_id IS NULL "
                              "ORDER BY c.seq", (model, sid)).fetchall()
        state = status(project, sid) or {}
        try:
            for start in range(0, len(rows), 256):
                token.check()
                batch = rows[start:start + 256]
                texts = []
                for r in batch:
                    body = json.loads(r["body"])
                    texts.append(Chunk(body["kind"], body["text"], body.get("headings") or []).embedding_text())
                _add_vectors(project, sid, model, [r["id"] for r in batch], self.embedder.documents(texts))
        except EmbeddingError as exc:
            _put_status(project, sid, {**state, "embedding": None, "warning": f"Keyword search only: {exc}"})
            return
        with project.store.tx() as db:
            db.execute("DELETE FROM chunk_vectors WHERE source_id=? AND model<>?", (sid, model))
        if str(state.get("warning", "")).startswith("Keyword search only"):
            del state["warning"]
        _put_status(project, sid, {**state, "embedding": model})


def _exists(project: "Project", sid: str) -> bool:
    try:
        project.source(sid)
        return True
    except KeyError:
        return False


def _current(project: "Project", sid: str, sha256: str) -> bool:
    try:
        return project.source(sid).sha256 == sha256
    except KeyError:
        return False
