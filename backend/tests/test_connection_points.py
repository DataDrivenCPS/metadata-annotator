"""Connection points as their own objects: ids, pairing, mapsTo, connecting specific points."""

import pytest
from rdflib import URIRef

from conftest import by_label
from workbench.operations import OperationError, OperationList
from workbench.vocabulary import S223


def ops(*raw):
    return OperationList.validate_python(list(raw))


@pytest.fixture
def plant(workspace):
    """A boiler loop through a heat exchanger, and a coil inside an AHU piped straight to it."""
    p = workspace.create("Plant", "watr")
    p.edit(p.head(), ops(
        {"op": "create_equipment", "id": "new:ahu", "label": "AHU", "type": "s223:AirHandlingUnit"},
        {"op": "create_equipment", "id": "new:coil", "label": "Coil", "type": "s223:HeatingCoil", "contained_in": "new:ahu"},
        {"op": "create_equipment", "id": "new:hx", "label": "HX", "type": "s223:HydronicHeatExchanger"},
        {"op": "create_equipment", "id": "new:boiler", "label": "Boiler", "type": "s223:Boiler"},
        {"op": "create_connection", "from_equipment": "new:boiler", "to_equipment": "new:hx", "medium": "s223:Fluid-Water"},
        {"op": "create_connection", "from_equipment": "new:hx", "to_equipment": "new:boiler", "medium": "s223:Fluid-Water"},
        {"op": "create_connection", "from_equipment": "new:hx", "to_equipment": "new:coil", "medium": "s223:Fluid-Water"},
        {"op": "create_connection", "from_equipment": "new:coil", "to_equipment": "new:hx", "medium": "s223:Fluid-Water"},
    ))
    return p


def port(view, equipment, direction, far):
    """The connection point of ``equipment`` whose connection goes to/from ``far``."""
    return by_label(view.connection_points, f"{equipment} {direction} {'from' if direction == 'inlet' else 'to'} {far} (water)")


def violations(p, rid):
    return {i.explanation for i in p.issues(rid) if i.severity == "violation"}


def test_connection_points_are_projected_with_stable_ids(plant):
    v = plant.view(plant.head())
    assert len(v.connection_points) == 8
    hx_in = port(v, "HX", "inlet", "Boiler")
    assert hx_in.equipment.label == "HX" and hx_in.medium.label == "Water" and hx_in.connection
    cx = next(c for c in v.connections if c.id == hx_in.connection.id)
    assert cx.to_point.id == hx_in.id
    # Ports minted with a connection have no wb:id yet; their derived id survives other edits.
    rev, _ = plant.edit(plant.head(), ops({"op": "update_equipment", "id": hx_in.equipment.id, "label": "HX-1"}))
    assert hx_in.id in plant.view(rev.id).rows()


def test_pairing_and_container_mapping_fix_the_validator_findings(plant):
    base = plant.head()
    v = plant.view(base)
    ahu = by_label(v.equipment, "AHU")
    to_coil = next(c for c in v.connections if c.to_equipment.label == "Coil")
    from_coil = next(c for c in v.connections if c.from_equipment.label == "Coil")
    rev, cand = plant.edit(base, ops(
        {"op": "update_connection_point", "id": port(v, "HX", "inlet", "Boiler").id,
         "paired_with": port(v, "HX", "outlet", "Boiler").id},
        {"op": "update_connection_point", "id": port(v, "HX", "inlet", "Coil").id,
         "paired_with": port(v, "HX", "outlet", "Coil").id},
        {"op": "create_connection_point", "id": "new:in", "equipment": ahu.id, "direction": "inlet",
         "medium": "s223:Fluid-Water", "label": "AHU water in"},
        {"op": "create_connection_point", "id": "new:out", "equipment": ahu.id, "direction": "outlet",
         "medium": "s223:Fluid-Water", "label": "AHU water out"},
        {"op": "update_connection_point", "id": port(v, "Coil", "inlet", "HX").id, "maps_to": "new:in"},
        {"op": "update_connection_point", "id": port(v, "Coil", "outlet", "HX").id, "maps_to": "new:out"},
        {"op": "update_connection", "id": to_coil.id, "to_point": "new:in"},
        {"op": "update_connection", "id": from_coil.id, "from_point": "new:out"},
    ))
    fixed = violations(plant, base) - violations(plant, rev.id)
    assert any("HydronicHeatExchanger" in t and "paired" in t for t in fixed)
    assert any("contained equipment" in t for t in fixed)
    after = plant.view(rev.id)
    coil_in = next(c for c in after.connection_points if c.equipment.label == "Coil" and c.medium and c.maps_to)
    assert coil_in.connection is None  # the coil's port stayed (it has a mapping) when the pipe moved to the AHU
    assert next(c for c in after.connections if c.id == to_coil.id).to_equipment.label == "AHU"
    g = plant.graph(rev.id).model
    hx_in = URIRef(port(v, "HX", "inlet", "Boiler").iri)
    assert len(set(g.objects(hx_in, S223.pairedConnectionPoint))) == 1
    assert (None, S223.pairedConnectionPoint, hx_in) in g
    # The applied change is exactly the previewed one, and the touched ports now have ids.
    assert plant.graph(rev.id).id_of(hx_in) == port(v, "HX", "inlet", "Boiler").id
    assert cand.diff.added


