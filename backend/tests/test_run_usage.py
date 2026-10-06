from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workbench.events import EventBus
from workbench.llm import CancelToken, LLMError
from workbench.llm.base import LLMResult, MalformedOutput, retry_malformed
from workbench.runs import RunManager
from workbench.schemas import AgentRun


@pytest.fixture
def manager():
    manager = RunManager(None, None, EventBus())
    manager.save = Mock()
    yield manager
    manager.pool.shutdown()


def make_run(kind="autofix"):
    return AgentRun(id="run", kind=kind, input_revision="rev", provider="test", model="test", skill_version="")


def test_parallel_calls_publish_usage_and_success_does_not_double_count(manager, monkeypatch):
    client = SimpleNamespace(complete_json=lambda *a, **kw: LLMResult({}, "", 10, 2), context_tokens=lambda: 8192)
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: client)
    run = make_run()
    tracked = manager.tracked_client(None, None, run)
    assert tracked.context_tokens() == 8192

    def work(progress):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: tracked.complete_json("", [], {}), range(10)))
        assert run.outcome == {"input_tokens": 100, "output_tokens": 20}
        return SimpleNamespace(input_tokens=100, output_tokens=20)

    manager._execute(None, run, CancelToken(), work)
    assert run.status == "succeeded"
    assert run.outcome["input_tokens"] == 100
    assert run.outcome["output_tokens"] == 20


def test_failed_run_keeps_successful_calls_and_failed_reply_usage(manager, monkeypatch):
    calls = Mock(side_effect=[LLMResult({}, "", 10, 2), LLMError("truncated", 20, 5)])
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(complete_json=calls))
    run = make_run("correction")
    tracked = manager.tracked_client(None, None, run)

    def work(progress):
        tracked.complete_json("", [], {})
        tracked.complete_json("", [], {})

    manager._execute(None, run, CancelToken(), work)
    assert run.status == "failed"
    assert run.outcome == {"input_tokens": 30, "output_tokens": 7}


def test_usage_includes_history_beyond_the_chat_limit(manager):
    bodies = [dict(id=f"run-{i}", outcome={"input_tokens": i, "output_tokens": 2}) for i in range(40)]
    project = SimpleNamespace(store=SimpleNamespace(list_bodies=lambda table: bodies))
    usage = manager.usage(project)
    assert len(usage) == 40
    assert sum(run["input_tokens"] for run in usage.values()) == sum(range(40))


def test_exhausted_retries_count_every_reply():
    call = Mock(side_effect=[MalformedOutput("invalid", 10, 2), MalformedOutput("invalid", 20, 3)])
    with pytest.raises(MalformedOutput) as caught:
        retry_malformed(call)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (30, 5)


def test_usage_endpoint_returns_all_recorded_runs(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from workbench.api import create_app
    from workbench.config import load_settings

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    app = create_app(load_settings())
    project = SimpleNamespace(_recovered=True, store=SimpleNamespace(list_bodies=lambda table: [
        {"id": "old", "outcome": {"input_tokens": 123, "output_tokens": 45}},
        {"id": "queued", "outcome": {}},
    ]))
    monkeypatch.setattr(app.state.workspace, "get", lambda pid: project)
    try:
        response = TestClient(app).get("/api/projects/test/usage")
        assert response.status_code == 200
        assert response.json() == {
            "old": {"input_tokens": 123, "output_tokens": 45},
            "queued": {"input_tokens": 0, "output_tokens": 0},
        }
    finally:
        app.state.runs.pool.shutdown()
