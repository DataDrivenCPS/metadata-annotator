"""Correction-workflow mechanics with a scripted model client.

These check application behaviour (tool routing, malformed-operation repair, proposal
construction, scope reporting). They do not evaluate model quality; the real-model check
is test_agent_live below, which only runs when WORKBENCH_TEST_PROVIDER is set.
"""

import os

import pytest

from conftest import by_label
from workbench.agent.correction import run_correction
from workbench.agent.guidance import SkillGuidance
from workbench.config import load_settings
from workbench.llm import CancelToken, Cancelled, LLMResult, make_client
from workbench.schemas import SelectionScope


class ScriptedLLM:
    provider = "scripted"
    model = "scripted"
    supports_images = False

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen: list[list[dict]] = []

    def complete_json(self, system, messages, schema, **kw):
        self.seen.append([dict(m) for m in messages])
        step = self.steps.pop(0)
        data = step(messages) if callable(step) else step
        return LLMResult(data=data, raw_text="", input_tokens=10, output_tokens=5)


@pytest.fixture
def guidance():
    return SkillGuidance(load_settings().skill_dir)


def run(project, llm, guidance, sel, text, cancel=None):
    events = []
    out = run_correction(project, llm, guidance, project.head(), sel, text, None,
                         lambda st, m, d: events.append((st, m, d)), cancel or CancelToken())
    return out, events


def test_tool_call_then_repair_then_proposal(sample_project, guidance):
    p = sample_project
    ct = by_label(p.view(p.head()).points, "CT-201")
    llm = ScriptedLLM([
        {"action": "units_for", "args": {"quantity_kind": "quantitykind:ElectrolyticConductivity"}},
        {"action": "propose", "explanation": "fix unit",
         "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:Microsiemens-per-cm"}]},
        {"action": "propose", "explanation": "Changed the unit to microsiemens per centimetre.",
         "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"}]},
    ])
    out, events = run(p, llm, guidance, SelectionScope(entity_ids=[ct.id], field_ids=["unit"]), "unit should be uS/cm")
    stages = [e[0] for e in events]
    assert stages.count("tool") == 1 and "rejected" in stages and stages[-1] == "validated"
    # the tool result reached the model, and the rejection carried suggestions
    assert "MicroS-PER-CentiM" in llm.seen[1][-1]["content"]
    assert "valid terms include" in llm.seen[2][-1]["content"]
    prop = out.proposal
    assert prop is not None and prop.status == "pending"
    assert [c.entity_id for c in prop.changes] == [ct.id]
    assert prop.changes[0].fields[0].after == "Microsiemens per Centimetre"
    assert any("CT-201" in r for r in prop.validation.resolved)
    assert prop.out_of_scope_ids == []
    # nothing was applied: the head is unchanged until a person applies it
    assert p.head() == prop.base_revision


def test_questions_without_operations(sample_project, guidance):
    p = sample_project
    llm = ScriptedLLM([{"action": "propose", "explanation": "Which tank?", "operations": [],
                        "questions": ["Does L-06 go to TK-301 or somewhere else?"]}])
    out, _ = run(p, llm, guidance, SelectionScope(), "fix the concentrate line")
    assert out.proposal is None and out.questions


def test_repeated_tool_calls_are_not_rerun(sample_project, guidance):
    p = sample_project
    search = {"thought": "find it", "action": "search_terms", "args": {"query": "AHU", "kind": "equipment"}}
    llm = ScriptedLLM([search, search, {"action": "propose", "explanation": "Nothing to change.", "operations": []}])
    out, events = run(p, llm, guidance, SelectionScope(), "add an AHU")
    assert [e[0] for e in events].count("tool") == 1 and any(e[0] == "repeat" for e in events)
    first, second = llm.seen[1][-1]["content"], llm.seen[2][-1]["content"]
    assert "s223:AirHandlingUnit" in first and "more replies" in first
    assert "already called search_terms" in second


