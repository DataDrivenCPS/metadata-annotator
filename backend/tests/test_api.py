"""API smoke tests: the HTTP layer maps service errors correctly and round-trips edits."""

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.api import create_app
from workbench.config import load_settings

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "ro-train"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    settings = load_settings()
    settings.cache_dir.mkdir(parents=True)
    # reuse the shared vocabulary cache so the test doesn't rebuild the closure
    real_cache = Path(__file__).resolve().parents[2] / "workbench-data" / "cache"
    for f in real_cache.glob("*"):
        if f.is_file():  # closure/catalog caches; the OntoEnv store isn't needed once they exist
            shutil.copy(f, settings.cache_dir / f.name)
    app = create_app(settings)
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
