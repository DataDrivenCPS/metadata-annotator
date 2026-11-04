"""Source indexing and search: chunking, the background indexer, and per-message evidence.

Offline by default: Docling's output comes from fixtures captured from docling-serve, and a
hashing embedder stands in for the model. ``test_real_docling_and_embeddings`` runs the real
parser and embedding model when WORKBENCH_TEST_RETRIEVAL is set (first run installs ~2 GB).
"""
from __future__ import annotations

import dataclasses
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from workbench.config import EmbeddingConfig, RetrievalConfig, load_settings
from workbench.retrieval import index as idx
from workbench.retrieval.chunking import MAX_CHARS, cluster_labels, docling_chunks, text_chunks
from workbench.retrieval.docling import DoclingUnavailable
from workbench.runs import RunManager, attachment_note
from workbench.schemas import AgentRun, SelectionScope, SourceRegion

from test_documents import text_pdf

FIXTURES = Path(__file__).parent / "fixtures"


class HashEmbedder:
    """Bag of words hashed into 64 dimensions: similar wording, similar vectors."""

    def __init__(self, model_id: str = "test:hash"):
        self.model_id = model_id
        self.calls = 0

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(64, np.float32)
        for word in re.findall(r"\w+", text.lower()):
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % 64] += 1
        return v / (np.linalg.norm(v) or 1)

    def documents(self, texts):
        self.calls += 1
        return np.array([self._vector(t) for t in texts])

    def query(self, text):
        return self._vector(text)


class FakeDocling:
    """Answers with captured docling-serve output instead of running it."""

    def __init__(self, documents=None, unavailable: str | None = None):
        self.documents = documents or {}
        self.unavailable = unavailable
        self.converted: list[tuple[str, tuple | None]] = []

    def ensure(self, progress, cancel):
        if self.unavailable:
            raise DoclingUnavailable(self.unavailable)
        return "http://fake", None

    def convert(self, data, filename, pages, progress, cancel):
        self.converted.append((filename, pages))
        return self.documents[Path(filename).suffix]

    def stop(self):
        pass


@pytest.fixture
def indexer(workspace, tmp_path):
    settings = dataclasses.replace(load_settings(), data_dir=tmp_path / "data",
                                   retrieval=RetrievalConfig(parser="basic", embedding=EmbeddingConfig(kind="none"),
                                                             pages_per_batch=2))
    ix = idx.Indexer(settings, workspace)
    ix.embedder = HashEmbedder()
    # Never the configured provider: a live local vision server would be sent the test drawings.
    ix.describer = FakeDescriber(reason="Tests describe drawings only with a fake model.")
    workspace.bus.listen(ix.on_event)
    yield ix
    ix.shutdown()


def settled(project, ids, timeout=30):
    """Statuses once every listed source has finished (ready, failed or unsupported)."""
    deadline = time.time() + timeout
    while True:
        states = idx.statuses(project)
        if all(states.get(i, {}).get("status") in ("ready", "failed", "unsupported") for i in ids):
            return states
        assert time.time() < deadline, states
        time.sleep(0.05)


def png(width=320, height=200):
    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


# ------------------------------------------------------------------ chunking

def test_text_chunks_keep_offsets_and_markdown_sections():
    text = "# Pumps\n\nP-101 is the feed pump.\n\nIt runs at 50 Hz.\n\n## Filters\n\nCF-101 removes particles."
    chunks = text_chunks(text, markdown=True)
    assert [c.headings for c in chunks] == [["Pumps"], ["Pumps", "Filters"]]
    for c in chunks:
        assert text[c.text_range[0]:c.text_range[1]] == c.text
    long = ("word " * 900).strip()
    pieces = text_chunks(long)
    assert len(pieces) > 1 and all(len(p.text) <= MAX_CHARS for p in pieces)
    assert all(long[p.text_range[0]:p.text_range[1]] == p.text for p in pieces)


def test_drawing_text_becomes_labels_with_pixel_boxes():
    doc = json.loads((FIXTURES / "docling-diagram.json").read_text())
    chunks = docling_chunks(doc, image_size=(1105, 482))
    assert [c.kind for c in chunks] == ["figure"]
    labels = {label["text"]: label["bbox"] for label in chunks[0].labels}
    for name in ("FEED PUMP P-101", "RAW WATER TANK TK-101", "CARTRIDGE FILTER CF-101", "RO SKID RO-1",
                 "CONCENTRATE TANK TK-301", "PT 101"):
        assert name in labels, sorted(labels)
    x, y, w, h = labels["FEED PUMP P-101"]  # drawn at about (195-315, 185-270) in the 1105x482 image
    assert 190 < x < 230 and 200 < y < 230 and w < 120 and h < 60
    assert chunks[0].headings == ["BRACKISH WATER RO TRAIN - PROCESS FLOW (SAMPLE)"]


def test_word_tables_become_rows_keyed_by_column():
    doc = json.loads((FIXTURES / "docling-manual-docx.json").read_text())
    chunks = docling_chunks(doc)
    table = next(c for c in chunks if c.kind == "table")
    assert table.text.splitlines() == ["Tag: FIT-101 | Description: Feed flow | Units: gpm",
                                       "Tag: PIT-201 | Description: HP pump discharge pressure | Units: psi"]
    assert table.headings == ["Operations Manual", "3 Instrumentation", "3.1 Point list"]
    assert chunks[0].text == "The feed flow is measured upstream of the cartridge filter."