def test_vocabulary_search_matches_initials(vocab):
    assert vocab.search("AHU", ["equipment"], 3)[0].label == "Air handling unit"
    assert [t for t in vocab.search("equipment", ["equipment"], 5) if t.label == "Equipment"]


def test_repair_budget_is_bounded(sample_project, guidance):
    p = sample_project
    bad = {"action": "propose", "explanation": "x", "operations": [{"op": "update_point", "id": "pt-nope", "label": "x"}]}
    llm = ScriptedLLM([bad] * 5)
    with pytest.raises(Exception, match="could not produce valid operations"):
        run(p, llm, guidance, SelectionScope(), "rename")


def test_cancellation_stops_the_run(sample_project, guidance):
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        run(sample_project, ScriptedLLM([]), guidance, SelectionScope(), "anything", token)


@pytest.mark.llm
@pytest.mark.skipif(not os.environ.get("WORKBENCH_TEST_PROVIDER"), reason="set WORKBENCH_TEST_PROVIDER to run")
def test_agent_live_reassigns_points(sample_project, guidance):
    """Real model: 'these belong to RO-1' on two selected points yields exactly that change."""
    settings = load_settings(os.environ.get("WORKBENCH_CONFIG"))
    llm = make_client(settings.provider(os.environ["WORKBENCH_TEST_PROVIDER"]))
    p = sample_project
    view = p.view(p.head())
    ids = [by_label(view.points, t).id for t in ("FT-201", "FT-301")]
    ro = by_label(view.equipment, "RO-1")
    out, _ = run(p, llm, guidance, SelectionScope(entity_ids=ids, field_ids=["equipment"]),
                 "These flow meters are on the RO skid. They belong to RO-1.")
    assert out.proposal is not None
    changed = {c.entity_id: c for c in out.proposal.changes}
    assert set(changed) == set(ids)
    assert all(c.fields[0].after == ro.label for c in changed.values())


def _pending_rename(project, guidance):
    point = by_label(project.view(project.head()).points, "CT-201")
    selection = SelectionScope(entity_ids=[point.id])
    llm = ScriptedLLM([{"action": "propose", "explanation": "Rename it",
                        "operations": [{"op": "update_point", "id": point.id, "label": "Old suggestion"}]}])
    outcome, _ = run(project, llm, guidance, selection, "rename this point")
    return point, outcome.proposal


def _reconsider(project, guidance, prior, response):
    llm = ScriptedLLM([response])
    live = project.view(project.head()).rows()
    selection = prior.selection.model_copy(update={
        "entity_ids": [i for i in prior.selection.entity_ids if i in live],
    })
    outcome = run_correction(project, llm, guidance, project.head(), selection,
                             "Reconsider on latest revision", None, lambda *args: None,
                             CancelToken(), prior_proposal=prior, reconsider=True)
    return outcome, llm


def test_reconsider_deleted_target_dismisses_resolved_proposal(sample_project, guidance):
    from workbench.operations import OperationList

    p = sample_project
    point, prior = _pending_rename(p, guidance)
    p.edit(p.head(), OperationList.validate_python([{"op": "delete_point", "id": point.id}]))
    outcome, llm = _reconsider(p, guidance, prior, {
        "action": "propose", "explanation": "The point has been removed; no change needed.", "operations": [],
    })
    assert len(llm.seen) == 1
    assert outcome.proposal is None
    assert outcome.dismissed_proposal_id == prior.id
    assert p.proposal(prior.id).status == "dismissed"
    assert "historical context only" in llm.seen[0][0]["content"]


