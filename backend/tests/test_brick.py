"""Brick projects: projection, operations (feeds edges keep stable ids), validation, export."""

from pathlib import Path

import pytest
from rdflib import Graph, URIRef
from rdflib.compare import isomorphic

from conftest import by_label
from workbench.operations import OperationError, OperationList
from workbench.vocabulary import BRICK

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "hvac-mini" / "model.ttl"


def ops(*raw):
    return OperationList.validate_python(list(raw))


@pytest.fixture
def brick_project(workspace):
    p = workspace.create("HVAC", "brick")
    p.import_model(SAMPLE.read_bytes(), "model.ttl")
    return p


def test_projection(brick_project):
    p = brick_project
    assert p.info()["family"] == "brick"
    v = p.view(p.head())
    assert {e.label for e in v.equipment} == {"AHU-A1", "AHU-A1 Supply Fan", "A1.RM1105", "A1.RM1107"}
    assert len(v.points) == 8
    zat = by_label(v.points, "A1.RM1105.Zone Air Temp")
    assert zat.point_type.label == "Zone Air Temperature Sensor" and zat.point_kind == "measurement"
    assert zat.equipment.label == "A1.RM1105" and zat.unit_symbol
    assert by_label(v.points, "A1.RM1105:DMPR COMD").point_kind == "command"
    assert by_label(v.points, "A1.RM1105:CTL STPT").point_kind == "setpoint"
    assert by_label(v.equipment, "AHU-A1 Supply Fan").contained_in.label == "AHU-A1"
    assert {(c.from_equipment.label, c.to_equipment.label) for c in v.connections} == {
        ("AHU-A1", "A1.RM1105"), ("AHU-A1", "A1.RM1107")}
    assert all(c.id.startswith("cx-") for c in v.connections)


def test_correct_point_type_and_undo(brick_project):
    p = brick_project
    base = p.head()
    before = p.graph(base).model
    zat = by_label(p.view(base).points, "A1.RM1107.Zone Air Temp")
    assert zat.point_type.label == "Sensor"
    rev, cand = p.edit(base, ops({"op": "update_point", "id": zat.id,
                                  "point_type": "brick:Zone_Air_Temperature_Sensor", "unit": "unit:DEG_F"}))
    after = by_label(p.view(rev.id).points, "A1.RM1107.Zone Air Temp")
    assert after.point_type.label == "Zone Air Temperature Sensor"
    assert {"point_type", "unit"} <= set(after.locked)
    assert [c.fields for c in cand.changes if c.entity_id == zat.id][0][0].field == "point_type"
    p.undo()
    assert isomorphic(p.graph(p.head()).model, before)


def test_feeds_edges_are_stable_entities(brick_project):
    p = brick_project
    v = p.view(p.head())
    cx = next(c for c in v.connections if c.to_equipment.label == "A1.RM1107")
    rm1105 = by_label(v.equipment, "A1.RM1105")
    # re-target: RM1107's feed actually comes from RM1105 (odd, but tests the mechanics)
    rev, _ = p.edit(p.head(), ops({"op": "update_connection", "id": cx.id, "from_equipment": rm1105.id}))
    v2 = p.view(rev.id)
    moved = next(c for c in v2.connections if c.id == cx.id)  # same id after the change
    assert moved.from_equipment.label == "A1.RM1105"
    g = p.graph(rev.id).model
    assert (p.graph(rev.id).iri(rm1105.id), BRICK.feeds, p.graph(rev.id).iri(moved.to_equipment.id)) in g
    # the exported model has only the Brick triple, no workbench bookkeeping
    exported = Graph().parse(data=p.export_turtle(rev.id), format="turtle")
    assert not any("urn:workbench:ann" in str(t) for triple in exported for t in triple)
    rev2, _ = p.edit(rev.id, ops({"op": "delete_connection", "id": cx.id}))
    assert cx.id not in {c.id for c in p.view(rev2.id).connections}


def test_create_with_placeholders(brick_project):
    p = brick_project
    ahu = by_label(p.view(p.head()).equipment, "AHU-A1")
    rev, _ = p.edit(p.head(), ops(
        {"op": "create_equipment", "id": "new:vav", "label": "A1.RM1111", "type": "brick:Variable_Air_Volume_Box"},
        {"op": "create_point", "label": "A1.RM1111.Zone Air Temp", "point_type": "brick:Zone_Air_Temperature_Sensor",
         "equipment": "new:vav"},
        {"op": "create_connection", "from_equipment": ahu.id, "to_equipment": "new:vav"},
    ))
    v = p.view(rev.id)
    vav = by_label(v.equipment, "A1.RM1111")
    assert by_label(v.points, "A1.RM1111.Zone Air Temp").equipment.id == vav.id
    assert any(c.to_equipment.id == vav.id and c.from_equipment.id == ahu.id for c in v.connections)


def test_family_field_checks(brick_project):
    p = brick_project
    zat = by_label(p.view(p.head()).points, "A1.RM1105.Zone Air Temp")
    with pytest.raises(OperationError) as e:
        p.build_candidate(p.head(), ops(
            {"op": "update_point", "id": zat.id, "medium": "s223:Fluid-Air"},
            {"op": "update_point", "id": zat.id, "point_type": "brick:AHU"},
        ))
    msg = " ".join(e.value.problems)
    assert "medium" in msg and "Brick" in msg and "point_class" in msg


def test_s223_rejects_brick_fields(sample_project):
    p = sample_project
    pt = by_label(p.view(p.head()).points, "PT-101")
    with pytest.raises(OperationError):
        p.build_candidate(p.head(), ops({"op": "update_point", "id": pt.id, "point_type": "brick:Pressure_Sensor"}))


def test_validation_runs(brick_project):
    p = brick_project
    assert p.revision(p.head()).validation is not None


def test_updating_a_feed_removed_with_its_equipment_is_an_error_not_a_crash(brick_project):
    p = brick_project
    v = p.view(p.head())
    cx = v.connections[0]
    eq = next(e for e in v.equipment if e.id == cx.from_equipment.id)
    with pytest.raises(OperationError, match="no longer exists"):
        p.build_candidate(p.head(), ops({"op": "delete_equipment", "id": eq.id},
                                        {"op": "update_connection", "id": cx.id, "label": "x"}))
    # deleting it again is a no-op
    p.build_candidate(p.head(), ops({"op": "delete_equipment", "id": eq.id}, {"op": "delete_connection", "id": cx.id}))


def test_realestatecore_spaces_and_assets_are_indexed(brick_project):
    vocab = brick_project.vocab
    assert vocab.search("room", ["location"], 1)[0].iri == "https://w3id.org/rec#Room"
    assert vocab.search("chair", ["equipment"], 1)[0].iri == "https://w3id.org/rec#Chair"
    # Brick deprecates its own locations; a search that matches one finds its REC replacement.
    room = vocab.term(str(BRICK.Room))
    assert room.deprecated and room.replaced_by == "https://w3id.org/rec#Room"
    assert [t.iri for t in vocab.search("floor", ["location"], 1)] == ["https://w3id.org/rec#Level"]


def test_using_a_deprecated_class_names_its_replacement(brick_project):
    p = brick_project
    cand = p.build_candidate(p.head(), ops({"op": "create_equipment", "label": "Old", "type": "rec:Server"}))
    assert any("(rec:Server) is deprecated" in n and "(brick:Server) instead" in n for n in cand.result.notes)
