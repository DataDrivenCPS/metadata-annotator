"""Generic entities (any ontology class) and relationships (any ontology relation)."""

import pytest
from rdflib import URIRef

from conftest import by_label
from workbench.operations import OperationError, OperationList
from workbench.vocabulary import REC, S223


def ops(*raw):
    return OperationList.validate_python(list(raw))


def rel(view, relation_curie_suffix, subject_label):
    return [r for r in view.relationships if r.relation.iri.endswith(relation_curie_suffix)
            and r.subject.label == subject_label]


def test_vocabulary_shapes_say_what_relates_to_what(registry):
    brick = registry.get("brick")
    room = {r["relation"]: r for r in brick.relations_for([str(REC.Room)])}
    assert str(REC.BuildingElement) in room[str(REC.adjacentElement)]["objects"]
    zone = {r["relation"]: r for r in registry.get("watr").relations_for([str(S223.Zone)])}
    assert str(S223.DomainSpace) in zone[str(S223.hasDomainSpace)]["objects"]
    assert zone[str(S223.hasDomain)]["max"] == 1
    assert registry.get("watr").term(str(S223.connected)).symmetric


@pytest.fixture
def zones(workspace):
    p = workspace.create("Zones", "watr")
    p.edit(p.head(), ops(
        {"op": "create_entity", "id": "new:z", "label": "Zone A", "type": "s223:Zone"},
        {"op": "create_entity", "id": "new:d1", "label": "Office 1 HVAC", "type": "s223:DomainSpace"},
        {"op": "create_entity", "id": "new:d2", "label": "Office 2 HVAC", "type": "s223:DomainSpace"},
        {"op": "relate", "subject": "new:z", "relation": "s223:hasDomainSpace", "object": "new:d1"},
        {"op": "relate", "subject": "new:z", "relation": "s223:hasDomainSpace", "object": "new:d2"},
        {"op": "relate", "subject": "new:z", "relation": "s223:hasDomain", "object": "s223:Domain-HVAC"},
    ))
    return p


def test_entities_and_relationships_round_trip(zones):
    p = zones
    v = p.view(p.head())
    zone = by_label(v.entities, "Zone A")
    assert zone.type.iri == str(S223.Zone) and zone.relation_count == 3
    members = rel(v, "hasDomainSpace", "Zone A")
    assert sorted(r.object.label for r in members) == ["Office 1 HVAC", "Office 2 HVAC"]
    domain = rel(v, "hasDomain", "Zone A")[0]
    assert domain.object is None and domain.value.iri == str(S223["Domain-HVAC"])
    g = p.graph(p.head()).model
    assert (URIRef(zone.iri), S223.hasDomainSpace, URIRef(by_label(v.entities, "Office 1 HVAC").iri)) in g
    # ids are stable across revisions and unrelate removes exactly that fact
    rid = members[0].id
    rev, _ = p.edit(p.head(), ops({"op": "update_entity", "id": zone.id, "label": "Zone A1"}))
    assert rid in p.view(rev.id).rows()
    rev2, cand = p.edit(rev.id, ops({"op": "unrelate", "id": rid}))
    assert rid not in p.view(rev2.id).rows()
    assert [c.change for c in cand.changes if c.entity_id == rid] == ["deleted"]


def test_one_valued_relations_replace_and_misfits_are_notes(zones):
    p = zones
    v = p.view(p.head())
    zone, office = by_label(v.entities, "Zone A"), by_label(v.entities, "Office 1 HVAC")
    rev, cand = p.edit(p.head(), ops({"op": "relate", "subject": zone.id, "relation": "s223:hasDomain",
                                      "object": "s223:Domain-Lighting"}))
    assert [r.value.iri for r in rel(p.view(rev.id), "hasDomain", "Zone A")] == [str(S223["Domain-Lighting"])]
    assert any("was replaced by" in n for n in cand.result.notes)
    # Relations that do not fit the shapes are allowed, with a note; validation reports the rest.
    cand = p.build_candidate(rev.id, ops({"op": "relate", "subject": zone.id, "relation": "s223:hasDomainSpace",
                                          "object": zone.id}))
    assert any("expects s223:DomainSpace" in n for n in cand.result.notes)
    cand = p.build_candidate(rev.id, ops({"op": "relate", "subject": office.id, "relation": "s223:hasDomainSpace",
                                          "object": office.id}))
    assert any("not used for" in n for n in cand.result.notes)
    with pytest.raises(OperationError, match="not a relation"):  # but only relations the vocabulary defines
        p.build_candidate(rev.id, ops({"op": "relate", "subject": zone.id, "relation": "s223:likes", "object": office.id}))


def test_any_relation_and_any_class(zones):
    p = zones
    zone = by_label(p.view(p.head()).entities, "Zone A")
    # a relation a typed field also shows is fine through relate
    p.build_candidate(p.head(), ops({"op": "relate", "subject": zone.id, "relation": "s223:hasProperty",
                                     "object": zone.id}))
    # create_entity makes any class; the node is what its class makes it
    rev, _ = p.edit(p.head(), ops({"op": "create_entity", "id": "new:r", "label": "Room", "type": "s223:PhysicalSpace"},
                                  {"op": "create_equipment", "label": "Fan", "type": "s223:Fan", "location": "new:r"}))
    v = p.view(rev.id)
    assert by_label(v.equipment, "Fan").location.label == "Room"
    room = by_label(v.spaces, "Room")
    rev2, _ = p.edit(rev.id, ops({"op": "update_entity", "id": room.id, "label": "Room 1"},
                                 {"op": "delete_entity", "id": by_label(v.equipment, "Fan").id}))
    assert [s.label for s in p.view(rev2.id).spaces] == ["Room 1"] and not p.view(rev2.id).equipment