def test_reconsider_replaces_operations_on_current_revision(sample_project, guidance):
    from workbench.operations import OperationList

    p = sample_project
    point, prior = _pending_rename(p, guidance)
    p.edit(p.head(), OperationList.validate_python([
        {"op": "update_point", "id": point.id, "label": "Current name"},
    ]))
    latest = p.head()
    outcome, llm = _reconsider(p, guidance, prior, {
        "action": "propose", "explanation": "Only the unit still needs correcting.",
        "operations": [{"op": "update_point", "id": point.id, "unit": "unit:MicroS-PER-CentiM"}],
    })
    proposal = outcome.proposal
    assert proposal is not None and proposal.base_revision == latest
    assert proposal.parent_proposal_id == prior.id
    assert len(proposal.operations) == 1
    assert "label" not in proposal.operations[0].model_fields_set
    assert "COMPLETE replacement operations" in llm.seen[0][0]["content"]
    assert p.proposal(prior.id).status == "dismissed"
    applied = p.apply_proposal(proposal.id)
    assert p.view(applied.id).rows()[point.id].label == "Current name"


def test_reconsider_questions_keep_original_proposal(sample_project, guidance):
    p = sample_project
    _, prior = _pending_rename(p, guidance)
    outcome, _ = _reconsider(p, guidance, prior, {
        "action": "propose", "operations": [], "questions": ["What name should it have?"],
    })
    assert outcome.proposal is None and outcome.dismissed_proposal_id is None
    assert p.proposal(prior.id).status == "pending"


def test_ordinary_reply_keeps_existing_operations(sample_project, guidance):
    p = sample_project
    point, prior = _pending_rename(p, guidance)
    llm = ScriptedLLM([{
        "action": "propose", "explanation": "Also correct its unit.",
        "operations": [{"op": "update_point", "id": point.id, "unit": "unit:MicroS-PER-CentiM"}],
    }])
    outcome = run_correction(p, llm, guidance, p.head(), prior.selection, "also fix unit", None,
                             lambda *args: None, CancelToken(), prior_proposal=prior)
    assert len(outcome.proposal.operations) == 2
    applied = p.apply_proposal(outcome.proposal.id)
    assert p.view(applied.id).rows()[point.id].label == "Old suggestion"


def test_refresh_run_persists_no_change_outcome(sample_project, guidance, monkeypatch):
    from workbench.events import EventBus
    from workbench.operations import OperationList
    from workbench.runs import RunManager

    p = sample_project
    point, prior = _pending_rename(p, guidance)
    p.edit(p.head(), OperationList.validate_python([{"op": "delete_point", "id": point.id}]))
    llm = ScriptedLLM([{"action": "propose", "explanation": "Already resolved.", "operations": []}])
    monkeypatch.setattr(llm, "health", lambda: {"ok": True}, raising=False)
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: llm)
    manager = RunManager(load_settings(), guidance, EventBus())
    # Execute synchronously to exercise the run entry point and persisted result without polling.
    monkeypatch.setattr(manager.pool, "submit", lambda fn, *args: fn(*args))
    try:
        run_result = manager.start_revision(p, p.proposal(prior.id), "Reconsider", reconsider=True)
        stored = manager.get(p, run_result.id)
        assert stored.status == "succeeded", stored.error
        assert stored.input_revision == p.head()
        assert stored.selection.entity_ids == []
        assert stored.outcome["proposal_id"] is None
        assert stored.outcome["dismissed_proposal_id"] == prior.id
        assert stored.outcome["explanation"] == "Already resolved."
    finally:
        manager.pool.shutdown()


def _ro1_issue(p):
    return next(i for i in p.issues(p.head()) if i.explanation.startswith("RO-1") and i.resolution_state == "open")


def test_dismissal_only_proposal_applies_without_a_revision(sample_project, guidance):
    p = sample_project
    issue = _ro1_issue(p)
    llm = ScriptedLLM([
        {"action": "propose", "explanation": "Wrong id first.", "operations": [],
         "dismiss_issues": [{"id": "val-nope", "reason": "expected"}]},
        {"action": "propose", "explanation": "RO-1 is connected; the finding is expected.", "operations": [],
         "dismiss_issues": [{"id": issue.id, "reason": "RO splits brackish feed into permeate and brine."}]},
    ])
    out, events = run(p, llm, guidance, SelectionScope(entity_ids=issue.affected_ids), "is this issue real?")
    assert f"[{issue.id}]" in llm.seen[0][0]["content"]
    assert any(stage == "rejected" for stage, _, _ in events)
    prop = out.proposal
    assert prop is not None and not prop.operations and [d.id for d in prop.issue_dismissals] == [issue.id]
    head = p.head()
    assert p.apply_proposal(prop.id).id == head
    assert next(i for i in p.issues(head) if i.id == issue.id).resolution_state == "dismissed"


