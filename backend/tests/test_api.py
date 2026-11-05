"""API smoke tests: the HTTP layer maps service errors correctly and round-trips edits."""

import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import load_settings

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "ro-train"


@pytest.fixture
def client(tmp_path, monkeypatch, registry):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    settings = load_settings()
    # The session's vocabularies: an app per test would load its own, and FastAPI's caches keep
    # finished apps (and so their vocabularies) alive.
    app = create_app(settings, registry)
    with TestClient(app) as c:
        deadline = time.time() + 120
        while not c.get("/api/status").json()["vocabulary"]["ready"]:
            assert time.time() < deadline
            time.sleep(0.2)
        yield c


def test_edit_undo_stale_and_export(client):
    pid = client.post("/api/projects", json={"name": "API test"}).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        r = client.post(f"/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")})
    assert r.status_code == 200, r.text
    model = client.get(f"/api/projects/{pid}/model").json()
    head = model["head"]
    points = {p["label"]: p for p in model["view"]["points"]}
    eq = {e["label"].split()[0]: e for e in model["view"]["equipment"]}

    r = client.post(f"/api/projects/{pid}/edits", json={"base_revision": head, "operations": [
        {"op": "update_point", "id": points["FT-201"]["id"], "equipment": eq["RO-1"]["id"]}]})
    assert r.status_code == 200, r.text
    new_head = r.json()["revision"]["id"]

    # stale base -> 409 with both revisions named
    r = client.post(f"/api/projects/{pid}/edits", json={"base_revision": head, "operations": [
        {"op": "update_point", "id": points["FT-301"]["id"], "equipment": eq["RO-1"]["id"]}]})
    assert r.status_code == 409 and r.json()["head"] == new_head

    # malformed -> 422 with problems
    r = client.post(f"/api/projects/{pid}/edits", json={"base_revision": new_head, "operations": [
        {"op": "update_point", "id": "pt-missing", "unit": "unit:PSI"}]})
    assert r.status_code == 422 and r.json()["problems"]

    ent = client.get(f"/api/projects/{pid}/entities/{points['FT-201']['id']}").json()
    assert "hasProperty" in ent["turtle"] and ent["locked"] == ["equipment"]

    ttl = client.get(f"/api/projects/{pid}/export.ttl")
    assert ttl.headers["x-revision"] == new_head

    assert client.post(f"/api/projects/{pid}/undo").json()["head"] == head
    assert client.get(f"/api/projects/{pid}/export.ttl").headers["x-revision"] == head

    s = client.post(f"/api/projects/{pid}/selection/describe",
                    json={"entity_ids": [points["FT-201"]["id"], points["FT-301"]["id"]], "field_ids": ["equipment"]})
    assert s.json()["summary"] == "Equipment assignments for 2 points"


