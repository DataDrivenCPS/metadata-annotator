"""Browser smoke test: the built app, a live server and a scripted model.

Walks the assistant and review flows end to end in headless Chromium: repair-engine detail
on issues, inspector and RDF tab,
asking the assistant, answering its question with nothing selected (the conversation keeps its
selection and history), applying a proposed issue dismissal, and reopening it.

Needs the built frontend (cd frontend && npm run build) and a Playwright browser
(uv run playwright install chromium); skipped with a reason otherwise. Run it alone with
    uv run pytest -m e2e
"""

import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from test_agent import ScriptedLLM
from test_documents import text_pdf
from workbench.api import create_app
from workbench.config import load_settings

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
SAMPLES = ROOT / "samples" / "ro-train"
SHOTS = Path(os.environ["WORKBENCH_TEST_SCREENSHOTS"]) if os.environ.get("WORKBENCH_TEST_SCREENSHOTS") else None


def _check_frontend_build() -> None:
    index = FRONTEND / "dist" / "index.html"
    if not index.exists():
        pytest.skip("frontend is not built: cd frontend && npm run build")
    newest = max(p.stat().st_mtime for p in (FRONTEND / "src").rglob("*") if p.is_file())
    if index.stat().st_mtime < newest:
        pytest.fail("frontend/dist is older than frontend/src: cd frontend && npm run build")


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A running workbench whose model provider is a scripted model."""
    _check_frontend_build()
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    settings = load_settings()
    settings.cache_dir.mkdir(parents=True)
    for f in (ROOT / "workbench-data" / "cache").glob("*"):
        if f.is_file():  # reuse the vocabulary caches, as test_api does
            shutil.copy(f, settings.cache_dir / f.name)
    llm = ScriptedLLM([])
    llm.health = lambda: {"ok": True, "model": "scripted"}
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: llm)
    monkeypatch.setattr("workbench.api.make_client", lambda cfg: llm)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 180
    while True:
        try:
            if httpx.get(f"{base}/api/status").json()["vocabulary"]["ready"]:
                break
        except httpx.HTTPError:
            pass
        assert time.time() < deadline, "the workbench did not become ready"
        time.sleep(0.2)
    yield base, llm
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture
def page():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except sync_api.Error as exc:
            pytest.skip(f"no Playwright browser ({exc.message.splitlines()[0]}): uv run playwright install chromium")
        pg = browser.new_page(viewport={"width": 1500, "height": 950})
        errors: list[str] = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        pg.set_default_timeout(20000)
        yield pg
        browser.close()
        assert not errors, errors


def test_assistant_and_review_flow(live, page):
    from playwright.sync_api import expect

    base, llm = live
    # The other vocabularies keep loading in the background (a cold Brick resolve takes ~20 s
    # of parsing), which can slow these first requests past httpx's 5 s default.
    pid = httpx.post(f"{base}/api/projects", json={"name": "Smoke"}, timeout=120).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        httpx.post(f"{base}/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")},
                   timeout=120).raise_for_status()

    question = "Is TK-301 meant to be unpiped for now?"

    def dismiss_tk301(messages):
        issue_id = re.search(r"\[(val-[0-9a-f]+)\][^\n]*TK-301", messages[0]["content"]).group(1)
        return {"action": "propose", "explanation": "TK-301's missing port is expected, so it can be dismissed.",
                "operations": [], "dismiss_issues": [{"id": issue_id, "reason": "The concentrate tank is piped later."}]}

    llm.steps = [
        {"action": "propose", "explanation": "TK-301 has no connection point.", "operations": [],
         "questions": [question]},
        dismiss_tk301,
    ]

    page.add_init_script(f"localStorage.setItem('workbench.project', '{pid}')")
    page.goto(base)
    health = page.locator(".topbar .health")
    expect(health).to_contain_text("violation(s)")
    # Issues tab: the repair engine's summary under the validator message; clicking an issue
    # inspects its object without leaving the list
    health.click()
    tank = page.locator(".issue", has_text="TK-301")
    # the repair engine runs server-side when the list opens; slow CI runners need more than 5 s
    expect(tank.locator(".repair-line")).to_contain_text("have 0, need 1", timeout=60000)
    tank.locator(".issue-message").click()
    expect(page.locator(".inspector h4")).to_contain_text("TK-301")
    expect(page.locator("nav.tabs button.active", has_text="Issues")).to_have_count(1)

    # Inspector, and the RDF on its own tab
    page.locator("nav.tabs button", has_text="Equipment").first.click()
    page.locator("table.data td", has_text="TK-301").first.click()
    expect(page.locator(".inspector h4")).to_contain_text("TK-301")
    expect(page.locator("pre.turtle")).to_have_count(0)
    page.locator(".drawer nav button", has_text="RDF").click()
    expect(page.locator("pre.turtle")).to_contain_text("TK-301")

    # Ask about the selected equipment: the reply asks a question
    composer = page.locator(".composer textarea")
    composer.fill("Is the TK-301 issue real?")
    composer.press("Enter")
    expect(page.locator(".msg.user").last).to_contain_text("Is the TK-301 issue real?")
    expect(page.locator(".needs-input")).to_contain_text(question)
    expect(page.locator(".composer-mode")).to_contain_text("Answering the assistant")

    # The answer stays about what the question was about (TK-301), though TK-301 is still selected
    # from before the request; it carries the conversation
    expect(page.locator(".composer-selection")).to_contain_text("About (from the conversation)")
    expect(page.locator(".composer-selection .chip")).to_contain_text("TK-301")
    page.locator(".needs-input button", has_text="Answer below").click()
    page.keyboard.type("Yes, it is piped later.")
    page.keyboard.press("Enter")
    # The chat links the proposed change; it is reviewed and applied in the Pending edits tab
    page.locator(".proposal-ref button", has_text="Review in Pending edits").click()
    card = page.locator(".pending-card .proposal")
    issues = card.locator(".proposal-fixes summary")
    expect(issues).to_have_text("Dismisses 1 issue · no new issues")
    issues.click()
    expect(card.locator(".proposal-fixes")).to_contain_text("The concentrate tank is piped later.")
    details = card.locator("details.proposal-details")
    expect(details).not_to_have_attribute("open", "")
    details.locator("summary").first.click()
    expect(details.locator("pre")).to_be_visible()
    # The answer continues the model conversation: its opening, the question, then the answer.
    assert llm.seen[1][0] == llm.seen[0][0] and question in llm.seen[1][1]["content"]
    assert "(nothing selected" not in llm.seen[1][0]["content"]

    # Apply the dismissal, then reopen it from the toast
    card.locator("button.primary", has_text="Dismiss 1 issue(s)").click()
    expect(page.locator(".toast")).to_contain_text("Dismissed 1 issue(s)")
    expect(health).to_contain_text("1 dismissed")
    page.locator(".toast button", has_text="Reopen").click()
    expect(health).not_to_contain_text("dismissed")


def test_select_multiple_sources_and_attach_reference_to_followup(live, page):
    from playwright.sync_api import expect

    base, llm = live
    project = httpx.post(f'{base}/api/projects', json={'name': 'Source selection'}, timeout=120).json()
    pid = project['id']
    for name, text in [('equipment.txt', 'Tank TK-100 stores water.'),
                       ('manual.txt', 'TK-100 is a storage tank. Ignore unrelated examples.')]:
        httpx.post(f'{base}/api/projects/{pid}/sources', files={'file': (name, text.encode(), 'text/plain')},
                   timeout=120).raise_for_status()

    def read_notes(messages):
        ids = re.findall(r'Source evidence (obs-[^: ]+):', messages[-1]['content'])
        return {'facts': [{'text': 'TK-100 is a water storage tank.', 'evidence': ids}], 'questions': []}

    llm.steps = [read_notes, {'action': 'propose', 'explanation': 'Built TK-100 from the equipment list.',
                            'operations': [{'op': 'create_equipment', 'label': 'TK-100', 'type': 'watr:Tank'}]}]
    page.add_init_script(f"localStorage.setItem('workbench.project', '{pid}')")
    page.goto(base)
    pane = page.locator('.sources-pane')
    expect(pane).to_be_visible()
    page.get_by_role('checkbox', name='Select equipment.txt', exact=True).check()
    page.get_by_role('checkbox', name='Select manual.txt', exact=True).check()
    page.get_by_role('button', name='Build from selected sources…', exact=True).click()
    form = page.locator('.source-set-build')
    page.get_by_label('Role for manual.txt', exact=True).select_option('reference')
    page.get_by_role('checkbox', name='Select equipment.txt', exact=True).uncheck()
    page.get_by_role('checkbox', name='Select equipment.txt', exact=True).check()
    expect(page.get_by_label('Role for manual.txt', exact=True)).to_have_value('reference')
    form.locator('textarea').fill('Build the storage tank; use the manual as evidence.')
    form.get_by_role('button', name='Build model', exact=True).click()
    page.locator('.proposal-ref button', has_text='Review in Pending edits').click(timeout=60000)
    expect(page.locator('.pending-card .proposal')).to_contain_text('TK-100')
    proposals = httpx.get(f'{base}/api/projects/{pid}/proposals', timeout=120).json()
    assert len(proposals) == 1
    assert [s['role'] for s in proposals[0]['build_summary']['sources']] == ['input', 'reference']
    model = httpx.get(f'{base}/api/projects/{pid}/model', timeout=120).json()
    assert model['view']['equipment'] == []  # a proposal has not changed the model

    # Attach a reference to the reply without extracting it as a new build input.
    pane.get_by_role('button', name='Clear', exact=True).click()
    page.get_by_role('checkbox', name='Select manual.txt', exact=True).check()
    page.get_by_role('button', name='Build from selected sources…', exact=True).click()
    page.get_by_role('button', name='Use as evidence in chat', exact=True).click()
    expect(page.locator('.toast')).to_contain_text('attached as evidence')
    page.screenshot(path='/tmp/nawi-multiple-sources.png')
    llm.steps = [read_notes, {'action': 'propose', 'explanation': 'The manual agrees with the proposed tank.',
                            'operations': [], 'questions': ['What is its capacity?']}]
    page.locator('.composer textarea').fill('Check the proposed tank against this manual.')
    page.locator('.composer button.primary').click()
    expect(page.locator('.thread')).to_contain_text('What is its capacity?', timeout=60000)
    assert 'role: reference' in llm.seen[-2][0]['content']
    page.locator('.proposal-ref button', has_text='Review in Pending edits').last.click()
    page.locator('.pending-card', has_text='TK-100').first.get_by_role('button', name='Apply change', exact=True).click()
    expect(page.locator('.toast')).to_contain_text('Applied', timeout=60000)
    page.locator('.model-pane nav.tabs').get_by_role('button', name=re.compile(r'^Equipment')).click()
    page.locator('.model-pane tbody tr', has_text='TK-100').click()
    expect(page.locator('.inspector').get_by_role('button', name='equipment.txt', exact=True)).to_be_visible()
    expect(page.locator('.inspector .evidence-excerpt', has_text='Tank TK-100 stores water.')).to_be_visible()


def test_chat_choices_wait_in_pending_edits_and_settle_when_applied(live, page):
    from playwright.sync_api import expect

    base, llm = live
    pid = httpx.post(f"{base}/api/projects", json={"name": "Choices"}, timeout=120).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        httpx.post(f"{base}/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")},
                   timeout=120).raise_for_status()
    model = httpx.get(f"{base}/api/projects/{pid}/model", timeout=120).json()
    ct = next(p for p in model["view"]["points"] if p["label"].startswith("CT-201"))

    def op(unit):
        return [{"op": "update_point", "id": ct["id"], "unit": unit}]

    llm.steps = [{"action": "propose", "explanation": "The unit depends on the stream.", "operations": [], "choices": [
        {"question": "Which unit does CT-201 report?", "options": [
            {"label": "microsiemens per cm", "operations": op("unit:MicroS-PER-CentiM")},
            {"label": "millisiemens per cm", "operations": op("unit:MilliS-PER-CentiM")}]}]}]

    page.add_init_script(f"localStorage.setItem('workbench.project', '{pid}')")
    page.goto(base)
    composer = page.locator(".composer textarea")
    composer.fill("Fix the CT-201 unit")
    composer.press("Enter")
    choices = page.locator(".needs-input.choices")
    expect(choices).to_contain_text("Which unit does CT-201 report?", timeout=60000)
    expect(page.locator(".composer-mode")).to_contain_text("Answering the assistant")

    # Picking an option opens the choice in Pending edits, each option a proposed change to choose.
    choices.get_by_role("button", name=re.compile("microsiemens")).click()
    card = page.locator(".pending-card", has_text="Which unit does CT-201 report?")
    expect(card.locator(".proposal")).to_have_count(2)
    option = card.locator(".proposal", has_text="microsiemens")
    expect(option).to_contain_text("CT-201")
    option.get_by_role("button", name="Choose this", exact=True).click()
    expect(page.locator(".toast")).to_contain_text("Applied", timeout=60000)
    expect(choices).to_contain_text("✓ microsiemens per cm")
    statuses = [p["status"] for p in httpx.get(f"{base}/api/projects/{pid}/proposals", timeout=120).json()]
    assert sorted(statuses) == ["applied", "dismissed"]  # the other option was closed

    # A reload brings back neither option as an open card; the message still shows the answer.
    page.reload()
    expect(page.locator(".needs-input.choices")).to_contain_text("✓ microsiemens per cm")
    expect(page.locator(".proposal-actions button.primary")).to_have_count(0)


def test_checking_citations_opens_the_source_at_the_cited_place(live, page, tmp_path):
    from playwright.sync_api import expect

    base, llm = live
    pid = httpx.post(f"{base}/api/projects", json={"name": "Citations", "profile": "brick"}, timeout=120).json()["id"]

    def shot(name):  # WORKBENCH_TEST_SCREENSHOTS=<dir> keeps pictures of each step, to look at
        if SHOTS:
            page.screenshot(path=str(SHOTS / f"{name}.png"))

    def upload(name, data, media):
        response = httpx.post(f"{base}/api/projects/{pid}/sources", files={"file": (name, data, media)}, timeout=120)
        response.raise_for_status()
        return response.json()["id"]

    pdf = upload("plant.pdf", text_pdf("Page one: P-1", "Page two: TK-2"), "application/pdf")
    image = upload("diagram.png", (SAMPLES / "diagram.png").read_bytes(), "image/png")
    notes = upload("notes.txt", b"First paragraph.\nPump P-1 feeds tank TK-2.\nA last paragraph, with more words in it.", "text/plain")
    points = upload("points.csv", b"Name\nFIRST\nSECOND\nTHIRD\n", "text/csv")
    grid = httpx.get(f"{base}/api/projects/{pid}/sources/{points}/grid", timeout=120).json()
    httpx.post(f"{base}/api/projects/{pid}/sources/{points}/confirm", json={
        **grid["config"], "layout": "row_points", "header_row": 0, "first_data_row": 1, "name_column": 0,
    }, timeout=120).raise_for_status()
    records = httpx.get(f"{base}/api/projects/{pid}/sources/{points}/observations", timeout=120).json()
    second = next(r for r in records if r["content"]["name"] == "SECOND")

    head = httpx.get(f"{base}/api/projects/{pid}/model", timeout=120).json()["head"]
    edited = httpx.post(f"{base}/api/projects/{pid}/edits", json={"base_revision": head, "operations": [
        {"op": "create_equipment", "label": "FROM-CSV", "type": "brick:AHU", "evidence": [second["id"]]}]}, timeout=120)
    edited.raise_for_status()
    head = edited.json()["revision"]["id"]
    model = httpx.get(f"{base}/api/projects/{pid}/model", timeout=120).json()
    eid = next(e["id"] for e in model["view"]["equipment"] if e["label"] == "FROM-CSV")

    # A build from page two of the PDF, citing what it read: checked before it is applied.
    def propose(messages):
        ids = re.findall(r"Source evidence (obs-[^: ]+):", messages[-1]["content"])
        return {"action": "propose", "explanation": "Page two names the air handler.", "operations": [
            {"op": "create_equipment", "label": "TK-2", "type": "brick:AHU", "evidence": ids},
            {"op": "update_equipment", "id": eid, "label": "RENAMED"}]}

    llm.steps = [propose]
    llm.supports_images = True
    httpx.post(f"{base}/api/projects/{pid}/build", json={
        "base_revision": head, "source_ids": [pdf], "source_pages": {pdf: [2]}}, timeout=120).raise_for_status()

    page.add_init_script(f"localStorage.setItem('workbench.project', '{pid}')")
    page.goto(base)
    # Reviewed in Pending edits: the Evidence line is on the proposal's card there.
    page.locator(".proposal-ref button", has_text="Review in Pending edits").click(timeout=60000)
    summary = page.locator(".pending-card .proposal summary", has_text="1 of 2 changes link to a source")
    expect(summary).to_be_visible(timeout=60000)
    summary.click()
    sources = page.locator(".proposal-sources")
    expect(sources.locator("section", has_text="RENAMED")).to_contain_text("No source linked.")
    sources.get_by_role("button", name=re.compile(r"plant\.pdf · page 2")).click()
    tray = page.locator(".sources-pane")
    expect(tray.locator(".citation-banner")).to_contain_text("plant.pdf · page 2")
    expect(tray.locator(".document-navigation")).to_contain_text("Page 2 of 2")
    shown = tray.locator(".document-canvas img")
    expect(shown).to_have_attribute("alt", "plant.pdf, page 2")
    page.wait_for_function("(img) => img.complete && img.naturalWidth > 0", arg=shown.element_handle())
    # The files the build read, listed under Details, open in the tray too rather than downloading.
    page.locator(".pending-card .proposal summary", has_text="Details").click()
    page.locator(".proposal-details section", has_text="Sources").get_by_role("button", name="plant.pdf", exact=True).click()
    expect(tray.locator(".citation-banner")).to_contain_text("plant.pdf · page 2")
    expect(tray.locator(".document-navigation")).to_contain_text("Page 2 of 2")
    labels = [e["label"] for e in httpx.get(f"{base}/api/projects/{pid}/model", timeout=120).json()["view"]["equipment"]]
    assert labels == ["FROM-CSV"]  # checking its sources has applied nothing
    shot("citation-proposal")

    # The inspector's citations open at the place too. Passages with a box come from the document
    # index, so this entity's evidence is shaped like them here.

    def boxed(route):
        detail = route.fetch().json()
        record = dict(detail["evidence"][0])
        detail["evidence"] += [
            {**record, "id": "obs-box", "source_id": pdf, "location": {"kind": "document", "page": 1},
             "content": {"filename": "plant.pdf", "text": "A description of the page.", "generated": True,
                         "box_pt": [72, 72, 144, 72]}},
            {**record, "id": "obs-region", "source_id": image, "location": {"kind": "image_region", "bbox": [400, 150, 300, 150]},
             "content": {"filename": "diagram.png"}},
            {**record, "id": "obs-passage", "source_id": notes, "location": {"kind": "document", "text_range": [17, 42]},
             "content": {"filename": "notes.txt", "text": "Pump P-1 feeds tank TK-2."}}]
        route.fulfill(json=detail)

    page.route(f"**/api/projects/{pid}/entities/{eid}*", boxed)
    page.reload()
    page.locator(".model-pane nav.tabs").get_by_role("button", name=re.compile(r"^Equipment")).click()
    page.locator(".model-pane tbody tr", has_text="FROM-CSV").click()
    inspector = page.locator(".inspector")

    inspector.get_by_role("button", name=re.compile(r"points\.csv · row")).click()
    row = tray.locator("tr.cited")
    expect(row).to_contain_text("SECOND")
    expect(tray.locator("tr.cited")).to_have_count(1)
    shot("citation-csv")

    inspector.get_by_role("button", name=re.compile(r"plant\.pdf · page 1")).click()
    expect(tray.locator(".document-navigation")).to_contain_text("Page 1 of 2")
    expect(tray.locator(".citation-banner")).to_contain_text("model's description")
    box = tray.locator(".document-canvas .region.cited")
    expect(box).to_be_visible()
    size = httpx.get(f"{base}/api/projects/{pid}/sources/{pdf}/document-preview?page=1", timeout=120).json()["page_size"]
    canvas, drawn = tray.locator(".document-canvas").bounding_box(), box.bounding_box()
    assert abs((drawn["x"] - canvas["x"]) / canvas["width"] - 72 / size[0]) < 0.01
    assert abs((drawn["y"] - canvas["y"]) / canvas["height"] - 72 / size[1]) < 0.01
    shot("citation-pdf-box")

    inspector.get_by_role("button", name=re.compile(r"diagram\.png")).click()
    expect(tray.locator(".image-canvas .region.cited")).to_be_visible()
    picture = tray.locator(".image-canvas img")
    page.wait_for_function("(img) => img.complete && img.naturalWidth > 0", arg=picture.element_handle())
    assert tray.locator(".citation-banner").bounding_box()["height"] < 60  # a line, not stretched by the tray
    shot("citation-image")

    inspector.get_by_role("button", name=re.compile(r"notes\.txt")).click()
    expect(tray.locator(".document-text mark")).to_have_text("Pump P-1 feeds tank TK-2.")

    tray.locator(".citation-banner").get_by_role("button", name="clear", exact=True).click()
    expect(tray.locator(".document-text mark")).to_have_count(0)
    inspector.get_by_role("button", name=re.compile(r"diagram\.png")).click()
    tray.locator(".citation-banner").get_by_role("button", name="clear", exact=True).click()
    expect(tray.locator(".region.cited")).to_have_count(0)
