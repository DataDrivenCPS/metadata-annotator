import json

import pytest

from workbench.questions import readable_questions
from workbench.schemas import AgentRun, ChangeProposal


QUESTION = {
    "question": "What equipment types are these?",
    "options": [
        {"label": "Coagulation Flocculation (Tank?)", "operations": []},
        {"label": "Thickening Press (Filter Press?)", "operations": [{"op": "delete_equipment", "id": "eq-1"}]},
    ],
    "type": "choice",
}
TEXT = "What equipment types are these?\n• Coagulation Flocculation (Tank?)\n• Thickening Press (Filter Press?)"


@pytest.mark.parametrize("value", [QUESTION, repr(QUESTION), json.dumps(QUESTION)])
def test_structured_and_saved_questions_show_only_question_and_labels(value):
    assert readable_questions([value]) == [TEXT]


def test_plain_questions_and_curly_prose_are_preserved():
    assert readable_questions(["What type?", "{some prose}", None, {}]) == ["What type?", "{some prose}"]


def test_loading_saved_run_repairs_legacy_question_without_mutating_body():
    outcome = {"questions": [repr(QUESTION)], "explanation": "Please clarify", "input_tokens": 10}
    run = AgentRun(id="run", kind="correction", input_revision="rev", provider="test", model="test",
                   skill_version="", outcome=outcome)
    assert run.outcome == {**outcome, "questions": [TEXT]}
    assert outcome["questions"] == [repr(QUESTION)]


def test_loading_saved_proposal_repairs_legacy_question():
    proposal = ChangeProposal(
        id="proposal", base_revision="rev", created_at="now", selection={}, instruction="",
        operations=[], affected_ids=[], out_of_scope_ids=[], changes=[],
        diff={"added": [], "removed": []}, questions=[repr(QUESTION)],
    )
    assert proposal.questions == [TEXT]
