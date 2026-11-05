"""Spaces (rooms, floors, buildings) as model objects, and equipment located in them."""

import pytest
from pydantic import ValidationError
from rdflib import URIRef

from conftest import by_label
from workbench.operations import OperationError, OperationList
from workbench.projection import project as project_view
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


def test_rejections_name_new_entities_as_written_and_say_when_one_is_the_wrong_kind(workspace):
    """A build failed three times on 'contained_in refers to unknown equipment sp-…': the id was
    minted for the model's own placeholder (new on every attempt) and the entity did exist, as a space."""
    p = workspace.create("Sections", "watr")
    with pytest.raises(OperationError) as caught:
        p.build_candidate(p.head(), ops(
            {"op": "create_space", "id": "new:pretreatment", "label": "Pretreatment", "type": "s223:PhysicalSpace"},
            {"op": "create_equipment", "label": "Pump", "type": "s223:Pump", "contained_in": "new:pretreatment"},
            {"op": "update_equipment", "id": "new:pretreatment", "label": "Pretreatment area"},
            {"op": "create_equipment", "label": "Filter", "type": "s223:Pump", "contained_in": "new:missing"},
        ))
    assert caught.value.problems == [
        "operation 2 (create_equipment): contained_in refers to equipment, but 'new:pretreatment' "
        "(created by operation 1) is a space, not an equipment; to refer to a space, use location",
        "operation 3 (update_equipment): 'new:pretreatment' (created by operation 1) is a space, "
        "not an equipment; use update_space",
        "operation 4 (create_equipment): contained_in refers to 'new:missing', but no operation creates "
        "'new:missing'; give the create_equipment for it \"id\": \"new:missing\", before this operation, "
        "or leave contained_in out",
    ]


def test_what_a_name_names_exactly_is_read_and_the_rest_says_what_to_do(workspace):
    """Models (gemma-3-4b, qwen3-vl-8b, and larger ones) wrote labels where ids belong, bare or
    wrongly prefixed term names, and references before the operation creating their target. Where
    the name names exactly one thing, that is what it means; otherwise the problem says what to do."""
    p = workspace.create("Labels", "watr")
    p.edit(p.head(), ops({"op": "create_equipment", "id": "new:p", "label": "P-101 Feed Pump", "type": "s223:Pump"},
                         {"op": "create_equipment", "label": "Tank A", "type": "watr:Tank"},
                         {"op": "create_equipment", "label": "tank a", "type": "watr:Tank"}))
    pump = next(e.id for e in p.view(p.head()).equipment if e.label == "P-101 Feed Pump")
    cand = p.build_candidate(p.head(), ops(
        {"op": "create_point", "label": "AIT-101", "equipment": "Sand Filter", "point_kind": "measurement",
         "quantity_kind": "Concentration", "unit": "http://qudt.org/vocab/unit/mg/L"},
        {"op": "create_equipment", "label": "Sand Filter", "type": "http://data.ashrae.org/standard223#Tank"},
        {"op": "create_connection", "from_equipment": "p-101 feed pump", "to_equipment": "Sand Filter",
         "medium": "s223:Fluid-Water"},
    ))
    view = project_view(cand.after, p.vocab)
    sand = next(e for e in view.equipment if e.label == "Sand Filter")
    assert sand.type.iri == "https://watermetadata.org/ontology/watr#Tank"
    assert next(pt for pt in view.points if pt.label == "AIT-101").equipment.id == sand.id  # created after it, by label
    assert {(c.from_equipment.id, c.to_equipment.id) for c in view.connections} == {(pump, sand.id)}  # type: ignore[union-attr]
    assert cand.ops.readings == [  # type: ignore[attr-defined]  (in the order applied: the filter first)
        "Sand Filter: type 'http://data.ashrae.org/standard223#Tank' was read as watr:Tank (Tank)",
        "AIT-101: quantity_kind 'Concentration' was read as quantitykind:Concentration (Concentration)",
        "AIT-101: unit 'http://qudt.org/vocab/unit/mg/L' was read as unit:MilliGM-PER-L (Milligram per Litre, mg/L)",
    ]

    tanks = sorted(e.id for e in p.view(p.head()).equipment if e.label.casefold() == "tank a")
    with pytest.raises(OperationError) as caught:
        p.build_candidate(p.head(), ops(
            {"op": "create_equipment", "label": "Filter", "type": "Pump", "location": "Mechanical room 101"},
            {"op": "create_point", "label": "LT-1", "equipment": "Tank A", "point_kind": "measurement"},
            {"op": "update_equipment", "id": "eq-zzzzzz", "label": "Typo"},
        ))
    assert caught.value.problems == [
        "operation 1 (create_equipment): location refers to 'Mechanical room 101', but nothing has the id or label "
        "'Mechanical room 101'; to add a new space, create it first (create_space with \"id\": \"new:…\") and use "
        "that id, or refer to one another operation creates by the \"new:…\" id it gives it, or leave location out",
        "operation 1 (create_equipment): type 'Pump' is not a term in the loaded vocabulary",  # s223:Pump or watr:Pump
        f"operation 2 (create_point): equipment refers to 'Tank A', but 'Tank A' is the label of 2 equipment "
        f"entities ({tanks[0]!r}, {tanks[1]!r}); use the id of the one you mean",
        "operation 3 (update_equipment): no equipment with id 'eq-zzzzzz'",  # an id: nearby ids are suggested
    ]
    assert caught.value.unknown_entities == [("eq-zzzzzz", "equipment")]


def test_a_renamed_entity_goes_by_its_new_label_in_later_operations(workspace):
    """An operation that renames something changes what later operations' labels name: the new
    label names it, and the old one no longer does."""
    p = workspace.create("Renames", "watr")
    p.edit(p.head(), ops({"op": "create_equipment", "label": "Tank A", "type": "watr:Tank"}))
    tank = p.view(p.head()).equipment[0].id
    cand = p.build_candidate(p.head(), ops(
        {"op": "update_equipment", "id": tank, "label": "Feed Tank"},
        {"op": "create_point", "label": "LT-1", "equipment": "feed tank", "point_kind": "measurement"},
    ))
    assert next(pt for pt in project_view(cand.after, p.vocab).points if pt.label == "LT-1").equipment.id == tank
    with pytest.raises(OperationError) as err:
        p.build_candidate(p.head(), ops(
            {"op": "update_equipment", "id": tank, "label": "Feed Tank"},
            {"op": "create_point", "label": "LT-1", "equipment": "Tank A", "point_kind": "measurement"},
        ))
    assert err.value.failed == {1} and "'Tank A'" in err.value.problems[0]


def test_checks_after_applying_also_name_new_entities_as_written(workspace, monkeypatch):
    from workbench import operations

    p = workspace.create("Loop", "watr")
    real = operations.containment_cycles
    monkeypatch.setattr(operations, "containment_cycles",  # report ids, as other post-apply checks do
                        lambda pg, vocab, ids: [f"{i} would end up inside itself" for i in ids] if real(pg, vocab, ids) else [])
    with pytest.raises(OperationError) as caught:
        p.build_candidate(p.head(), ops(
            {"op": "create_equipment", "id": "new:a", "label": "A", "type": "s223:Pump"},
            {"op": "create_equipment", "id": "new:b", "label": "B", "type": "s223:Pump", "contained_in": "new:a"},
            {"op": "update_equipment", "id": "new:a", "contained_in": "new:b"},
        ))
    assert sorted(caught.value.problems) == ["'new:a' (created by operation 1) would end up inside itself",
                                             "'new:b' (created by operation 2) would end up inside itself"]
