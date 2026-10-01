"""Mutation service: apply/undo consistency, stale proposals, save/reopen, exports."""

import pytest
from rdflib import Graph
from rdflib.compare import isomorphic

from conftest import by_label
from workbench.events import EventBus
from workbench.operations import OperationError, OperationList
from workbench.project import StaleRevision, Workspace
from workbench.schemas import SelectionScope


def ops(*raw):
    return OperationList.validate_python(list(raw))


def test_import_projects_sample(sample_project):
    view = sample_project.view(sample_project.head())
    assert len(view.equipment) == 7
    assert len(view.points) == 14
    assert len(view.connections) == 6
    ft = by_label(view.points, "FT-201")
    assert ft.equipment.label.startswith("P-201")
    assert ft.unit.label == "Us Gallon per Minute"
    assert ft.sensor_type.label == "Flow Sensor"
    # sensors are not equipment rows
    assert not any("sensor" in e.label.lower() for e in view.equipment)
    issues = sample_project.issues(sample_project.head())
    assert any("TK-301" in i.explanation and "inlet" in i.explanation for i in issues)


def test_edit_then_undo_restores_graph_exactly(sample_project):
    p = sample_project
    base = p.head()
    before = p.graph(base).model
    view = p.view(base)
    ro = by_label(view.equipment, "RO-1")
    ft = by_label(view.points, "FT-201")
    rev, cand = p.edit(base, ops({"op": "update_point", "id": ft.id, "equipment": ro.id}))
    assert p.head() == rev.id
    after_view = p.view(rev.id)
    assert by_label(after_view.points, "FT-201").equipment.id == ro.id
    assert "equipment" in by_label(after_view.points, "FT-201").locked
    # only the point (and the two equipments' counts) changed; the point is the named change
    assert [c.entity_id for c in cand.changes if c.entity_kind == "point"] == [ft.id]

    assert p.undo() == base
    assert isomorphic(p.graph(p.head()).model, before)
    assert p.redo() == rev.id
    assert by_label(p.view(p.head()).points, "FT-201").equipment.id == ro.id


def test_new_edit_clears_redo(sample_project):
    p = sample_project
    base = p.head()
    tt = by_label(p.view(base).points, "TT-101")
    rev1, _ = p.edit(base, ops({"op": "update_point", "id": tt.id, "label": "TT-101A"}))
    p.undo()
    p.edit(base, ops({"op": "update_point", "id": tt.id, "label": "TT-101B"}))
    assert p.info()["can_redo"] is False
    with pytest.raises(ValueError):
        p.redo()


def test_stale_edit_rejected(sample_project):
    p = sample_project
    base = p.head()
    tt = by_label(p.view(base).points, "TT-101")
    p.edit(base, ops({"op": "update_point", "id": tt.id, "label": "TT-1"}))
    with pytest.raises(StaleRevision):
        p.edit(base, ops({"op": "update_point", "id": tt.id, "label": "TT-2"}))


def _proposal(p, raw, selection_ids=()):
    base = p.head()
    before_issues = p.issues(base)
    cand = p.build_candidate(base, ops(*raw), SelectionScope(entity_ids=list(selection_ids)))
    rev = p.revision(base)
    return p.save_proposal(cand, SelectionScope(entity_ids=list(selection_ids)), "test", "because",
                           [], [], None, rev.validation, before_issues)


