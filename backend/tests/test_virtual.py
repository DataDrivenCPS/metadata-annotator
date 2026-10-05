"""Virtual relations: named paths over ontology relations, set through a created middle node.

Brick: virtual:adjacent (RealEstateCore, both rooms rec:adjacentElement one shared rec:Wall).
223P: virtual:serves_space (a zone hasDomainSpace a domain space the physical space encloses).
"""

import pytest
from rdflib import RDF, URIRef

from conftest import by_label
from workbench import views
from workbench.operations import OperationError, OperationList, resolve
from workbench.vocabulary import REC, S223

ADJ = "virtual:adjacent"


def ops(*raw):
    return OperationList.validate_python(list(raw))


def adjacent(a, b):
    return {"op": "relate", "subject": a, "relation": ADJ, "object": b}


@pytest.fixture
def floor(workspace):
    p = workspace.create("Floor", "brick")
    p.edit(p.head(), ops(*[{"op": "create_space", "label": f"Room {n}", "type": "rec:Office"} for n in (101, 102, 103)]))
    return p


def rooms(p, rid=None):
    v = p.view(rid or p.head())
    return [by_label(v.spaces, f"Room {n}") for n in (101, 102, 103)]


def walls(p, rid=None):
    return [e for e in p.view(rid or p.head()).entities if e.type and e.type.iri == str(REC.Wall)]


def virtual_rows(p, rid=None):
    pairs = [(r.subject.label, r.object.label) for r in p.view(rid or p.head()).relationships if r.virtual]
    return sorted(tuple(sorted(x)) if r.symmetric else x
                  for x, r in zip(pairs, [r for r in p.view(rid or p.head()).relationships if r.virtual]))


def test_relating_shares_one_wall_and_is_idempotent(floor):
    p = floor
    a, b, _ = rooms(p)
    rev, cand = p.edit(p.head(), ops(adjacent(a.id, b.id)))
    (wall,) = walls(p, rev.id)
    assert wall.label == "Wall: Room 101 | Room 102"
    g = p.graph(rev.id).model
    w = URIRef(wall.iri)
    assert (w, RDF.type, REC.Wall) in g
    assert (URIRef(a.iri), REC.adjacentElement, w) in g and (URIRef(b.iri), REC.adjacentElement, w) in g
    assert virtual_rows(p, rev.id) == [("Room 101", "Room 102")]  # symmetric: listed once
    # the proposal shows the wall, both links, and the adjacency itself
    changed = {c.entity_kind for c in cand.changes}
    assert {"entity", "relationship"} <= changed
    assert any(c.label in ("Room 101 adjacent to Room 102", "Room 102 adjacent to Room 101") for c in cand.changes)
    # stating it again, either way round or twice in one proposal, adds nothing
    rev2, cand2 = p.edit(rev.id, ops(adjacent(b.id, a.id)))
    assert len(walls(p, rev2.id)) == 1 and not cand2.diff.added
    rev3, _ = p.edit(p.head(), ops(adjacent(a.id, rooms(p)[2].id), adjacent(rooms(p)[2].id, a.id)))
    assert len(walls(p, rev3.id)) == 2


def test_unrelate_removes_the_links_and_the_unused_wall(floor):
    p = floor
    a, b, c = rooms(p)
    rev, _ = p.edit(p.head(), ops(adjacent(a.id, b.id), adjacent(b.id, c.id)))
    assert len(walls(p, rev.id)) == 2
    ab = next(r for r in p.view(rev.id).relationships if r.virtual and {r.subject.id, r.object.id} == {a.id, b.id})
    rev2, _ = p.edit(rev.id, ops({"op": "unrelate", "id": ab.id}))
    assert len(walls(p, rev2.id)) == 1  # the A|B wall had no other use, so it went
    assert virtual_rows(p, rev2.id) == [("Room 102", "Room 103")]


def test_a_wall_shared_with_a_third_room_is_not_split(floor):
    p = floor
    a, b, c = rooms(p)
    rev, _ = p.edit(p.head(), ops(adjacent(a.id, b.id)))
    (wall,) = walls(p, rev.id)
    # a plain relation puts a third room against the same wall
    rev2, _ = p.edit(rev.id, ops({"op": "relate", "subject": c.id, "relation": "rec:adjacentElement", "object": wall.id}))
    assert len(virtual_rows(p, rev2.id)) == 3
    ab = next(r for r in p.view(rev2.id).relationships if r.virtual and {r.subject.id, r.object.id} == {a.id, b.id})
    with pytest.raises(OperationError, match="also links Room 103"):
        p.build_candidate(rev2.id, ops({"op": "unrelate", "id": ab.id}))


def test_virtual_relations_are_per_family_and_need_entities(floor, workspace):
    p = floor
    a, _, _ = rooms(p)
    with pytest.raises(OperationError, match="two different entities"):
        p.build_candidate(p.head(), ops(adjacent(a.id, a.id)))
    with pytest.raises(OperationError, match="not found: sp-nope"):
        p.build_candidate(p.head(), ops(adjacent(a.id, "sp-nope")))
    q = workspace.create("223P", "223p")
    q.edit(q.head(), ops({"op": "create_space", "id": "new:x", "label": "X", "type": "s223:PhysicalSpace"},
                         {"op": "create_space", "id": "new:y", "label": "Y", "type": "s223:PhysicalSpace"}))
    x, y = q.view(q.head()).spaces
    with pytest.raises(OperationError, match="not a relation"):
        q.build_candidate(q.head(), ops(adjacent(x.id, y.id)))