def test_label_clustering_joins_stacked_and_adjacent_words_only():
    words = [("FEED", [10, 10, 40, 10]), ("PUMP", [55, 10, 40, 10]), ("P-101", [25, 23, 40, 10]),
             ("L-01", [200, 10, 30, 10])]
    assert [label["text"] for label in cluster_labels(words)] == ["FEED PUMP P-101", "L-01"]


# ------------------------------------------------------------------- indexer

def test_upload_indexes_in_background_and_search_finds_tags(sample_project, indexer):
    pdf = sample_project.add_source("manual.pdf", text_pdf("Feed pump P-101 discharges to RO-1_FIT_101.",
                                                           "Antiscalant pump AS-1 doses upstream.",
                                                           "The concentrate valve CV-301 throttles reject flow."))
    notes = sample_project.add_source("notes.md", b"# Tanks\n\nTK-201 stores permeate.\n\nTK-301 holds concentrate.")
    csv = sample_project.add_source("points.csv", b"name\nFIT-101\n")
    states = settled(sample_project, [pdf.id, notes.id])
    assert states[pdf.id]["status"] == "ready" and states[pdf.id]["pages_done"] == 3
    assert states[pdf.id]["parser"] == "basic" and states[pdf.id]["embedding"] == "test:hash"
    assert csv.id not in states  # point lists are already structured records

    hits = idx.search(sample_project, "RO-1_FIT_101", embedder=indexer.embedder)
    top = idx.chunks(sample_project, [hits[0].chunk_id])[hits[0].chunk_id]
    assert top["page"] == 1 and "RO-1_FIT_101" in top["text"] and hits[0].keyword_rank == 1
    from workbench.documents import pdf_page
    page_text, _ = pdf_page(sample_project.source_file(pdf.id).read_bytes(), 1, render=False)
    assert page_text[top["text_range"][0]:top["text_range"][1]] == top["text"]  # the viewer's offsets

    hits = idx.search(sample_project, "which tank stores permeate", source_ids=[notes.id], embedder=indexer.embedder)
    found = idx.chunks(sample_project, [h.chunk_id for h in hits])
    assert {c["source_id"] for c in found.values()} == {notes.id}
    assert found[hits[0].chunk_id]["headings"] == ["Tanks"] and hits[0].vector_rank is not None

    assert idx.search(sample_project, "permeate", source_ids=[pdf.id]) == []  # scoped to the asked sources