def test_reply_can_withdraw_a_dismissal(sample_project, guidance):
    p = sample_project
    issue = _ro1_issue(p)
    first = ScriptedLLM([{"action": "propose", "explanation": "Dismiss it.", "operations": [],
                          "dismiss_issues": [{"id": issue.id, "reason": "expected"}]}])
    prior, _ = run(p, first, guidance, SelectionScope(), "dismiss the RO-1 issue")
    second = ScriptedLLM([{"action": "propose", "explanation": "Keeping it open.", "operations": [],
                           "withdraw_dismissals": [issue.id]}])
    out = run_correction(p, second, guidance, p.head(), prior.proposal.selection, "actually keep it", None,
                         lambda *a: None, CancelToken(), prior_proposal=prior.proposal)
    assert "would also dismiss" in second.seen[0][0]["content"]
    assert out.proposal is not None and out.proposal.issue_dismissals == []


def test_gate_gives_the_model_one_chance_to_respond(sample_project, guidance):
    p = sample_project
    tank = {"op": "create_equipment", "label": "TK-999 Spare Tank", "type": "watr:Tank"}
    llm = ScriptedLLM([
        {"action": "propose", "explanation": "Add the spare tank.", "operations": [tank]},
        {"action": "propose", "explanation": "Add the spare tank; it is not piped yet, so the inlet "
                                             "and outlet findings are expected.", "operations": [tank]},
    ])
    out, events = run(p, llm, guidance, SelectionScope(), "add a spare tank TK-999")
    assert any(stage == "gated" for stage, _, _ in events)
    nudge = llm.seen[1][-1]["content"]
    assert "soundness gate" in nudge and "TK-999 Spare Tank · watr:Tank" in nudge
    assert out.proposal is not None and out.steps == 2
    assert out.proposal.gate["introduced"] == ["TK-999 Spare Tank · watr:Tank"]


def test_sound_changes_are_not_questioned(sample_project, guidance):
    p = sample_project
    ct = by_label(p.view(p.head()).points, "CT-201")
    llm = ScriptedLLM([{"action": "propose", "explanation": "Permeate conductivity is in uS/cm.",
                        "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"}]}])
    out, events = run(p, llm, guidance, SelectionScope(entity_ids=[ct.id]), "fix the CT-201 unit")
    assert out.steps == 1 and not any(stage == "gated" for stage, _, _ in events)
    assert out.proposal.gate["sound"] and out.proposal.gate["progress"]


def test_json_wrapped_in_a_list_is_accepted():
    from workbench.llm import LLMError
    from workbench.llm.base import parse_json_text

    assert parse_json_text('[{"action": "propose"}]') == {"action": "propose"}
    with pytest.raises(LLMError):
        parse_json_text('[{"a": 1}, {"b": 2}]')


