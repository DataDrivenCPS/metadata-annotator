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
    pid = httpx.post(f"{base}/api/projects", json={"name": "Smoke"}).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        httpx.post(f"{base}/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")}).raise_for_status()

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
    expect(card.locator(".dismissals")).to_contain_text("RO changes the medium by design.")
    second_context = llm.seen[1][0]["content"]
    assert "EARLIER CONVERSATION" in second_context and question in second_context
    assert "(nothing selected" not in second_context

    # Apply the dismissal, then reopen it from the toast
    card.locator("button.primary", has_text="Dismiss 1 issue(s)").click()
    expect(page.locator(".toast")).to_contain_text("Dismissed 1 issue(s)")
    expect(health).to_contain_text("1 dismissed")
    page.locator(".toast button", has_text="Reopen").click()
    expect(health).not_to_contain_text("dismissed")