def test_adjacency_adds_no_violations(floor):
    p = floor
    a, b, _ = rooms(p)
    before = {i.explanation for i in p.issues(p.head()) if i.severity == "violation"}
    rev, _ = p.edit(p.head(), ops(adjacent(a.id, b.id)))
    after = {i.explanation for i in p.issues(rev.id) if i.severity == "violation"}
    assert after <= before, after - before


def test_resolved_operations_are_ordinary_and_resolve_to_themselves(floor):
    # Applying a stored proposal re-resolves its (already resolved) operations.
    p = floor
    a, b, _ = rooms(p)
    pg = p.graph(p.head())
    once = resolve(pg, p.vocab, ops(adjacent(a.id, b.id)))
    assert [o.op for o in once] == ["create_entity", "relate", "relate"]
    twice = resolve(pg, p.vocab, once)
    assert [o.model_dump() for o in once] == [o.model_dump() for o in twice]


def test_spaces_table_column_edits_adjacency(floor):
    p = floor
    a, b, _ = rooms(p)
    spec = next(s for s in views.load_specs()[0] if s.id == "space_adjacency")
    out = views.evaluate(p.graph(p.head()), p.vocab, p.view(p.head()), spec)
    (col,) = out["columns"]
    assert col["editor"] == "relation" and col["relation_curie"] == ADJ
    assert {c["label"] for c in col["candidates"]} == {"Room 101", "Room 102", "Room 103"}
    rev, _ = p.edit(p.head(), ops(adjacent(a.id, b.id)))
    out = views.evaluate(p.graph(rev.id), p.vocab, p.view(rev.id), spec)
    cells = {r["label"]: r["cells"]["adjacent"] for r in out["rows"]}
    assert [c["label"] for c in cells["Room 102"]] == ["Room 101"]
    rid = cells["Room 102"][0]["relationship"]  # the virtual fact, removable from either side
    rev2, _ = p.edit(rev.id, ops({"op": "unrelate", "id": rid}))
    assert virtual_rows(p, rev2.id) == [] and walls(p, rev2.id) == []


# ------------------------------------------------------------------------- 223P zones

def test_zone_serves_spaces_through_domain_spaces(workspace):
    p = workspace.create("Zones", "watr")
    rev, _ = p.edit(p.head(), ops(
        {"op": "create_space", "id": "new:o1", "label": "Office 1", "type": "s223:PhysicalSpace"},
        {"op": "create_space", "id": "new:o2", "label": "Office 2", "type": "s223:PhysicalSpace"},
        {"op": "create_entity", "id": "new:z", "label": "Zone A", "type": "s223:Zone"},
        {"op": "relate", "subject": "new:z", "relation": "s223:hasDomain", "object": "s223:Domain-HVAC"},
        {"op": "relate", "subject": "new:z", "relation": "virtual:serves_space", "object": "new:o1"},
        {"op": "relate", "subject": "new:z", "relation": "virtual:serves_space", "object": "new:o2"},
    ))
    v = p.view(rev.id)
    domain_spaces = [e for e in v.entities if e.type and e.type.iri == str(S223.DomainSpace)]
    assert sorted(d.label for d in domain_spaces) == ["Office 1 (Zone A)", "Office 2 (Zone A)"]
    g = p.graph(rev.id).model
    for d in domain_spaces:  # the zone's domain is copied onto each domain space
        assert (URIRef(d.iri), S223.hasDomain, S223["Domain-HVAC"]) in g
    assert virtual_rows(p, rev.id) == [("Zone A", "Office 1"), ("Zone A", "Office 2")]
    zone_issues = [i for i in p.issues(rev.id) if i.severity == "violation"
                   and i.entity_id in {d.id for d in domain_spaces} | {by_label(v.entities, "Zone A").id}]
    assert not zone_issues, [i.explanation for i in zone_issues]
    # the Spaces table's Zones column reads the inverse
    spec = next(s for s in views.load_specs()[0] if s.id == "space_zones")
    out = views.evaluate(p.graph(rev.id), p.vocab, v, spec)
    assert {r["label"]: [c["label"] for c in r["cells"]["zones"]] for r in out["rows"]} == \
        {"Office 1": ["Zone A"], "Office 2": ["Zone A"]}
    assert out["columns"][0]["editor"] == "relation" and out["columns"][0]["inverse"]


def test_relations_for_lists_virtual_relations(registry):
    brick = registry.get("brick")
    assert "urn:workbench:virtual#adjacent" in {r["relation"] for r in brick.relations_for([str(REC.Office)])}
    assert "urn:workbench:virtual#adjacent" not in {r["relation"] for r in brick.relations_for([str(REC.Wall)])}
    watr = registry.get("watr")
    zone = {r["relation"]: r for r in watr.relations_for([str(S223.Zone)])}
    assert zone["urn:workbench:virtual#serves_space"]["objects"] == [str(S223.PhysicalSpace)]
    assert not brick.virtual_errors and not watr.virtual_errors