def test_source_upload_preview_confirm(client):
    pid = client.post("/api/projects", json={"name": "Sources"}).json()["id"]
    with open(SAMPLES / "points_wide.csv", "rb") as f:
        src = client.post(f"/api/projects/{pid}/sources", files={"file": ("points_wide.csv", f, "text/csv")}).json()
    grid = client.get(f"/api/projects/{pid}/sources/{src['id']}/grid").json()
    assert grid["suggested"]["layout"] == "column_points" and not grid["confirmed"]
    prev = client.post(f"/api/projects/{pid}/sources/{src['id']}/preview", json=grid["config"]).json()
    assert prev["total"] == 6
    assert client.post(f"/api/projects/{pid}/sources/{src['id']}/confirm", json=grid["config"]).json()["observations"] == 6
    obs = client.get(f"/api/projects/{pid}/sources/{src['id']}/observations").json()
    assert obs[0]["content"]["name"] == "LT-101" and obs[0]["location"]["column"] == 1
    listed = client.get(f"/api/projects/{pid}/sources").json()
    assert listed[0]["observation_count"] == 6 and listed[0]["status"] == "configured"
    with open(SAMPLES / "diagram.png", "rb") as f:
        img = client.post(f"/api/projects/{pid}/sources", files={"file": ("diagram.png", f, "image/png")}).json()
    assert img["kind"] == "image" and img["width"] > 0
    assert client.get(f"/api/projects/{pid}/sources/{img['id']}/file").headers["content-type"] == "image/png"
    bad = client.post(f"/api/projects/{pid}/sources", files={"file": ("x.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 400


def test_pdf_preview_build_and_review_flow(client, monkeypatch):
    from test_agent import ScriptedLLM
    from test_documents import text_pdf

    pid = client.post('/api/projects', json={'name': 'Document model'}).json()['id']
    response = client.post(f'/api/projects/{pid}/sources', files={
        'file': ('plant.pdf', text_pdf('Page one: P-1', 'Page two: TK-2'), 'application/pdf'),
    })
    assert response.status_code == 200, response.text
    source = response.json()
    sid = source['id']
    assert source['kind'] == 'pdf' and source['page_count'] == 2
    preview = client.get(f'/api/projects/{pid}/sources/{sid}/document-preview?page=2')
    assert 'TK-2' in preview.json()['text']
    rendered = client.get(f'/api/projects/{pid}/sources/{sid}/pages/2.png')
    assert rendered.status_code == 200 and rendered.headers['content-type'] == 'image/png'
    assert client.get(f'/api/projects/{pid}/sources/{sid}/pages/0.png').status_code == 400
    assert client.get(f'/api/projects/{pid}/sources/{sid}/file').headers['content-type'] == 'application/pdf'
    llm = ScriptedLLM([{'action': 'propose', 'explanation': 'The source identifies TK-2 as a tank.',
                        'operations': [{'op': 'create_equipment', 'label': 'DOC-TK-2', 'type': 'watr:Tank'}]}])
    llm.supports_images = True
    llm.health = lambda: {'ok': True}
    monkeypatch.setattr('workbench.runs.make_client', lambda cfg: llm)
    manager = client.app.state.runs
    monkeypatch.setattr(manager.pool, 'submit', lambda fn, *args: fn(*args))
    base = client.get(f'/api/projects/{pid}/model').json()['head']
    body = {'base_revision': base, 'source_ids': [sid], 'source_pages': {sid: [2]}}
    invalid = client.post(f'/api/projects/{pid}/build', json={**body, 'source_pages': {sid: [3]}})
    assert invalid.status_code == 400
    reference_only = client.post(f'/api/projects/{pid}/build', json={**body, 'source_roles': {sid: 'reference'}})
    assert reference_only.status_code == 400
    assert 'at least one build input' in reference_only.json()['detail']
    duplicate = client.post(f'/api/projects/{pid}/build', json={**body, 'source_ids': [sid, sid]})
    assert duplicate.status_code == 400
    response = client.post(f'/api/projects/{pid}/build', json=body)
    assert response.status_code == 200, response.text
    run = client.get(f'/api/projects/{pid}/runs/{response.json()["id"]}').json()
    assert run['status'] == 'succeeded', run['error']
    proposal_id = run['outcome']['proposal_id']
    proposal = client.get(f'/api/projects/{pid}/proposals/{proposal_id}').json()
    assert proposal['kind'] == 'build'
    assert proposal['selection']['source_regions'][0]['pages'] == [2]
    assert 'Page two: TK-2' in llm.seen[0][0]['content']
    assert 'Page one: P-1' not in llm.seen[0][0]['content']
    assert client.get(f'/api/projects/{pid}/model').json()['head'] == base
    applied = client.post(f'/api/projects/{pid}/proposals/{proposal_id}/apply')
    assert applied.status_code == 200, applied.text
    rows = client.get(f'/api/projects/{pid}/model').json()['view']['equipment']
    assert any(row['label'] == 'DOC-TK-2' and row['evidence'] for row in rows)


def test_answering_questions_continues_the_conversation(client, monkeypatch):
    from test_agent import ScriptedLLM

    pid = client.post("/api/projects", json={"name": "Follow-up"}).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        client.post(f"/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")})
    model = client.get(f"/api/projects/{pid}/model").json()
    head = model["head"]
    ct = next(p for p in model["view"]["points"] if p["label"].startswith("CT-201"))
    llm = ScriptedLLM([
        {"action": "propose", "explanation": "CT-201 could be permeate or feed conductivity.", "operations": [],
         "questions": ["Which stream does CT-201 measure?"]},
        {"action": "propose", "explanation": "Permeate conductivity is reported in uS/cm.",
         "operations": [{"op": "update_point", "id": ct["id"], "unit": "unit:MicroS-PER-CentiM"}]},
    ])
    llm.health = lambda: {"ok": True}
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: llm)
    monkeypatch.setattr(client.app.state.runs.pool, "submit", lambda fn, *args: fn(*args))

    first = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ct["id"]]}, "instruction": "Fix the CT-201 unit"}).json()
    first = client.get(f"/api/projects/{pid}/runs/{first['id']}").json()
    assert first["outcome"]["questions"] and not first["outcome"]["proposal_id"]

    r = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ct["id"]]}, "instruction": "It is permeate.",
        "parent_run_id": first["id"]})
    assert r.status_code == 200, r.text
    second = client.get(f"/api/projects/{pid}/runs/{r.json()['id']}").json()
    assert second["status"] == "succeeded", second["error"]
    assert second["parent_run_id"] == first["id"]
    assert [t["role"] for t in second["conversation"]] == ["user", "assistant"]
    # The follow-up continues the model conversation: the same opening, the reply, then the new turn.
    assert second["continued"] and llm.seen[1][0] == llm.seen[0][0] and len(llm.seen[1]) == 3
    assert "Which stream does CT-201 measure?" in llm.seen[1][1]["content"]
    assert llm.seen[1][2]["content"].startswith("Request: It is permeate.")
    assert "EARLIER CONVERSATION" not in llm.seen[1][2]["content"]
    proposal = client.get(f"/api/projects/{pid}/proposals/{second['outcome']['proposal_id']}").json()
    assert [t["text"] for t in proposal["conversation"]][0] == "Fix the CT-201 unit"
    assert proposal["conversation"][-2]["text"] == "It is permeate."

    unknown = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "instruction": "again", "parent_run_id": "run-missing"})
    assert unknown.status_code == 400