def test_changed_and_removed_sources_are_kept_current(sample_project, indexer):
    src = sample_project.add_source("notes.txt", b"Pump P-1 feeds tank TK-1.")
    settled(sample_project, [src.id])
    # A new version of the file (as the delete-files branch's replace does): reindexed.
    path = sample_project.source_file(src.id)
    path.write_bytes(b"Pump P-2 feeds tank TK-2.")
    sample_project._put_source(src.model_copy(update={"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    deadline = time.time() + 30
    while not idx.search(sample_project, "P-2"):
        assert time.time() < deadline
        time.sleep(0.05)
    assert idx.search(sample_project, "P-1") == []
    # Removing the source (its rows and files) drops its passages and status.
    with sample_project.store.tx() as db:
        db.execute("DELETE FROM sources WHERE id=?", (src.id,))
    sample_project.bus.publish(sample_project.id, {"type": "sources"})
    deadline = time.time() + 30
    while idx.statuses(sample_project):
        assert time.time() < deadline
        time.sleep(0.05)
    with sample_project.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM source_chunks").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0] == 0


def test_new_embedding_model_reembeds_without_reparsing(sample_project, indexer):
    src = sample_project.add_source("notes.txt", b"Pump P-1 feeds tank TK-1.\n\nValve V-7 isolates it.")
    settled(sample_project, [src.id])
    indexer.embedder = HashEmbedder("test:other")
    indexer.maintain(sample_project)
    deadline = time.time() + 30
    while idx.status(sample_project, src.id).get("embedding") != "test:other":
        assert time.time() < deadline
        time.sleep(0.05)
    with sample_project.store.tx() as db:
        models = {r[0] for r in db.execute("SELECT DISTINCT model FROM chunk_vectors")}
    assert models == {"test:other"}


def test_interrupted_indexing_resumes(sample_project, indexer):
    src = sample_project.add_source("notes.txt", b"Pump P-1 feeds tank TK-1.")
    settled(sample_project, [src.id])
    idx._put_status(sample_project, src.id, {"status": "indexing", "sha256": src.sha256})  # a restart mid-way
    indexer.maintain(sample_project)
    assert settled(sample_project, [src.id])[src.id]["status"] == "ready"


def test_docling_drawings_and_pdf_page_batches(sample_project, indexer):
    diagram = json.loads((FIXTURES / "docling-diagram.json").read_text())
    indexer.docling = FakeDocling({".png": diagram, ".pdf": diagram})
    image = sample_project.add_source("pid.png", png(1105, 482))
    pdf = sample_project.add_source("drawings.pdf", text_pdf("one", "two", "three"))
    states = settled(sample_project, [image.id, pdf.id])
    assert states[image.id]["parser"] == "docling" and states[image.id]["status"] == "ready"
    hits = idx.search(sample_project, "FEED PUMP P-101", source_ids=[image.id])
    chunk = idx.chunks(sample_project, [hits[0].chunk_id])[hits[0].chunk_id]
    assert chunk["kind"] == "figure" and any(label["text"] == "FEED PUMP P-101" for label in chunk["labels"])
    # PDFs go to the parser a few pages at a time; passages outside a batch's pages are not repeated.
    assert [p for f, p in indexer.docling.converted if f == "drawings.pdf"] == [(1, 2), (3, 3)]
    assert states[pdf.id]["chunks"] == 1  # the fixture's only page (1) falls in the first batch


def test_without_docling_text_falls_back_and_images_wait(sample_project, indexer):
    indexer.docling = FakeDocling(unavailable="no network")
    image = sample_project.add_source("pid.png", png())
    pdf = sample_project.add_source("manual.pdf", text_pdf("Feed pump P-101."))
    states = settled(sample_project, [image.id, pdf.id])
    assert states[image.id]["status"] == "unsupported" and "no network" in states[image.id]["message"]
    assert states[pdf.id]["status"] == "ready" and states[pdf.id]["parser"] == "basic"
    assert "no network" in states[pdf.id]["warning"]


# ------------------------------------------------------- per-message evidence

def test_follow_ups_do_not_reread_attachments_but_remember_them(sample_project, tmp_path):
    from workbench.agent.guidance import SkillGuidance
    from workbench.events import EventBus

    pdf = sample_project.add_source("manual.pdf", text_pdf("a", "b", "c", "d"))
    manager = RunManager(load_settings(), SkillGuidance(load_settings().skill_dir), EventBus())
    parent = AgentRun(id="run-1", kind="correction", input_revision=sample_project.head(), provider="p", model="m",
                      skill_version="", status="succeeded", instruction="Which pumps are listed?",
                      selection=SelectionScope(source_regions=[SourceRegion(source_id=pdf.id, pages=[2, 3])]),
                      outcome={"explanation": "P-101 and P-201."})
    manager.save(sample_project, parent)
    assert attachment_note(sample_project, parent) == "\n(Attached to this message and read then: manual.pdf, pages 2–3.)"
    follow_up = manager.continued_selection(sample_project, "run-1", SelectionScope())
    assert follow_up.source_regions == []
    history = manager.history(sample_project, "run-1")
    assert history[0]["text"].endswith("(Attached to this message and read then: manual.pdf, pages 2–3.)")


# ------------------------------------------------------------- real models

@pytest.mark.skipif(not os.environ.get("WORKBENCH_TEST_RETRIEVAL"), reason="set WORKBENCH_TEST_RETRIEVAL=1 to run")
def test_real_docling_and_embeddings(sample_project, workspace):
    """Installs docling-serve and the embedding model into workbench-data/retrieval (once)."""
    settings = load_settings()
    settings = dataclasses.replace(settings, retrieval=RetrievalConfig(pages_per_batch=2))
    ix = idx.Indexer(settings, workspace)
    workspace.bus.listen(ix.on_event)
    try:
        diagram = sample_project.add_source("diagram.png", (Path(__file__).resolve().parents[2] / "samples" /
                                                            "ro-train" / "diagram.png").read_bytes())
        manual = sample_project.add_source("manual.docx", (FIXTURES / "manual.docx").read_bytes())
        pdf = sample_project.add_source("spec.pdf", text_pdf("Feed pump P-101 discharges to RO-1_FIT_101.",
                                                             "Antiscalant pump AS-1 doses upstream.",
                                                             "The concentrate valve CV-301 throttles reject flow."))
        states = settled(sample_project, [diagram.id, manual.id, pdf.id], timeout=3600)
        for sid in (diagram.id, manual.id, pdf.id):
            assert states[sid]["status"] == "ready" and states[sid]["parser"] == "docling", states[sid]
            assert states[sid]["embedding"] == "fastembed:BAAI/bge-small-en-v1.5", states[sid]
        hits = idx.search(sample_project, "feed pump", source_ids=[diagram.id], embedder=ix.embedder)
        top = idx.chunks(sample_project, [hits[0].chunk_id])[hits[0].chunk_id]
        assert any("FEED PUMP" in label["text"] for label in top["labels"])
        hits = idx.search(sample_project, "units of the HP pump discharge pressure", embedder=ix.embedder)
        assert "PIT-201" in idx.chunks(sample_project, [hits[0].chunk_id])[hits[0].chunk_id]["text"]
        pages = {c["page"] for c in idx.chunks(sample_project, [h.chunk_id for h in
                                                                idx.search(sample_project, "CV-301 P-101 AS-1",
                                                                           source_ids=[pdf.id], k=10)]).values()}
        assert pages == {1, 2, 3}
    finally:
        ix.shutdown()


# --------------------------------------------------------------------- API

from test_api import client  # noqa: E402,F401 - the app fixture


def test_api_reports_index_status_and_searches(client):  # noqa: F811
    pid = client.post("/api/projects", json={"name": "Retrieval"}).json()["id"]
    sid = client.post(f"/api/projects/{pid}/sources",
                      files={"file": ("notes.md", b"# Valves\n\nCV-301 throttles the concentrate.", "text/markdown")}).json()["id"]
    deadline = time.time() + 30
    while (index := next(s for s in client.get(f"/api/projects/{pid}/sources").json() if s["id"] == sid)["index"]) is None \
            or index["status"] != "ready":
        assert time.time() < deadline, index
        time.sleep(0.05)
    assert index["chunks"] == 1 and index["parser"] == "basic" and index["embedding"] is None  # tests: keywords only
    hits = client.get(f"/api/projects/{pid}/sources/search", params={"q": "CV-301", "source_id": sid}).json()
    start, end = hits[0]["text_range"]
    assert hits[0]["filename"] == "notes.md" and hits[0]["headings"] == ["Valves"]
    assert "# Valves\n\nCV-301 throttles the concentrate."[start:end] == hits[0]["text"] == "CV-301 throttles the concentrate."
    assert client.post(f"/api/projects/{pid}/sources/{sid}/reindex").json()["status"] == "queued"
    csv = client.post(f"/api/projects/{pid}/sources", files={"file": ("p.csv", b"name\nA\n", "text/csv")}).json()["id"]
    assert client.post(f"/api/projects/{pid}/sources/{csv}/reindex").status_code == 400


# ------------------------------------------------- parser sharing between workbenches

def _fake_parser(key: str):
    """docling-serve's health check and an authenticated probe: enough for ownership checks."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/health":
                self.send_response(200)
            else:
                self.send_response(404 if self.headers.get("X-Api-Key") == key else 401)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("owner_alive", [True, False])
def test_a_second_workbench_shares_a_live_owners_parser_and_never_stops_it(tmp_path, owner_alive):
    import subprocess
    import sys

    from workbench.llm import CancelToken
    from workbench.retrieval.docling import STALE_S, DoclingService

    server = _fake_parser("k")
    stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])  # the parser's process
    try:
        root = tmp_path / "docling-serve"
        root.mkdir()
        state = root / "server.json"
        state.write_text(json.dumps({"pid": stand_in.pid, "port": server.server_address[1], "key": "k",
                                     "version": RetrievalConfig().docling_version}))
        if not owner_alive:  # the owner stopped touching it: restarted or crashed
            os.utime(state, (time.time() - STALE_S - 5, time.time() - STALE_S - 5))
        service = DoclingService(RetrievalConfig(), root)
        url, key = service.ensure(lambda m: None, CancelToken())
        assert url.endswith(str(server.server_address[1])) and key == "k"
        assert service._owner is (not owner_alive)
        service.stop()
        time.sleep(0.3)
        assert (stand_in.poll() is None) is owner_alive  # shared: left running; taken over: stopped
        assert state.exists() is owner_alive
    finally:
        stand_in.kill()
        server.shutdown()


def test_an_owner_leaves_its_parser_running_when_another_workbench_uses_it(tmp_path):
    import subprocess
    import sys

    from workbench.llm import CancelToken
    from workbench.retrieval.docling import DoclingService

    server = _fake_parser("k")
    stand_in = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        root = tmp_path / "docling-serve"
        root.mkdir()
        (root / "server.json").write_text(json.dumps({"pid": stand_in.pid, "port": server.server_address[1],
                                                      "key": "k", "version": RetrievalConfig().docling_version}))
        os.utime(root / "server.json", (0, 0))  # stale: this workbench becomes the owner
        owner = DoclingService(RetrievalConfig(), root)
        owner.ensure(lambda m: None, CancelToken())
        assert owner._owner
        (root / "server.used").write_text(json.dumps({"pid": os.getpid() + 1, "at": time.time()}))  # someone else
        owner.stop()
        time.sleep(0.3)
        assert stand_in.poll() is None and (root / "server.json").exists()
    finally:
        stand_in.kill()
        server.shutdown()


# ------------------------------------------------- the assistant reads the index

from test_agent import ScriptedLLM  # noqa: E402
from workbench.agent.correction import run_correction  # noqa: E402
from workbench.agent.guidance import SkillGuidance  # noqa: E402
from workbench.agent.multisource import run_source_build, run_with_evidence  # noqa: E402
from workbench.config import AgentConfig  # noqa: E402
from workbench.llm import CancelToken  # noqa: E402
from workbench.operations import OperationList  # noqa: E402
from workbench.schemas import CsvImportConfig  # noqa: E402

FILLER = ("Chiller CH-{0} rejects heat through cooling tower CT-{0}; its condenser water pump runs at constant speed "
          "and the operator checks approach temperature every shift. Strainers are cleaned quarterly and the "
          "bearings are greased according to the lubrication schedule in appendix {0}. ",)


def long_manual(fact: str) -> bytes:
    """A manual well over the size supplied whole, with ``fact`` in one paragraph near the end."""
    paragraphs = [FILLER[0].format(i) * 2 for i in range(40)]
    paragraphs.insert(31, fact)
    return "\n\n".join(paragraphs).encode()


def guidance():
    return SkillGuidance(load_settings().skill_dir)


def with_air_handler(workspace):
    p = workspace.create("Indexed evidence", "brick")
    rev, _ = p.edit(p.head(), OperationList.validate_python([
        {"op": "create_equipment", "label": "A1", "type": "brick:AHU"}]))
    return p, rev, p.view(rev.id).equipment[0]


def cited(messages):
    return re.findall(r"\b(obs-[A-Za-z0-9-]+-[0-9a-f]{16})\b", messages[-1]["content"])


def test_attached_indexed_manual_supplies_relevant_passages_without_a_notes_pass(workspace, indexer):
    p, rev, equipment = with_air_handler(workspace)
    manual = p.add_source("manual.txt", long_manual("Air handler A1 is named Supply air handler on the drawings."))
    settled(p, [manual.id])

    def change(messages):
        content = messages[0]["content"]
        assert "SOURCE PASSAGES" in content and "Supply air handler" in content
        assert content.count("Source evidence obs-") <= 6 < idx.status(p, manual.id)["chunks"]  # excerpts only
        oid = next(o for o in cited([messages[0]]) if "Supply air handler" in p.store.get_body("observations", o)["content"]["text"])
        return {"action": "propose", "operations": [
            {"op": "update_equipment", "id": equipment.id, "label": "Supply air handler", "evidence": [oid]}]}

    llm = ScriptedLLM([change])  # one reply: the correction itself, no source-notes call
    outcome = run_with_evidence(p, llm, guidance(), rev.id,
                                SelectionScope(entity_ids=[equipment.id], source_regions=[SourceRegion(source_id=manual.id)]),
                                "What is the drawing name of this air handler?", None, lambda *_: None, CancelToken(),
                                agent=AgentConfig())
    assert outcome.steps == 1
    applied = p.apply_proposal(outcome.proposal.id)
    oid = p.view(applied.id).equipment[0].evidence[0]
    observation = p.store.get_body("observations", oid)
    text = p.source_file(manual.id).read_text()
    start, end = observation["location"]["text_range"]
    assert "Supply air handler" in text[start:end] == observation["content"]["text"]  # opens at the passage
    assert any(ref.ref == oid for ref in outcome.proposal.evidence)
    # Asking again cites the same observation.
    again = run_with_evidence(p, ScriptedLLM([change]), guidance(), rev.id,
                              SelectionScope(entity_ids=[equipment.id], source_regions=[SourceRegion(source_id=manual.id)]),
                              "What is the drawing name of this air handler?", None, lambda *_: None, CancelToken(),
                              agent=AgentConfig())
    assert again.proposal.operations[0].evidence == [oid]


def test_unindexed_and_exact_attachments_are_still_read_into_notes(workspace, indexer):
    p, rev, equipment = with_air_handler(workspace)
    manual = p.add_source("manual.txt", long_manual("Air handler A1 serves the east wing."))
    settled(p, [manual.id])
    excerpt = p.add_source("excerpt.txt", b"A1 is named Supply air handler.")
    # The person pointed at an exact passage: it is read as is, not retrieved.
    regions = [SourceRegion(source_id=manual.id), SourceRegion(source_id=excerpt.id, text_range=[0, 31])]
    read = []

    def notes(messages):
        read.append(messages[-1]["content"])
        ids = re.findall(r"Source evidence (obs-[^: ]+):", messages[-1]["content"])
        return {"facts": [{"text": "A1 is named Supply air handler.", "evidence": ids}], "questions": []}

    def change(messages):
        content = messages[0]["content"]
        assert "east wing" in content and "A1 is named Supply air handler" in content
        return {"action": "propose", "operations": [
            {"op": "update_equipment", "id": equipment.id, "label": "Supply air handler"}]}

    run_with_evidence(p, ScriptedLLM([notes, change]), guidance(), rev.id,
                      SelectionScope(entity_ids=[equipment.id], source_regions=regions),
                      "Name this air handler", None, lambda *_: None, CancelToken(), agent=AgentConfig())
    assert len(read) == 1 and "excerpt.txt" in read[0] and "east wing" not in read[0]


def test_search_sources_finds_passages_the_assistant_can_cite(workspace, indexer):
    p, rev, equipment = with_air_handler(workspace)
    manual = p.add_source("manual.txt", long_manual("Air handler A1 is named Supply air handler on the drawings."))
    settled(p, [manual.id])

    def invented(messages):
        return {"action": "propose", "operations": [
            {"op": "update_equipment", "id": equipment.id, "label": "Supply air handler", "evidence": ["obs-invented"]}]}

    def change(messages):
        found = [m for m in messages if "Supply air handler" in m["content"] and "observation_id" in m["content"]]
        assert found, "the search result reaches the model"
        oid = cited([found[-1]])[0]
        return {"action": "propose", "operations": [
            {"op": "update_equipment", "id": equipment.id, "label": "Supply air handler", "evidence": [oid]}]}

    llm = ScriptedLLM([{"action": "search_sources", "args": {"query": "A1 drawing name", "source": "manual.txt"}},
                       invented, change])
    outcome = run_correction(p, llm, guidance(), rev.id, SelectionScope(entity_ids=[equipment.id]),
                             "Rename this air handler to its drawing name", None, lambda *_: None, CancelToken())
    assert "search_sources(query, source?)" in llm.systems[0]
    assert any("cite only observation ids" in m["content"] for m in llm.seen[2])  # the invented id was refused
    oid = outcome.proposal.operations[0].evidence[0]
    assert "Supply air handler" in p.store.get_body("observations", oid)["content"]["text"]
    assert [ref.ref for ref in outcome.proposal.evidence if ref.kind == "observation"] == [oid]


def test_without_indexed_documents_the_tool_is_not_offered(workspace, indexer):
    p, rev, equipment = with_air_handler(workspace)
    llm = ScriptedLLM([{"action": "propose", "operations": []}])
    run_correction(p, llm, guidance(), rev.id, SelectionScope(entity_ids=[equipment.id]), "Anything?", None,
                   lambda *_: None, CancelToken())
    assert "search_sources" not in llm.systems[0]


def test_builds_retrieve_reference_passages_for_each_input_batch(workspace, indexer):
    p = workspace.create("Build with manual", "brick")
    csv = p.add_source("points.csv", b"Name\nA1:Temp\n")
    p.confirm_csv_mapping(csv.id, CsvImportConfig(layout="row_points", delimiter=",", header_row=0,
                                                  first_data_row=1, name_column=0))
    drawing = p.add_source("equipment.txt", b"A1 and A2 are air handlers.")
    manual = p.add_source("manual.txt", long_manual("Temp on air handlers means zone air temperature."))
    settled(p, [manual.id])
    regions = [SourceRegion(source_id=csv.id, role="input"), SourceRegion(source_id=drawing.id, role="input"),
               SourceRegion(source_id=manual.id, role="reference")]
    read = []

    def notes(messages):
        read.append(messages[-1]["content"])
        ids = re.findall(r"Source evidence (obs-[^: ]+):", messages[-1]["content"])
        return {"facts": [{"text": "A1 and A2 are air handlers.", "evidence": ids}], "questions": []}

    def extract(messages):
        content = messages[0]["content"]
        assert "zone air temperature" in content  # the reference passage reached this batch
        oid = next(o for o in cited([messages[0]])
                   if "zone air" in p.store.get_body("observations", o)["content"]["text"])
        return {"action": "propose", "operations": [
            {"op": "create_equipment", "label": "A2", "type": "brick:AHU", "evidence": [oid]}]}

    llm = ScriptedLLM([
        notes,
        {"pattern": r"(?P<equipment>A\d+):(?P<point>.+)", "equipment_from": "group:equipment",
         "token_from": "group:point", "units_from": "none"},
        {"action": "map", "mappings": [{"id": "T1", "point_type": "brick:Zone_Air_Temperature_Sensor"}]},
        {"action": "map", "mappings": [{"id": "G1", "type": "brick:AHU"}]},
        extract,
    ])
    outcome = run_source_build(p, llm, guidance(), p.head(), regions, "Build the air handlers", None,
                               lambda *_: None, CancelToken(), AgentConfig())
    assert len(read) == 1 and "manual.txt" not in read[0]  # the manual is retrieved, not read whole
    assert all("zone air temperature" in system for system in llm.systems[1:4])  # point mapping saw it
    proposal = outcome.proposal
    assert "Reference passages:" in proposal.build_summary["source_notes"]
    assert any(ref.summary.startswith("manual.txt") for ref in proposal.evidence)


# ------------------------------------------------ drawings: descriptions and views

from workbench.config import ProviderConfig  # noqa: E402
from workbench.llm import LLMError  # noqa: E402
from workbench.retrieval.evidence import search_passages  # noqa: E402

PROSE_PAGE = ("The feed pump P-101 supplies the reverse osmosis train through the cartridge filters; "
              "operators check the differential pressure across the filters every shift and replace "
              "the cartridges when the pressure drop exceeds the limit stated in the maintenance section.")


class FakeDescriber:
    model_id = "test:vision"

    def __init__(self, fail: str | None = None, reason: str | None = None):
        self.fail, self.reason, self.calls = fail, reason, []

    @property
    def available(self):
        return self.reason is None

    def describe(self, png, where, ocr_text, cancel):
        self.calls.append((where, Image.open(io.BytesIO(png)).size, ocr_text))
        if self.fail:
            raise LLMError(self.fail)
        return f"Process diagram: centrifugal pump P-101 discharges to skid RO-1 ({where}).", ["P-101", "RO-1"], (600, 300)


def test_scanned_and_drawing_pages_are_described_once(sample_project, indexer):
    indexer.describer = FakeDescriber()
    # The test PDF draws one line per page, so a page of prose is the part of it on the page.
    indexer.cfg = dataclasses.replace(indexer.cfg, describe_min_text=40)
    pdf = sample_project.add_source("drawings.pdf", text_pdf(PROSE_PAGE, "P-101", PROSE_PAGE))
    states = settled(sample_project, [pdf.id])
    state = states[pdf.id]
    assert [where for where, _, _ in indexer.describer.calls] == ["page 2 of drawings.pdf"]  # only the scan-like page
    _, size, ocr = indexer.describer.calls[0]
    assert max(size) == 1568 and ocr == "P-101"  # the whole page, sharp; its few words as a hint
    assert state["described"] == 1 and state["description_tokens"] == 900 and state["description_model"] == "test:vision"
    results, refs = search_passages(sample_project, "centrifugal pump", indexer.embedder)
    assert "centrifugal" in results[0]["text"] and "generated" in results[0]["source"]
    observation = sample_project.store.get_body("observations", refs[0].ref)
    assert observation["content"]["generated"] is True and observation["location"]["page"] == 2


def test_images_are_described_even_without_docling(sample_project, indexer):
    indexer.describer = FakeDescriber()
    image = sample_project.add_source("pid.png", png(1105, 482))
    state = settled(sample_project, [image.id])[image.id]
    assert state["status"] == "ready" and state["parser"] == "describe" and state["chunks"] == 1
    assert "labels are not read" in state["warning"]
    assert idx.search(sample_project, "centrifugal", source_ids=[image.id])


def test_description_failures_and_limits_leave_the_text_indexed(sample_project, indexer):
    indexer.describer = FakeDescriber(fail="model unavailable")
    pdf = sample_project.add_source("scans.pdf", text_pdf("A", "B", "C"))
    state = settled(sample_project, [pdf.id])[pdf.id]
    assert state["status"] == "ready" and len(indexer.describer.calls) == 1  # stops asking after a failure
    assert "model unavailable" in state["warning"] and "reindex" in state["warning"]

    indexer.describer = FakeDescriber()
    indexer.cfg = dataclasses.replace(indexer.cfg, describe_max_pages=2)
    indexer.reindex(sample_project, pdf.id)
    state = settled(sample_project, [pdf.id])[pdf.id]
    assert state["described"] == 2 and "1 more drawing page was not described" in state["warning"]

    indexer.describer = FakeDescriber(reason="the model provider 'local' does not accept images")
    other = sample_project.add_source("more-scans.pdf", text_pdf("D"))
    assert "does not accept images" in settled(sample_project, [other.id])[other.id]["note"]


def test_drawings_skipped_without_a_vision_model_are_described_once_one_is_available(sample_project, indexer):
    indexer.describer = FakeDescriber(reason="the model provider 'local' does not accept images")
    scan = sample_project.add_source("scan.pdf", text_pdf("D"))
    image = sample_project.add_source("pid.png", png(1105, 482))
    skipped = settled(sample_project, [scan.id, image.id])
    assert skipped[scan.id]["drawings"] and "does not accept images" in skipped[scan.id]["note"]
    assert skipped[image.id]["status"] == "unsupported"  # no Docling here: nothing could read it
    indexer.maintain(sample_project)  # still no vision model: nothing to redo
    assert settled(sample_project, [scan.id])[scan.id]["note"] == skipped[scan.id]["note"]

    indexer.describer = FakeDescriber()  # e.g. the server now serves a vision model
    indexer.maintain(sample_project)
    time.sleep(0.2)  # let the queued jobs start
    described = settled(sample_project, [scan.id, image.id])
    assert described[scan.id]["description_model"] == "test:vision" and "note" not in described[scan.id]
    assert described[image.id]["status"] == "ready" and described[image.id]["parser"] == "describe"
    calls = len(indexer.describer.calls)
    indexer.maintain(sample_project)  # described: not again
    time.sleep(0.2)
    assert len(indexer.describer.calls) == calls == 2


class VisionLLM(ScriptedLLM):
    supports_images = True
    prompt_cache = False

    def __init__(self, steps, prompt_cache=False):
        super().__init__(steps)
        self.prompt_cache = prompt_cache
        self.attached: list[list] = []  # the request-level images of each call

    def complete_json(self, system, messages, schema, **kw):
        self.attached.append(list(kw.get("images") or []))
        return super().complete_json(system, messages, schema, **kw)


def test_the_assistant_can_look_at_a_region_and_only_the_latest_stays_attached(workspace, indexer):
    p, rev, equipment = with_air_handler(workspace)
    drawing = p.add_source("drawing.pdf", text_pdf("A1 supply fan"))
    settled(p, [drawing.id])
    passage = search_passages(p, "supply fan", indexer.embedder)[1][0].ref
    llm = VisionLLM([
        {"action": "view_source", "args": {"source": "drawing.pdf", "page": 1, "box": [60, 80, 120, 30]}},
        {"action": "view_source", "args": {"observation_id": passage}},
        {"action": "view_source", "args": {"source": "nothing.pdf"}},
        {"action": "propose", "operations": []},
    ])
    run_correction(p, llm, guidance(), rev.id, SelectionScope(entity_ids=[equipment.id]), "What is A1?", None,
                   lambda *_: None, CancelToken())
    assert "view_source(observation_id | source, page?, box?)" in llm.systems[0]
    first = llm.seen[1][-1]
    assert "region [" in first["content"] and len(first["images"]) == 1
    assert Image.open(io.BytesIO(first["images"][0].data)).size[0] > 120  # zoomed in, not page-sized
    second = llm.seen[2]
    assert "images" not in second[-3] and "no longer attached" not in second[-3]["content"]
    assert len(second[-1]["images"]) == 1 and "drawing.pdf, page 1" in second[-1]["content"]
    assert "earlier view_source is no longer attached" in second[-1]["content"]
    assert "no PDF or image 'nothing.pdf'" in llm.seen[3][-1]["content"] and "images" not in llm.seen[3][-1]
    # Once the model has replied to a view, its image is not sent again (no prompt cache to make it cheap)
    assert sum(1 for m in llm.seen[3] if m.get("images")) == 0
    assert "earlier view_source is no longer attached" in llm.seen[3][-1]["content"]

    # With prompt caching the latest view stays: cached input is cheap, and removing it would break the prefix.
    cached = VisionLLM([{"action": "view_source", "args": {"source": "drawing.pdf", "page": 1}},
                        {"action": "find_entities", "args": {"query": "A1"}},
                        {"action": "propose", "operations": []}], prompt_cache=True)
    run_correction(p, cached, guidance(), rev.id, SelectionScope(entity_ids=[equipment.id]), "What is A1?", None,
                   lambda *_: None, CancelToken())
    assert sum(1 for m in cached.seen[2] if m.get("images")) == 1

    text_only = ScriptedLLM([{"action": "propose", "operations": []}])
    run_correction(p, text_only, guidance(), rev.id, SelectionScope(entity_ids=[equipment.id]), "What is A1?", None,
                   lambda *_: None, CancelToken())
    assert "view_source" not in text_only.systems[0]


def test_images_attach_to_the_turn_that_carries_them():
    from workbench.llm import ImageInput
    from workbench.llm.litellm_client import LiteLLMClient
    client = LiteLLMClient(ProviderConfig(name="v", kind="openai", model="m", base_url="http://x/v1", supports_images=True))
    params = client._params("sys", [{"role": "user", "content": "first"}, {"role": "assistant", "content": "{}"},
                                    {"role": "user", "content": "viewed", "images": [ImageInput(data=b"png")]}],
                            {"type": "object"}, [ImageInput(data=b"page")], 100)
    first, _, viewed = params["messages"][1:]
    assert [part["type"] for part in first["content"]] == ["image_url", "text"]
    assert [part["type"] for part in viewed["content"]] == ["image_url", "text"]
    assert viewed["content"][1]["text"] == "viewed"
    text_only = LiteLLMClient(ProviderConfig(name="t", kind="openai", model="m", base_url="http://x/v1"))
    with pytest.raises(LLMError, match="does not accept images"):
        text_only._params("sys", [{"role": "user", "content": "x", "images": [ImageInput(data=b"png")]}], {}, None, 10)


@pytest.mark.llm
@pytest.mark.skipif(not os.environ.get("WORKBENCH_TEST_PROVIDER"), reason="set WORKBENCH_TEST_PROVIDER to run")
def test_real_vision_model_describes_the_sample_drawing():
    """Real model: the RO train drawing's description names its feed pump tag."""
    from workbench.documents import region_png
    from workbench.retrieval.describe import Describer
    settings = load_settings(os.environ.get("WORKBENCH_CONFIG"))
    settings = dataclasses.replace(settings, retrieval=dataclasses.replace(
        settings.retrieval, describe="auto", describe_provider=os.environ["WORKBENCH_TEST_PROVIDER"]))
    describer = Describer(settings)
    if not describer.available:
        pytest.skip(describer.reason)
    data = (Path(__file__).resolve().parents[2] / "samples" / "ro-train" / "diagram.png").read_bytes()
    png_bytes, _ = region_png(data, "image")
    description, labels, tokens = describer.describe(png_bytes, "diagram.png", "", CancelToken())
    assert "P-101" in description + " ".join(labels) and tokens > 0


def test_attached_pictures_go_with_the_request_beside_their_passages(workspace, indexer):
    indexer.describer = FakeDescriber()
    p, rev, equipment = with_air_handler(workspace)
    image = p.add_source("pid.png", png(1105, 482))
    settled(p, [image.id])
    selection = SelectionScope(entity_ids=[equipment.id], source_regions=[SourceRegion(source_id=image.id)])

    def answer(messages):
        assert "ATTACHED IMAGES" in messages[0]["content"] and "image 1 is pid.png" in messages[0]["content"]
        assert "centrifugal pump" in messages[0]["content"]  # the passages too
        return {"action": "propose", "operations": []}

    vision = VisionLLM([answer])
    run_with_evidence(p, vision, guidance(), rev.id, selection, "What does the drawing show?", None,
                      lambda *_: None, CancelToken(), agent=AgentConfig())
    assert len(vision.attached[0]) == 1 and Image.open(io.BytesIO(vision.attached[0][0].data)).size == (1105, 482)

    text_only = ScriptedLLM([{"action": "propose", "operations": []}])
    run_with_evidence(p, text_only, guidance(), rev.id, selection, "What does the drawing show?", None,
                      lambda *_: None, CancelToken(), agent=AgentConfig())
    assert "ATTACHED IMAGES" not in text_only.seen[0][0]["content"]