def test_stale_proposal_can_be_replayed_on_latest_revision(sample_project):
    p = sample_project
    view = p.view(p.head())
    ct = by_label(view.points, "CT-201")
    prop = _proposal(p, [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"}], [ct.id])
    # someone edits first
    tt = by_label(view.points, "TT-101")
    concurrent, _ = p.edit(p.head(), ops({"op": "update_point", "id": tt.id, "label": "TT-9"}))
    assert p.proposal(prop.id).status == "stale"
    applied = p.apply_proposal(prop.id)
    assert applied.parent_id == concurrent.id
    assert p.proposal(prop.id).status == "applied"
    assert p.view(applied.id).rows()[ct.id].unit.label == "Microsiemens per Centimetre"


def test_stale_proposal_replays_its_values_over_concurrent_edit(sample_project):
    p = sample_project
    ct = by_label(p.view(p.head()).points, "CT-201")
    prop = _proposal(p, [{"op": "update_point", "id": ct.id, "label": "Proposed label"}], [ct.id])
    p.edit(p.head(), ops({"op": "update_point", "id": ct.id, "label": "Concurrent label"}))
    applied = p.apply_proposal(prop.id)
    assert p.view(applied.id).rows()[ct.id].label == "Proposed label"


def test_applied_graph_matches_displayed_proposal(sample_project):
    p = sample_project
    view = p.view(p.head())
    ct = by_label(view.points, "CT-201")
    base_graph = p.graph(p.head()).model
    prop = _proposal(p, [{"op": "update_point", "id": ct.id, "unit": "unit:MicroS-PER-CentiM"}], [ct.id])
    assert prop.changes and prop.changes[0].fields[0].field == "unit"
    # dimensional-inconsistency issue on CT-201 goes away
    assert any("CT-201" in r for r in prop.validation.resolved)
    rev = p.apply_proposal(prop.id)
    after = p.graph(rev.id).model
    added = {f"{s.n3()} {pp.n3()} {o.n3()} ." for s, pp, o in after} - {f"{s.n3()} {pp.n3()} {o.n3()} ." for s, pp, o in base_graph}
    removed = {f"{s.n3()} {pp.n3()} {o.n3()} ." for s, pp, o in base_graph} - {f"{s.n3()} {pp.n3()} {o.n3()} ." for s, pp, o in after}
    assert sorted(added) == prop.diff.added
    assert sorted(removed) == prop.diff.removed
    assert p.proposal(prop.id).status == "applied"


def test_out_of_scope_changes_are_visible(sample_project):
    p = sample_project
    view = p.view(p.head())
    ft = by_label(view.points, "FT-201")
    ro = by_label(view.equipment, "RO-1")
    prop = _proposal(p, [
        {"op": "update_point", "id": ft.id, "equipment": ro.id},
        {"op": "update_equipment", "id": ro.id, "label": "RO-1 Skid"},
    ], [ft.id])
    assert ro.id in prop.out_of_scope_ids
    assert ft.id not in prop.out_of_scope_ids


def test_malformed_operations_rejected(sample_project):
    p = sample_project
    with pytest.raises(OperationError) as e:
        p.build_candidate(p.head(), ops(
            {"op": "update_point", "id": "pt-nope", "unit": "unit:PSI"},
            {"op": "create_equipment", "label": "X", "type": "unit:PSI"},
        ))
    assert len(e.value.problems) == 2


def test_create_and_connect_with_placeholders(sample_project):
    p = sample_project
    view = p.view(p.head())
    tk = by_label(view.equipment, "TK-201")
    rev, cand = p.edit(p.head(), ops(
        {"op": "create_equipment", "id": "new:uv", "label": "UV-1", "type": "watr:UltravioletLightUnit",
         "process": "watr:Process-UVDisinfection"},
        {"op": "create_connection", "from_equipment": tk.id, "to_equipment": "new:uv", "medium": "watr:Water-Freshwater"},
    ))
    v2 = p.view(rev.id)
    uv = by_label(v2.equipment, "UV-1")
    assert uv.id.startswith("eq-")
    assert any(c.from_equipment.id == tk.id and c.to_equipment.id == uv.id for c in v2.connections)


def test_delete_equipment_unassigns_points(sample_project):
    p = sample_project
    view = p.view(p.head())
    p201 = by_label(view.equipment, "P-201")
    rev, cand = p.edit(p.head(), ops({"op": "delete_equipment", "id": p201.id}))
    v2 = p.view(rev.id)
    assert not any(e.id == p201.id for e in v2.equipment)
    assert by_label(v2.points, "PT-201").equipment is None
    assert not any((c.from_equipment and c.from_equipment.id == p201.id) or
                   (c.to_equipment and c.to_equipment.id == p201.id) for c in v2.connections)
    # the orphaned points' changes appear in the change list even though not named
    assert {c.label for c in cand.changes if c.entity_kind == "point"} >= {"PT-201", "FT-201"}


def test_save_reopen_fidelity(tmp_path, registry, sample_project):
    p = sample_project
    view = p.view(p.head())
    ft = by_label(view.points, "FT-201")
    ro = by_label(view.equipment, "RO-1")
    rev, _ = p.edit(p.head(), ops({"op": "update_point", "id": ft.id, "equipment": ro.id}))
    p.save_layout({ro.id: [10.0, 20.0]})
    graph_before = p.graph(rev.id).model

    ws2 = Workspace(p.root.parent, registry, EventBus())  # a fresh process opening the directory
    p2 = ws2.get(p.id)
    assert p2.head() == rev.id
    assert isomorphic(p2.graph(rev.id).model, graph_before)
    reopened = by_label(p2.view(rev.id).points, "FT-201")
    assert reopened.id == ft.id and reopened.equipment.id == ro.id
    assert "equipment" in reopened.locked
    assert p2.layout()[ro.id] == [10.0, 20.0]
    assert [r.id for r in p2.revisions()] == [r.id for r in p.revisions()]
    assert any(c["entity_id"] == ft.id and c["field"] == "equipment" for c in p2.corrections())


def test_export_matches_revision(sample_project):
    p = sample_project
    rid = p.head()
    exported = Graph().parse(data=p.export_turtle(rid), format="turtle")
    assert isomorphic(exported, p.graph(rid).model)
    csv_text = p.export_points_csv(rid)
    assert csv_text.count("\n") == 1 + len(p.view(rid).points)
    assert "FT-201" in csv_text


def test_annotation_only_proposal_persists_field_lock(sample_project, registry):
    p = sample_project
    base = p.head()
    point = by_label(p.view(base).points, "CT-201")
    assert "label" not in point.locked
    prop = _proposal(p, [{"op": "update_point", "id": point.id, "label": point.label}], [point.id])
    assert not prop.diff.added and not prop.diff.removed
    applied = p.apply_proposal(prop.id)
    assert applied.id != base
    reopened = Workspace(p.root.parent, registry, EventBus()).get(p.id)
    assert "label" in reopened.view(applied.id).rows()[point.id].locked
    assert reopened.proposal(prop.id).status == "applied"
    # Once both the value and annotation are present, applying again is a true no-op.
    repeat = _proposal(reopened, [{"op": "update_point", "id": point.id, "label": point.label}], [point.id])
    assert reopened.apply_proposal(repeat.id).id == applied.id