def test_chat_choices_become_options_and_a_new_request_closes_the_open_proposal(client, monkeypatch):
    from test_agent import ScriptedLLM

    pid = client.post("/api/projects", json={"name": "Choices"}).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        client.post(f"/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")})
    model = client.get(f"/api/projects/{pid}/model").json()
    head = model["head"]
    ct = next(p for p in model["view"]["points"] if p["label"].startswith("CT-201"))

    def op(unit):
        return [{"op": "update_point", "id": ct["id"], "unit": unit}]

    llm = ScriptedLLM([
        {"action": "propose", "explanation": "CT-201's stream decides its unit.", "operations": [], "choices": [
            {"question": "Which unit does CT-201 report?", "options": [
                {"label": "uS/cm", "operations": op("unit:MicroS-PER-CentiM")},
                {"label": "mS/cm", "operations": op("unit:MilliS-PER-CentiM")},
                {"label": "Not a unit", "operations": op("unit:Nope")}]},
            {"question": "Is it calibrated?", "options": [{"label": "Yes", "operations": []}, {"label": "No", "operations": []}]},
        ]},
        {"action": "propose", "explanation": "Something else.", "operations": [], "questions": ["Anything else?"]},
    ])
    llm.health = Mock(return_value={"ok": True})
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: llm)
    monkeypatch.setattr(client.app.state.runs.pool, "submit", lambda fn, *args: fn(*args))

    first = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ct["id"]]}, "instruction": "Fix the CT-201 unit"}).json()
    first = client.get(f"/api/projects/{pid}/runs/{first['id']}").json()
    assert first["status"] == "succeeded", first["error"]
    [choice] = first["outcome"]["choices"]  # the choice without operations stays a question
    assert choice["question"] == "Which unit does CT-201 report?"
    assert [o["label"] for o in choice["options"]] == ["uS/cm", "mS/cm"]  # the unknown unit is dropped
    assert first["outcome"]["questions"] == ["Is it calibrated?\n• Yes\n• No"]
    option = client.get(f"/api/projects/{pid}/proposals/{choice['options'][0]['proposal_id']}").json()
    assert option["status"] == "pending" and option["agent_run_id"] == first["id"]
    assert option["operations"][0]["unit"].endswith("MicroS-PER-CentiM")

    # Starting a new request closes the proposal the person had open, instead of leaving it pending.
    second = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "instruction": "Something else", "supersedes_proposal_id": option["id"]})
    assert second.status_code == 200, second.text
    assert client.get(f"/api/projects/{pid}/proposals/{option['id']}").json()["status"] == "dismissed"
    # The second start reused the recent health check instead of probing the endpoint again.
    assert llm.health.call_count == 1


def test_follow_up_without_selection_keeps_the_conversation_selection(client, monkeypatch):
    from test_agent import ScriptedLLM

    pid = client.post("/api/projects", json={"name": "Follow-up selection"}).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        client.post(f"/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")})
    model = client.get(f"/api/projects/{pid}/model").json()
    head = model["head"]
    ro = next(e for e in model["view"]["equipment"] if e["label"].startswith("RO-1"))
    asked = {"action": "propose", "explanation": "Is this expected?", "operations": [], "questions": ["Expected?"]}
    llm = ScriptedLLM([asked, asked, asked])
    llm.health = lambda: {"ok": True}
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: llm)
    monkeypatch.setattr(client.app.state.runs.pool, "submit", lambda fn, *args: fn(*args))

    first = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ro["id"]]}, "instruction": "Check RO-1"}).json()
    # nothing selected now: the follow-up is still about RO-1
    second = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "instruction": "Yes, expected.", "parent_run_id": first["id"]}).json()
    assert second["selection"]["entity_ids"] == [ro["id"]]
    assert "(nothing selected" not in llm.seen[1][0]["content"]
    # a new selection wins
    p201 = next(e for e in model["view"]["equipment"] if e["label"].startswith("P-201"))
    third = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [p201["id"]]}, "instruction": "And this one?",
        "parent_run_id": second["id"]}).json()
    assert third["selection"]["entity_ids"] == [p201["id"]]
    # the person dropped the conversation's scope: this reply is about nothing in particular
    fourth = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "instruction": "In general?", "parent_run_id": third["id"],
        "inherit_selection": False}).json()
    assert fourth["selection"]["entity_ids"] == []


