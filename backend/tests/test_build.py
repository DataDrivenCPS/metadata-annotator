"""Build proposals from source records with a scripted model."""

import pytest

from test_agent import ScriptedLLM
from workbench.agent.build import Rec, apply_spec, run_build
from workbench.agent.correction import run_correction
from workbench.agent.guidance import SkillGuidance
from workbench.config import load_settings
from workbench.llm import CancelToken, LLMError
from workbench.schemas import CsvImportConfig, Observation, SourceLocation


def test_regex_mismatch_is_unparsed_even_when_token_comes_from_column():
    rec = Rec(Observation(id="obs-1", source_id="src-1", kind="point_record",
                          content={"name": "OTHER", "metadata": {"Description": "Temp"}},
                          location=SourceLocation(source_id="src-1", kind="csv_row", row=1)),
              "OTHER", {"Description": "Temp"})
    coverage, failures, error = apply_spec({"pattern": r"A\d+:.*", "equipment_from": "none",
                                             "token_from": "column:Description"}, [rec])
    assert error is None
    assert coverage == 0 and failures == ["OTHER"] and not rec.parsed


def test_brick_build_preview_apply_and_skip_modeled_records(workspace):
    p = workspace.create("AHUs", "brick")
    source = p.add_source("points.csv", b"Name,Description\nA1:Temp,Zone Air Temp\nA2:Temp,Zone Air Temp\nA1:Mystery,Unknown Signal\n")
    p.confirm_csv_mapping(source.id, CsvImportConfig(layout="row_points", delimiter=",", header_row=0,
                                                      first_data_row=1, name_column=0, metadata_columns=[1]))
    guidance = SkillGuidance(load_settings().skill_dir)
    llm = ScriptedLLM([
        {"pattern": r"(?P<equipment>A\d+):(?P<point>.+)", "equipment_from": "group:equipment",
         "token_from": "column:Description", "units_from": "none", "explanation": "AHU prefix and description"},
        {"action": "map", "mappings": [
            {"id": "T1", "point_type": "brick:Zone_Air_Temperature_Sensor"},
            {"id": "T2", "point_type": None}]},
        {"action": "map", "mappings": [{"id": "G1", "type": "brick:AHU"},
                                         {"id": "G2", "type": "brick:AHU"}]},
    ])
    initial = p.head()
    outcome = run_build(p, llm, guidance, initial, [source.id], "", None,
                        lambda *_: None, CancelToken())
    prop = outcome.proposal
    assert prop.kind == "build" and prop.status == "pending"
    assert p.head() == initial
    assert prop.build_summary["points_created"] == 3
    assert prop.build_summary["equipment_created"] == 2
    assert prop.build_summary["mapped_tokens"] == 1
    assert prop.build_summary["unmapped_records"] == 1
    assert any(i["category"] == "unresolved_extraction" for i in prop.followup_issues)

    uncertain = run_correction(
        p, ScriptedLLM([{"action": "propose", "explanation": "Need the equipment type.",
                         "operations": [], "questions": ["What is A2?"]}]),
        guidance, initial, prop.selection, "Please check A2", None,
        lambda *_: None, CancelToken(), prior_proposal=prop)
    assert uncertain.proposal is None and uncertain.questions == ["What is A2?"]
    assert p.proposal(prop.id).status == "pending"

    a2 = next(c.entity_id for c in prop.changes if c.label == "A2")
    reply = run_correction(
        p, ScriptedLLM([{"action": "propose", "explanation": "A2 is a VAV with reheat; clarified point tokens.",
                         "operations": [{"op": "update_equipment", "id": a2,
                                         "type": "brick:Variable_Air_Volume_Box_With_Reheat"}],
                         "token_updates": [{"id": "T1", "point_type": "brick:Air_Temperature_Sensor"},
                                           {"id": "T2", "point_type": "brick:Damper_Position_Command"}]}]),
        guidance, initial, prop.selection, "A2 is a VAV with reheat, not an AHU", None,
        lambda *_: None, CancelToken(), prior_proposal=prop)
    revised = reply.proposal
    assert revised is not None and revised.kind == "build"
    assert revised.parent_proposal_id == prop.id
    assert revised.conversation[-2]["text"] == "A2 is a VAV with reheat, not an AHU"
    assert revised.build_summary["mapped_tokens"] == 2
    assert revised.build_summary["unmapped_records"] == 0
    assert len([op for op in revised.operations if op.op == "update_point"]) == 3
    assert not any(i.get("details", {}).get("token") == "Unknown Signal" for i in revised.followup_issues)
    assert p.proposal(prop.id).status == "dismissed"
    assert p.head() == initial

    rev = p.apply_proposal(revised.id)
    assert rev.kind == "extraction"
    assert len(p.view(rev.id).points) == 3
    assert len(p.view(rev.id).equipment) == 2
    assert next(e for e in p.view(rev.id).equipment if e.label == "A2").type.iri.endswith("Variable_Air_Volume_Box_With_Reheat")
    assert len([pt for pt in p.view(rev.id).points if pt.point_type.iri.endswith("Air_Temperature_Sensor")]) == 2
    assert set(p.evidence_map(rev.id)) == {o.id for o in p.observations(source.id)}
    assert all(not point.locked for point in p.view(rev.id).points)
    original_ids = {o.id for o in p.observations(source.id)}
    p.confirm_csv_mapping(source.id, p.source(source.id).import_config)
    assert {o.id for o in p.observations(source.id)} == original_ids
    with pytest.raises(LLMError, match="already in the model"):
        run_build(p, ScriptedLLM([]), guidance, rev.id, [source.id], "", None,
                  lambda *_: None, CancelToken())