def test_symmetric_and_inverse_statements_are_one_fact(registry):
    # Every symmetric relation in these vocabularies belongs to a typed editor (s223:connected,
    # cnx, pairedConnectionPoint), so check the identity rule itself: either way round is one fact.
    from workbench.projection import relationship_id, relationship_key
    v = registry.get("watr")
    a, b = URIRef("urn:x/a"), URIRef("urn:x/b")
    assert relationship_id(relationship_key(v, a, S223.connected, b)) == relationship_id(relationship_key(v, b, S223.connected, a))
    brick = registry.get("brick")
    inv = next(t for t in brick.terms.values() if t.kind == "relation" and t.inverse)
    assert relationship_key(brick, a, URIRef(inv.iri), b) == relationship_key(brick, b, URIRef(inv.inverse), a)


def test_brick_entities_and_imported_relations(workspace):
    p = workspace.create("Brick", "brick")
    rev, _ = p.edit(p.head(), ops(
        {"op": "create_entity", "id": "new:w", "label": "Wall W1", "type": "rec:Wall"},
        {"op": "create_entity", "id": "new:w2", "label": "Wall W2", "type": "rec:Wall"},
        {"op": "relate", "subject": "new:w2", "relation": "rec:isPartOf", "object": "new:w"},
    ))
    v = p.view(rev.id)
    assert by_label(v.entities, "Wall W1").type.iri == str(REC.Wall)
    assert len(rel(v, "isPartOf", "Wall W2")) == 1


def test_relations_endpoint_lists_allowed_and_current(zones, tmp_path):
    from fastapi.testclient import TestClient

    from workbench.api import create_app
    from workbench.config import load_settings

    p = zones
    settings = load_settings()
    data_dir = p.root.parent.parent  # the workspace's data dir (projects/<id>)
    try:  # reuse the already loaded vocabularies
        (data_dir / "cache").symlink_to(settings.cache_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable (Windows without developer mode)")
    settings.data_dir = data_dir
    client = TestClient(create_app(settings))
    zone = by_label(p.view(p.head()).entities, "Zone A")
    body = client.get(f"/api/projects/{p.id}/entities/{zone.id}/relations").json()
    allowed = {a["curie"]: a for a in body["allowed"]}
    assert "s223:hasDomainSpace" in allowed and allowed["s223:hasDomain"]["max"] == 1
    assert allowed["s223:hasProperty"]["typed_field"] == "points"  # listed, with the field that also shows it
    assert len(body["relationships"]) == 3


@pytest.mark.parametrize("profile, equipment_type, space_type, relation", [
    ("brick", "brick:Pump", "rec:Room", "brick:hasLocation"),
    ("watr", "s223:Pump", "s223:PhysicalSpace", "s223:hasPhysicalLocation"),
])
def test_generic_location_edits_lock_the_owning_entity(workspace, profile, equipment_type, space_type, relation):
    from workbench.relations import relationship_id, relationship_key
    from workbench.vocabulary import expand_term

    p = workspace.create("Locations", profile)
    p.edit(p.head(), ops(
        {"op": "create_equipment", "label": "Pump", "type": equipment_type},
        {"op": "create_space", "label": "Room", "type": space_type},
    ))
    view = p.view(p.head())
    pump, room = view.equipment[0], view.spaces[0]
    rev, cand = p.edit(p.head(), ops({"op": "relate", "subject": pump.id, "relation": relation, "object": room.id}))
    assert "location" in p.view(rev.id).rows()[pump.id].locked
    assert "location" in cand.result.changes[pump.id]
    pg = p.graph(rev.id)
    predicate = URIRef(expand_term(p.vocab, relation))
    rid = relationship_id(relationship_key(p.vocab, pg.iri(pump.id), predicate, pg.iri(room.id)))
    from workbench.graph import WB
    pg.ann.remove((pg.iri(pump.id), WB.locked, None))
    # An automatic build can set an unlocked value which a person then reconfirms.
    from workbench.operations import apply
    apply(pg, p.vocab, cand.ops)
    assert "location" in pg.locked_fields(pg.iri(pump.id))
    if profile == "brick":
        pg.ann.remove((pg.iri(pump.id), WB.locked, None))
        from workbench.operations import resolve
        inverse = resolve(pg, p.vocab, ops({"op": "relate", "subject": room.id,
                                          "relation": "brick:isLocationOf", "object": pump.id}))
        apply(pg, p.vocab, inverse)
        assert "location" in pg.locked_fields(pg.iri(pump.id))
    removal = p.build_candidate(rev.id, ops({"op": "unrelate", "id": rid}))
    assert "location" in removal.result.changes[pump.id]
    assert next(c for c in removal.changes if c.entity_id == pump.id).overrides_locked
