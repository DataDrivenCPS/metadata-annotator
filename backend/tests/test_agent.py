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