def test_port_rules_are_checked(plant):
    base = plant.head()
    v = plant.view(base)
    hx_in, other_in = port(v, "HX", "inlet", "Boiler"), port(v, "HX", "inlet", "Coil")
    boiler_out = port(v, "Boiler", "outlet", "HX")
    with pytest.raises(OperationError) as exc:
        plant.build_candidate(base, ops(
            {"op": "update_connection_point", "id": hx_in.id, "paired_with": other_in.id},
            {"op": "update_connection_point", "id": boiler_out.id, "maps_to": hx_in.id},
        ))
    text = " ".join(exc.value.problems)
    assert "pair an inlet with an outlet" in text
    assert "equipment that contains Boiler" in text
    with pytest.raises(OperationError, match="already joined"):
        plant.build_candidate(base, ops({"op": "create_connection", "from_point": boiler_out.id,
                                         "to_equipment": hx_in.equipment.id}))


def test_create_connection_between_existing_points_and_delete_cascade(plant):
    base = plant.head()
    v = plant.view(base)
    ahu, coil = by_label(v.equipment, "AHU"), by_label(v.equipment, "Coil")
    rev, _ = plant.edit(base, ops(
        {"op": "create_connection_point", "id": "new:a", "equipment": coil.id, "direction": "outlet", "medium": "s223:Fluid-Air"},
        {"op": "create_connection_point", "id": "new:b", "equipment": ahu.id, "direction": "outlet", "medium": "s223:Fluid-Air",
         "label": "AHU supply"},
        {"op": "update_connection_point", "id": "new:a", "maps_to": "new:b"},
        {"op": "create_equipment", "id": "new:room", "label": "Room", "type": "s223:Fan"},
        {"op": "create_connection_point", "id": "new:c", "equipment": "new:room", "direction": "inlet", "medium": "s223:Fluid-Air"},
        {"op": "create_connection", "id": "new:duct", "from_point": "new:b", "to_point": "new:c"},
    ))
    after = plant.view(rev.id)
    supply = by_label(after.connection_points, "AHU supply")
    duct = next(c for c in after.connections if c.id == supply.connection.id)
    assert duct.medium.label == "Air" and duct.from_equipment.label == "AHU" and duct.to_equipment.label == "Room"
    assert supply.mapped_from is not None
    rev2, cand = plant.edit(rev.id, ops({"op": "delete_connection_point", "id": supply.id}))
    gone = {c.entity_id for c in cand.changes if c.change == "deleted"}
    assert {supply.id, duct.id} <= gone
    assert all(c.maps_to is None for c in plant.view(rev2.id).connection_points)


def test_issues_name_the_connection_points_they_concern(plant):
    rid = plant.head()
    rows = plant.view(rid).rows()
    port_issues = [i for i in plant.issues(rid) if any(rows.get(a) and rows[a].kind == "connection_point"
                                                       for a in i.affected_ids)]
    assert port_issues and all(rows[i.affected_ids[0]].kind != "connection_point" for i in port_issues)
    assert not any("urn:workbench" in i.explanation for i in port_issues)


def test_brick_has_no_connection_points(workspace):
    p = workspace.create("HVAC", "brick")
    p.edit(p.head(), ops({"op": "create_equipment", "id": "new:a", "label": "AHU", "type": "brick:AHU"}))
    eq = p.view(p.head()).equipment[0]
    with pytest.raises(OperationError, match="connection points do not exist in Brick"):
        p.build_candidate(p.head(), ops({"op": "create_connection_point", "equipment": eq.id,
                                         "direction": "inlet", "medium": "s223:Fluid-Air"}))