def test_mapping_batches_run_concurrently_up_to_the_provider_limit(workspace):
    import re
    import threading
    import time
    from types import SimpleNamespace

    from workbench.llm import LLMResult

    p = workspace.create("Many points", "brick")
    names = [f"A{e}:P{t}" for e in range(1, 5) for t in range(1, 71)]  # 70 tokens -> 3 point batches
    source = p.add_source("points.csv", ("Name\n" + "\n".join(names) + "\n").encode())
    p.confirm_csv_mapping(source.id, CsvImportConfig(layout="row_points", delimiter=",", header_row=0,
                                                      first_data_row=1, name_column=0))

    class Concurrent:
        provider = model = "scripted"
        supports_images = False
        cfg = SimpleNamespace(concurrency=3)

        def __init__(self):
            self.lock, self.active, self.peak = threading.Lock(), 0, 0

        def complete_json(self, system, messages, schema, **kw):
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            time.sleep(0.2)
            with self.lock:
                self.active -= 1
            if "pattern" in schema["properties"]:
                data = {"pattern": r"(?P<equipment>A\d+):(?P<point>.+)", "equipment_from": "group:equipment",
                        "token_from": "group:point", "units_from": "none", "explanation": "equipment:point"}
            else:
                ids = re.findall(r"^([TG]\d+) \|", messages[-1]["content"], re.M)
                field = "point_type" if ids[0].startswith("T") else "type"
                term = "brick:Zone_Air_Temperature_Sensor" if field == "point_type" else "brick:AHU"
                data = {"action": "map", "mappings": [{"id": i, field: term} for i in ids]}
            return LLMResult(data=data, raw_text="", input_tokens=10, output_tokens=5)

    llm = Concurrent()
    outcome = run_build(p, llm, SkillGuidance(load_settings().skill_dir), p.head(), [source.id], "", None,
                        lambda *_: None, CancelToken())
    summary = outcome.proposal.build_summary
    assert llm.peak == 3  # three point batches at once, never more than the limit
    assert summary["mapped_tokens"] == 70 and summary["equipment_created"] == 4
    assert outcome.steps == 1 + 3 + 1 and outcome.input_tokens == 50


def test_cancelled_build_cannot_save_a_validated_draft(workspace):
    from workbench.llm import Cancelled

    p = workspace.create("Cancelled build", "brick")
    src = p.add_source("points.csv", b"Name\nA1:Temp\n")
    p.confirm_csv_mapping(src.id, CsvImportConfig(layout="row_points", delimiter=",", header_row=0,
                                                 first_data_row=1, name_column=0))
    base = p.head()
    token = CancelToken()
    llm = ScriptedLLM([
        {"pattern": r"(?P<equipment>A\d+):(?P<point>.+)", "equipment_from": "group:equipment",
         "token_from": "group:point", "units_from": "none"},
        {"action": "map", "mappings": [{"id": "T1", "point_type": "brick:Temperature_Sensor"}]},
        {"action": "map", "mappings": [{"id": "G1", "type": "brick:AHU"}]},
    ])

    def cancel_on_validation(stage, *_):
        if stage == "validated":
            token.cancel()

    with pytest.raises(Cancelled):
        run_build(p, llm, SkillGuidance(load_settings().skill_dir), base, [src.id], "", None,
                  cancel_on_validation, token)
    assert not p.proposals() and p.head() == base
