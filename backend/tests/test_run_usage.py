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
        assert (run.outcome["input_tokens"], run.outcome["output_tokens"], len(run.outcome["calls"])) == (100, 20, 10)
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
    assert (run.outcome["input_tokens"], run.outcome["output_tokens"]) == (30, 7)
    assert run.outcome["calls"][1] == {"at": "", "input_tokens": 20, "output_tokens": 5, "error": "truncated"}


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
    monkeypatch.setattr("workbench.api.retrieval.statuses", lambda p: {
        "src-1": {"status": "ready", "description_tokens": 1500, "description_input_tokens": 1400,
                  "description_output_tokens": 100},
        "src-2": {"status": "ready"}})
    try:
        response = TestClient(app).get("/api/projects/test/usage")
        assert response.status_code == 200
        assert response.json() == {
            "old": {"input_tokens": 123, "output_tokens": 45},
            "queued": {"input_tokens": 0, "output_tokens": 0},
            "index:src-1": {"input_tokens": 1400, "output_tokens": 100},  # drawing descriptions
        }
    finally:
        app.state.runs.pool.shutdown()


def test_choice_only_response_is_visible_in_chat_and_history(manager):
    from workbench.runs import run_reply_text

    run = make_run("correction")
    choice = {"question": "Which type?", "options": [{"label": "Tank", "operations": []}]}
    manager._execute(None, run, CancelToken(), lambda progress: SimpleNamespace(choices=[choice]))
    assert run.status == "succeeded"
    assert run.outcome["questions"] == ["Which type?\n• Tank"]
    assert "Which type?\n• Tank" in run_reply_text(run)


def test_each_call_is_recorded_with_its_step_cache_reads_estimates_and_cancellation(manager, monkeypatch):
    from workbench.llm import Cancelled
    from workbench.schemas import ProgressEvent

    def complete(system, messages, schema, **kw):
        replies.pop(0)
        if not replies:
            raise Cancelled()
        return results.pop(0)

    results = [LLMResult({}, "", 5000, 300, cached_tokens=4600), LLMResult({}, "", 800, 20, estimated=True)]
    replies = [1, 2, 3]
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(complete_json=complete))
    run = make_run("correction")
    tracked = manager.tracked_client(None, None, run)

    def work(progress):
        for step in (1, 2, 3):
            run.progress.append(ProgressEvent(stage="model", message=f"Asking test (step {step})"))
            tracked.complete_json("system " * 30, [{"role": "user", "content": "x" * 300}], {})

    manager._execute(None, run, CancelToken(), work)
    assert run.status == "cancelled"
    first, second, third = run.outcome["calls"]
    assert first == {"at": "Asking test (step 1)", "input_tokens": 5000, "output_tokens": 300, "cached_tokens": 4600}
    assert second["estimated"] and run.outcome["estimated"] and run.outcome["cached_tokens"] == 4600
    assert third["cancelled"] and third["unreported_input_tokens"] > 100
    assert (run.outcome["input_tokens"], run.outcome["output_tokens"]) == (5800, 320)  # estimates noted, not counted


def test_finished_runs_keep_their_call_log(manager, monkeypatch):
    monkeypatch.setattr("workbench.runs.make_client",
                        lambda cfg: SimpleNamespace(complete_json=lambda *a, **kw: LLMResult({}, "", 10, 2, cached_tokens=6)))
    run = make_run("correction")
    tracked = manager.tracked_client(None, None, run)

    def work(progress):
        tracked.complete_json("", [], {})
        return SimpleNamespace(steps=1)

    manager._execute(None, run, CancelToken(), work)
    assert run.status == "succeeded" and run.outcome["steps"] == 1
    assert run.outcome["cached_tokens"] == 6 and len(run.outcome["calls"]) == 1