def _project_with_ct(client):
    pid = client.post("/api/projects", json={"name": "Continue"}).json()["id"]
    with open(SAMPLES / "model.ttl", "rb") as f:
        client.post(f"/api/projects/{pid}/import", files={"file": ("model.ttl", f, "text/turtle")})
    model = client.get(f"/api/projects/{pid}/model").json()
    ct = next(p for p in model["view"]["points"] if p["label"].startswith("CT-201"))
    return pid, model["head"], ct


def _scripted(client, monkeypatch, steps):
    from test_agent import ScriptedLLM
    llm = ScriptedLLM(steps)
    llm.health = lambda: {"ok": True}
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: llm)
    monkeypatch.setattr(client.app.state.runs.pool, "submit", lambda fn, *args: fn(*args))
    return llm


ASK = {"action": "propose", "explanation": "Which stream?", "operations": [], "questions": ["Which stream?"]}


def test_a_follow_up_keeps_what_the_model_looked_up(client, monkeypatch):
    pid, head, ct = _project_with_ct(client)
    search = {"action": "search_terms", "args": {"query": "conductivity", "kind": "quantity_kind"}}
    llm = _scripted(client, monkeypatch, [search, ASK, ASK])
    first = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ct["id"]]}, "instruction": "Check CT-201"}).json()
    client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "instruction": "It is permeate.", "parent_run_id": first["id"]})
    sent = [m["content"] for m in llm.seen[2]]
    assert any(c.startswith("Result of search_terms") for c in sent)  # the earlier lookup is still there
    changes = client.get(f"/api/projects/{pid}/runs/{first['id']}/changes").json()
    assert changes == {"changed": False, "summary": "", "continuable": True}


def test_after_the_model_changes_the_person_chooses_to_continue_or_start_fresh(client, monkeypatch):
    pid, head, ct = _project_with_ct(client)
    llm = _scripted(client, monkeypatch, [ASK, ASK, ASK])
    first = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ct["id"]]}, "instruction": "Check CT-201"}).json()
    edited = client.post(f"/api/projects/{pid}/edits", json={
        "base_revision": head, "operations": [{"op": "update_point", "id": ct["id"], "label": "CT-201 permeate"}]})
    assert edited.status_code == 200, edited.text
    now = client.get(f"/api/projects/{pid}/model").json()["head"]
    changes = client.get(f"/api/projects/{pid}/runs/{first['id']}/changes").json()
    assert changes["changed"] and changes["continuable"]
    assert changes["summary"] == f"The model changed since the last reply ({head} → {now}: 1 edit)."

    kept = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": now, "instruction": "Go on.", "parent_run_id": first["id"]}).json()
    turn = llm.seen[1][-1]["content"]
    assert kept["continued"] and turn.startswith("Since your last reply:")
    assert "The model changed since: " in turn and "now " in turn and "CT-201 permeate" in turn
    assert "check the current model with tools" in turn

    fresh = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": now, "instruction": "Go on.", "parent_run_id": first["id"], "fresh": True}).json()
    assert not fresh["continued"] and len(llm.seen[2]) == 1  # a new conversation
    assert "EARLIER CONVERSATION" in llm.seen[2][0]["content"] and "Since your last reply" not in llm.seen[2][0]["content"]


def test_applying_the_runs_own_proposal_is_not_a_change_it_does_not_know(client, monkeypatch):
    pid, head, ct = _project_with_ct(client)
    rename = {"action": "propose", "explanation": "Renamed.",
              "operations": [{"op": "update_point", "id": ct["id"], "label": "CT-201 permeate"}]}
    llm = _scripted(client, monkeypatch, [rename, ASK])
    first = client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": head, "selection": {"entity_ids": [ct["id"]]}, "instruction": "Rename CT-201"}).json()
    prop = client.get(f"/api/projects/{pid}/runs/{first['id']}").json()["outcome"]["proposal_id"]
    assert client.post(f"/api/projects/{pid}/proposals/{prop}/apply").status_code == 200
    changes = client.get(f"/api/projects/{pid}/runs/{first['id']}/changes").json()
    assert changes["changed"] is False
    now = client.get(f"/api/projects/{pid}/model").json()["head"]
    client.post(f"/api/projects/{pid}/assist", json={
        "base_revision": now, "instruction": "Thanks, now its unit.", "parent_run_id": first["id"]})
    turn = llm.seen[1][-1]["content"]
    assert f"Your proposed change {prop} was applied as {now}." in turn and "Objects that differ" not in turn
