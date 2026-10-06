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


def test_an_ungrounded_choice_is_left_for_review(sample_project):
    p = sample_project
    ct, issue = unit_issue(p)
    base = p.head()
    # S/m also fixes the dimensions, but nothing in the model, the issue or the evidence says S/m.
    out, _ = autofix(p, [{"action": "propose", "explanation": "Use siemens per metre.",
                          "operations": [{"op": "update_point", "id": ct.id, "unit": "unit:S-PER-M"}]}], [issue.id])
    (group,) = out.groups
    assert group["status"] == "review" and p.head() == base
    assert any("unit:S-PER-M" in r for r in group["reasons"]), group["reasons"]
    assert p.proposal(group["proposal_id"]).status == "pending"


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
