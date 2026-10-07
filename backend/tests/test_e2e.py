"""Browser smoke test: the built app, a live server and a scripted model.

Walks the assistant and review flows end to end in headless Chromium: repair-engine detail
on issues, inspector and RDF tab,
asking the assistant, answering its question with nothing selected (the conversation keeps its
selection and history), applying a proposed issue dismissal, and reopening it.

Needs the built frontend (cd frontend && npm run build) and a Playwright browser
(uv run playwright install chromium); skipped with a reason otherwise. Run it alone with
    uv run pytest -m e2e
"""

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
from workbench.api import create_app
from workbench.config import load_settings

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
SAMPLES = ROOT / "samples" / "ro-train"


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

    question = "Is the medium change across RO-1 expected?"

    def dismiss_ro1(messages):
        issue_id = re.search(r"\[(val-[0-9a-f]+)\][^\n]*RO-1", messages[0]["content"]).group(1)
        return {"action": "propose", "explanation": "RO-1's medium finding is expected, so it can be dismissed.",
                "operations": [], "dismiss_issues": [{"id": issue_id, "reason": "RO changes the medium by design."}]}

    llm.steps = [
        {"action": "propose", "explanation": "RO-1 has medium findings from the validator.", "operations": [],
         "questions": [question]},
        dismiss_ro1,
    ]

    page.add_init_script(f"localStorage.setItem('workbench.project', '{pid}')")
    page.goto(base)
    health = page.locator(".topbar .health")
    expect(health).to_contain_text("violation(s)")
    # Issues tab: the repair engine's summary under the validator message; clicking an issue
    # inspects its object without leaving the list
    health.click()
    tank = page.locator(".issue", has_text="TK-101")
    expect(tank.locator(".repair-line")).to_contain_text("have 0, need 1")
    tank.locator(".issue-message").click()
    expect(page.locator(".inspector h4")).to_contain_text("TK-101")
    expect(page.locator("nav.tabs button.active", has_text="Issues")).to_have_count(1)

    # Inspector, and the RDF on its own tab
    page.locator("nav.tabs button", has_text="Equipment").first.click()
    page.locator("table.data td", has_text="RO-1").first.click()
    expect(page.locator(".inspector h4")).to_contain_text("RO-1")
    expect(page.locator("pre.turtle")).to_have_count(0)
    page.locator(".drawer nav button", has_text="RDF").click()
    expect(page.locator("pre.turtle")).to_contain_text("RO-1")

    # Ask about the selected equipment: the reply asks a question
    composer = page.locator(".composer textarea")
    composer.fill("Is the RO-1 issue real?")
    composer.press("Enter")
    expect(page.locator(".msg.user").last).to_contain_text("Is the RO-1 issue real?")
    expect(page.locator(".needs-input")).to_contain_text(question)
    expect(page.locator(".composer-mode")).to_contain_text("Answering the assistant")

    # Answer with nothing selected: the message stays about RO-1 and carries the conversation
    page.locator(".composer-selection button", has_text="clear").click()
    expect(page.locator(".composer-selection")).to_contain_text("About (from the conversation)")
    expect(page.locator(".composer-selection .chip")).to_contain_text("RO-1")
    page.locator(".needs-input button", has_text="Answer below").click()
    page.keyboard.type("Yes, that is expected for reverse osmosis.")
    page.keyboard.press("Enter")
    card = page.locator(".proposal")
    card.get_by_text("Dismisses 1 issue", exact=True).click()
    expect(card.locator(".proposal-fixes")).to_contain_text("RO changes the medium by design.")
    expect(card.locator(".proposal-fixes")).to_contain_text("No new issues.")
    rdf = card.locator("details", has=page.locator("summary", has_text="Show RDF"))
    expect(rdf).not_to_have_attribute("open", "")
    rdf.locator("summary").click()
    expect(rdf.locator("pre")).to_be_visible()
    second_context = llm.seen[1][0]["content"]
    assert "EARLIER CONVERSATION" in second_context and question in second_context
    assert "(nothing selected" not in second_context

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
    expect(page.locator('.proposal')).to_contain_text('TK-100', timeout=60000)
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
    page.get_by_role('button', name='Apply change', exact=True).click()
    expect(page.locator('.toast')).to_contain_text('Applied', timeout=60000)
    page.locator('.model-pane nav.tabs').get_by_role('button', name=re.compile(r'^Equipment')).click()
    page.locator('.model-pane tbody tr', has_text='TK-100').click()
    expect(page.locator('.inspector').get_by_role('link', name='equipment.txt', exact=True)).to_be_visible()
    expect(page.locator('.inspector .evidence-excerpt', has_text='Tank TK-100 stores water.')).to_be_visible()
