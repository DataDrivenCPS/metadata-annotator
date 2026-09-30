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
