"""Auto-fix: issues grouped by cause, each group's fix classified by checks, obvious ones applied."""

from conftest import by_label
from test_agent import ScriptedLLM

from workbench.agent.guidance import SkillGuidance
from workbench.autofix import group_issues, run_autofix
from workbench.config import load_settings
from workbench.llm import CancelToken


def guidance():
    return SkillGuidance(load_settings().skill_dir)


def unit_issue(p):
    ct = by_label(p.view(p.head()).points, "CT-201")
    return ct, next(i for i in p.issues(p.head()) if ct.id in i.affected_ids and i.severity == "violation")


def autofix(p, steps, issue_ids):
    events = []
    out = run_autofix(p, ScriptedLLM(steps), guidance(), issue_ids, "run-test",
                      lambda s, m, d: events.append((s, m)), CancelToken())
    return out, events


def test_issues_group_by_cause(sample_project):
    groups = group_issues(sample_project.issues(sample_project.head()))
    # the sample's nine violations have three causes; a group shares severity, shape and path
    assert sorted(len(g) for g in groups) == [1, 4, 4]
    for g in groups:
        shapes = {tuple(sorted(str(d.get("shape")) for d in i.details["findings"])) for i in g}
        assert len(shapes) == 1


def test_a_grounded_fix_is_applied_automatically_and_unlocked(sample_project):
    p = sample_project
    ct, issue = unit_issue(p)
    base = p.head()
    # CT-101 already uses µS/cm for the same quantity, so choosing it is grounded in the model.
    out, events = autofix(p, [{"action": "propose", "explanation": "CT-201 measures conductivity like CT-101.",
                               "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"}]}],
                          [issue.id])
    (group,) = out.groups
    assert group["status"] == "fixed", group
    rev = p.revision(p.head())
    assert p.head() != base and rev.kind == "autofix" and rev.summary.startswith("Automatic fix")
    assert out.autofix["revisions"] == [rev.id]
    assert issue.id not in {i.id for i in p.issues(p.head())}
    assert "unit" not in by_label(p.view(p.head()).points, "CT-201").locked  # nobody confirmed it
    assert ("group_done", "Group 1/1: fixed") in events


def test_an_ungrounded_choice_becomes_a_choice_between_the_terms_that_work(sample_project):
    p = sample_project
    ct, issue = unit_issue(p)
    base = p.head()
    # S/m also fixes the dimensions, but nothing in the model, the issue or the evidence says S/m:
    # the person picks between the units that work (the conductivity units), nothing is applied.
    out, _ = autofix(p, [{"action": "propose", "explanation": "Use siemens per metre.",
                          "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:S-PER-M"}]}], [issue.id])
    (group,) = out.groups
    assert group["status"] == "choice" and p.head() == base
    (choice,) = group["choices"]
    assert choice["question"] == "Which unit for CT-201?"
    labels = [o["label"] for o in choice["options"]]
    assert any("unit:S-PER-M" in label for label in labels) and any("unit:MicroS-PER-CentiM" in label for label in labels)
    assert not any("MilliGM" in label for label in labels)  # the current unit does not fix it: not offered
    # choosing is applying that option: the person confirmed it, so the unit is locked
    option = next(o for o in choice["options"] if "MicroS" in o["label"])
    p.apply_proposal(option["proposal_id"])
    row = by_label(p.view(p.head()).points, "CT-201")
    assert row.unit.iri.endswith("MicroS-PER-CentiM") and "unit" in row.locked
    assert issue.id not in {i.id for i in p.issues(p.head())}


def test_the_assistants_choices_are_checked_before_they_are_offered(sample_project):
    p = sample_project
    ct, issue = unit_issue(p)
    out, _ = autofix(p, [{"action": "propose", "explanation": "Which unit does CT-201 report in?", "operations": [],
                          "choices": [{"question": "What unit does CT-201 report?", "options": [
                              {"label": "µS/cm", "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"}]},
                              {"label": "mg/L (as now)", "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:MilliGM-PER-L"}]},
                              {"label": "psi", "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:PSI"}]},
                          ]}]}], [issue.id])
    (group,) = out.groups
    assert group["status"] == "choice"
    # "as now" changes nothing and psi is the wrong dimension: only µS/cm is offered
    assert [o["label"] for o in group["choices"][0]["options"]] == ["µS/cm"]


def test_a_fix_with_a_change_it_does_not_need_is_left_for_review(sample_project):
    # The unit fixes the issue; the medium change rides along ("conductivity usually monitors brine").
    p = sample_project
    ct, issue = unit_issue(p)
    base = p.head()
    out, _ = autofix(p, [{"action": "propose", "explanation": "Fix the unit; it probably monitors brine.",
                          "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM",
                                          "medium": "watr:Water-Brine"}]}], [issue.id])
    (group,) = out.groups
    assert group["status"] == "review" and p.head() == base
    assert any("does not need: medium of CT-201" in r for r in group["reasons"]), group["reasons"]


def test_questions_need_input_and_failures_do_not_stop_the_rest(sample_project):
    p = sample_project
    _, issue = unit_issue(p)
    out, _ = autofix(p, [{"action": "propose", "explanation": "Which unit does CT-201 report in?",
                          "operations": [], "questions": ["Does CT-201 report µS/cm or S/m?"]}], [issue.id])
    assert out.groups[0]["status"] == "input" and out.groups[0]["questions"]

    def boom(_messages):
        raise RuntimeError("model went away")

    groups = group_issues(p.issues(p.head()))
    ids = [i.id for g in groups[:2] for i in g]
    out, _ = autofix(p, [boom, {"action": "propose", "explanation": "Expected for now.", "operations": [],
                                "questions": ["Is this expected?"]}], ids)
    assert [g["status"] for g in out.groups] == ["failed", "input"]


def test_cancellation_during_verification_prevents_automatic_publish(sample_project, monkeypatch):
    import pytest
    import workbench.autofix as module
    from workbench.llm import Cancelled

    p = sample_project
    ct, issue = unit_issue(p)
    base = p.head()
    token = CancelToken()
    verify = module.verify

    def cancelled_verification(*args):
        reasons = verify(*args)
        assert not reasons
        token.cancel()
        return reasons

    monkeypatch.setattr(module, "verify", cancelled_verification)
    llm = ScriptedLLM([{"action": "propose", "operations": [
        {"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"},
    ]}])
    with pytest.raises(Cancelled):
        run_autofix(p, llm, guidance(), [issue.id], "run-cancel", lambda *_: None, token)
    assert p.head() == base
