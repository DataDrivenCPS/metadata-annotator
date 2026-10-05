"""Spaces (rooms, floors, buildings) as model objects, and equipment located in them."""

import pytest
from pydantic import ValidationError
from rdflib import URIRef

from conftest import by_label
from workbench.operations import OperationError, OperationList
from workbench.vocabulary import BRICK, REC, S223


def ops(*raw):
    return OperationList.validate_python(list(raw))


FAMILIES = {
    # profile: (equipment type, space types building/level/room, nesting predicate (child, parent), location predicate)
    "watr": ("s223:Pump", ("s223:PhysicalSpace",) * 3, lambda c, p: (p, S223.contains, c), S223.hasPhysicalLocation),
    "brick": ("brick:AHU", ("rec:Building", "rec:Level", "rec:UtilitiesRoom"),
              lambda c, p: (c, REC.isPartOf, p), BRICK.hasLocation),
}


@pytest.fixture(params=sorted(FAMILIES))
def site(request, workspace):
    profile = request.param
    eq_type, (building, level, room), _, _ = FAMILIES[profile]
    p = workspace.create(f"Site {profile}", profile)
    p.edit(p.head(), ops(
        {"op": "create_space", "id": "new:b", "label": "Building A", "type": building},
        {"op": "create_space", "id": "new:l", "label": "Level 1", "type": level, "part_of": "new:b"},
        {"op": "create_space", "id": "new:r", "label": "Mech 101", "type": room, "part_of": "new:l"},
        {"op": "create_equipment", "id": "new:e", "label": "Unit 1", "type": eq_type, "location": "new:r"},
    ))
    return profile, p


def test_spaces_nest_and_hold_equipment(site):
    profile, p = site
    _, _, nests, located = FAMILIES[profile]
    v = p.view(p.head())
    building, level, room = (by_label(v.spaces, n) for n in ("Building A", "Level 1", "Mech 101"))
    unit = by_label(v.equipment, "Unit 1")
    assert level.part_of.id == building.id and room.part_of.id == level.id and building.part_of is None
    assert unit.location.id == room.id and room.equipment_count == 1
    g = p.graph(p.head())
    iri = lambda row: URIRef(row.iri)  # noqa: E731
    assert nests(iri(room), iri(level)) in g.model and nests(iri(level), iri(building)) in g.model
    assert (iri(unit), located, iri(room)) in g.model
    if profile == "brick":
        assert room.type.iri == str(REC.UtilitiesRoom)


def test_moving_deleting_and_loops(site):
    profile, p = site
    v = p.view(p.head())
    building, level, room = (by_label(v.spaces, n) for n in ("Building A", "Level 1", "Mech 101"))
    unit = by_label(v.equipment, "Unit 1")
    with pytest.raises(OperationError, match="inside itself"):
        p.build_candidate(p.head(), ops({"op": "update_space", "id": building.id, "part_of": room.id}))
    rev, cand = p.edit(p.head(), ops({"op": "delete_space", "id": room.id}))
    after = p.view(rev.id)
    assert by_label(after.equipment, "Unit 1").location is None
    assert {c.entity_id for c in cand.changes} >= {room.id, unit.id}
    rev2, _ = p.edit(rev.id, ops({"op": "update_equipment", "id": unit.id, "location": level.id}))
    assert by_label(p.view(rev2.id).equipment, "Unit 1").location.id == level.id


def test_space_types_must_be_spaces(site):
    profile, p = site
    eq_type = FAMILIES[profile][0]
    with pytest.raises(OperationError, match="expected a location"):
        p.build_candidate(p.head(), ops({"op": "create_space", "label": "Oops", "type": eq_type}))


def test_brick_points_take_no_location_and_old_classes_point_to_rec(workspace):
    p = workspace.create("Brick", "brick")
    p.edit(p.head(), ops({"op": "create_space", "id": "new:r", "label": "Room", "type": "rec:Room"},
                         {"op": "create_point", "label": "T", "point_type": "brick:Zone_Air_Temperature_Sensor"}))
    v = p.view(p.head())
    with pytest.raises(ValidationError, match="location"):  # points have no location field at all
        ops({"op": "update_point", "id": v.points[0].id, "location": v.spaces[0].id})
    cand = p.build_candidate(p.head(), ops({"op": "create_space", "label": "Old", "type": "brick:Room"}))
    assert any("use Room (rec:Room) instead" in n for n in cand.result.notes)


def test_located_brick_equipment_adds_no_violations(workspace):
    p = workspace.create("Brick", "brick")
    base = p.head()
    before = {i.explanation for i in p.issues(base) if i.severity == "violation"}
    rev, _ = p.edit(base, ops(
        {"op": "create_space", "id": "new:b", "label": "HQ", "type": "rec:Building"},
        {"op": "create_space", "id": "new:r", "label": "Plant room", "type": "rec:UtilitiesRoom", "part_of": "new:b"},
        {"op": "create_equipment", "label": "AHU-1", "type": "brick:AHU", "location": "new:r"},
    ))
    after = {i.explanation for i in p.issues(rev.id) if i.severity == "violation"}
    assert after <= before, after - before