def test_a_reply_stuck_on_blank_space_is_abandoned_and_asked_again(monkeypatch):
    # Schema-constrained decoding can emit whitespace until the token limit (seen with gemma on
    # OpenRouter: 244k blank characters). The stream is left early and the request repeated.
    import json as _json

    import httpx

    import workbench.llm.openai_compat as oc
    from workbench.config import ProviderConfig

    calls = []

    def sse(pieces):
        body = "".join(f"data: {_json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in pieces)
        return body + f"data: {_json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\ndata: [DONE]\n\n"

    def handler(request):
        calls.append(request)
        pieces = ['{"action": ', *[" " * 50] * 40, '"x"}'] if len(calls) == 1 else ['{"action": "propose"}']
        return httpx.Response(200, text=sse(pieces), headers={"content-type": "text/event-stream"})

    real = httpx.Client
    monkeypatch.setattr(oc.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    client = oc.OpenAICompatClient(ProviderConfig(name="t", kind="openai", model="m", base_url="http://x/v1"))
    result = client.complete_json("sys", [{"role": "user", "content": "hi"}], {"type": "object"})
    assert result.data == {"action": "propose"} and len(calls) == 2


def test_malformed_replies_are_retried_once_and_counted():
    from workbench.llm import LLMResult
    from workbench.llm.base import MalformedOutput, retry_malformed

    replies = [MalformedOutput("not an object", 7, 3), LLMResult(data={"ok": 1}, raw_text="", input_tokens=5, output_tokens=2)]

    def call():
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    result = retry_malformed(call)
    assert result.data == {"ok": 1} and (result.input_tokens, result.output_tokens) == (12, 5)
    always_bad = lambda: (_ for _ in ()).throw(MalformedOutput("[]"))  # noqa: E731
    with pytest.raises(MalformedOutput):
        retry_malformed(always_bad)


def test_context_fits_the_budget_by_priority(sample_project):
    from workbench.agent.correction import build_context

    p = sample_project
    full, _ = build_context(p, p.head(), SelectionScope(), "fix the issues")
    assert "not shown" not in full
    small, _ = build_context(p, p.head(), SelectionScope(), "fix the issues", budget=3000)
    # Issues outrank the model listing; whatever is left out is counted and can be looked up.
    assert "Open issues on these objects:" in small and "[val-" in small
    assert "more points not shown; look them up with find_entities)" in small
    assert len(small) < len(full) and small.rstrip().endswith("Request: fix the issues")


def test_context_budget_leaves_room_for_reply_and_tool_steps():
    from workbench.agent.correction import CHARS_PER_TOKEN, MIN_CONTEXT_CHARS, context_budget

    # 32k window: an eighth for the reply, a quarter for tool steps, then the system prompt and the rest
    assert context_budget(32768, "s" * 9000, 1000) == (32768 - 4096 - 8192) * CHARS_PER_TOKEN - 10000
    assert context_budget(262144, "", 0) > context_budget(32768, "", 0)
    assert context_budget(8192, "s" * 9000, 0, images=2) == MIN_CONTEXT_CHARS  # the selection still shows


def test_context_window_from_config_or_endpoint(monkeypatch):
    import httpx

    import workbench.llm.openai_compat as oc
    from workbench.config import ProviderConfig
    from workbench.llm import context_window

    replies = {
        # llama-server: per-slot context in /props at the server root
        "http://llama/props": {"default_generation_settings": {"n_ctx": 16384}},
        # Lemonade: the loaded model's ctx_size in /health
        "http://lemon/v1/health": {"all_models_loaded": [
            {"model_name": "other", "recipe_options": {"ctx_size": 4096}},
            {"model_name": "gemma", "recipe_options": {"ctx_size": 65536}}]},
        # OpenRouter: context_length on the model's /models entry
        "http://router/v1/models": {"data": [{"id": "a/b", "context_length": 8192},
                                             {"id": "g/gemma", "context_length": 131072}]},
    }
    asked: list[str] = []

    def get(url, **kw):
        asked.append(url)
        if url in replies:
            return httpx.Response(200, json=replies[url])
        return httpx.Response(200, text="<html>app</html>") if url.endswith("/props") else httpx.Response(404)

    monkeypatch.setattr(oc.httpx, "get", get)

    def client(base_url, model, **kw):
        return oc.OpenAICompatClient(ProviderConfig(name="t", kind="openai", model=model, base_url=base_url, **kw))

    assert context_window(client("http://llama/v1", "local")) == 16384
    assert context_window(client("http://lemon/v1", "gemma")) == 65536
    assert context_window(client("http://router/v1", "g/gemma")) == 131072
    assert context_window(client("http://router/v1", "g/gemma", context_tokens=20000)) == 20000  # config wins
    asked.clear()
    assert context_window(client("http://router/v1", "g/gemma")) == 131072 and not asked  # remembered
    assert context_window(client("http://silent/v1", "m")) == 32768  # nothing reported: the default
    assert context_window(client("http://silent/v1", "m")) == 32768 and asked.count("http://silent/v1/models") == 2


def test_reply_tokens_shrink_with_small_windows():
    from workbench.llm import reply_tokens

    assert reply_tokens(1_000_000) == 8000 and reply_tokens(32768) == 4096 and reply_tokens(8192) == 2048


class SmallWindowLLM(ScriptedLLM):
    def __init__(self, steps, window):
        super().__init__(steps)
        self.window, self.kwargs = window, []

    def context_tokens(self):
        return self.window

    def complete_json(self, system, messages, schema, **kw):
        self.kwargs.append(kw)
        return super().complete_json(system, messages, schema, **kw)


def test_small_window_asks_for_a_smaller_reply_and_shortens_old_results(sample_project, guidance):
    p = sample_project
    tank = by_label(p.view(p.head()).equipment, "TK-101")
    searches = [{"action": "read_guidance", "args": {"topic": t}} for t in ("points", "connections", "watr_layers")]
    llm = SmallWindowLLM([*searches, {"action": "propose", "explanation": "nothing to change", "operations": []}],
                         window=7000)
    out, events = run(p, llm, guidance, SelectionScope(entity_ids=[tank.id]), "check this tank")
    assert all(kw.get("max_tokens") == 2048 for kw in llm.kwargs)
    # The first guidance result no longer fits beside the later ones; the latest are kept whole.
    last = llm.seen[-1]
    assert "Result of read_guidance(topic='points'), shortened to save room" in last[2]["content"]
    assert last[-1]["content"].startswith("Result of read_guidance:")
    assert any(e[0] == "shortened" for e in events)

    big = SmallWindowLLM([{"action": "propose", "explanation": "ok", "operations": []}], window=1_000_000)
    run(p, big, guidance, SelectionScope(entity_ids=[tank.id]), "check this tank")
    assert "max_tokens" not in big.kwargs[0]  # a large window keeps the adapter's default


def test_long_history_keeps_the_latest_turns_clipped(sample_project, guidance):
    p = sample_project
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"turn {i} " + "x" * 2000} for i in range(12)]
    llm = ScriptedLLM([{"action": "propose", "explanation": "ok", "operations": []}])
    run_correction(p, llm, guidance, p.head(), SelectionScope(), "and now?", None, lambda *a: None, CancelToken(),
                   history=history)
    context = llm.seen[0][0]["content"]
    assert "(4 earlier turns not shown)" in context and "turn 3 " not in context and "turn 11 " in context
    assert "x" * 801 not in context


def test_step_schema_offers_only_what_the_family_accepts(registry):
    from workbench.agent.correction import step_schema

    def ops(schema):
        return {v["properties"]["op"]["enum"][0]: set(v["properties"])
                for v in schema["properties"]["operations"]["items"]["anyOf"]}

    brick = step_schema(registry.get("brick"), token_updates=False, evidence=False)
    assert "token_updates" not in brick["properties"]
    b = ops(brick)
    assert "create_connection_point" not in b and "process" not in b["create_equipment"]
    assert "point_type" in b["create_point"] and "quantity_kind" not in b["create_point"]
    assert "medium" not in b["create_connection"] and "evidence" not in b["create_point"]
    # the choices offer the same operations
    assert brick["properties"]["choices"]["items"]["properties"]["options"]["items"]["properties"][
        "operations"]["items"] == brick["properties"]["operations"]["items"]

    s223 = ops(step_schema(registry.get("223p"), evidence=True))
    assert "create_connection_point" in s223 and "process" not in s223["create_equipment"]
    assert "evidence" in s223["create_point"] and "point_type" not in s223["create_point"]
    watr = step_schema(registry.get("watr"))
    assert "process" in ops(watr)["create_equipment"] and "token_updates" in watr["properties"]
