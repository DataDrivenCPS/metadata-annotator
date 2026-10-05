"""Room adjacency the RealEstateCore way: both rooms rec:adjacentElement one shared element."""

import pytest
from rdflib import RDF, URIRef

from conftest import by_label
from workbench.operations import OperationError, OperationList
from workbench.vocabulary import REC


def ops(*raw):
    return OperationList.validate_python(list(raw))


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


def test_make_adjacent_shares_one_wall_and_is_idempotent(floor):
    p = floor
    a, b, _ = rooms(p)
    rev, cand = p.edit(p.head(), ops({"op": "make_adjacent", "space": a.id, "other": b.id}))
    (wall,) = walls(p, rev.id)
    g = p.graph(rev.id).model
    w = URIRef(wall.iri)
    assert (w, RDF.type, REC.Wall) in g
    assert (URIRef(a.iri), REC.adjacentElement, w) in g and (URIRef(b.iri), REC.adjacentElement, w) in g
    a2, b2, _ = rooms(p, rev.id)
    assert [x.id for x in a2.adjacent] == [b.id] and [x.id for x in b2.adjacent] == [a.id]
    assert {c.entity_id for c in cand.changes} >= {a.id, b.id, wall.id}
    # stating it again (even twice in one proposal) adds nothing
    rev2, cand2 = p.edit(rev.id, ops({"op": "make_adjacent", "space": b.id, "other": a.id}))
    assert len(walls(p, rev2.id)) == 1 and not cand2.diff.added


def test_a_pair_stated_twice_in_one_proposal_shares_one_wall(floor):
    p = floor
    a, b, _ = rooms(p)
    rev, _ = p.edit(p.head(), ops({"op": "make_adjacent", "space": a.id, "other": b.id},
                                  {"op": "make_adjacent", "space": b.id, "other": a.id}))
    assert len(walls(p, rev.id)) == 1


def test_existing_shared_wall_is_reused_and_unmake_cleans_up(floor):
    p = floor
    a, b, c = rooms(p)
    rev, _ = p.edit(p.head(), ops({"op": "make_adjacent", "space": a.id, "other": b.id},
                                  {"op": "make_adjacent", "space": b.id, "other": c.id}))
    assert len(walls(p, rev.id)) == 2
    rev2, cand = p.edit(rev.id, ops({"op": "unmake_adjacent", "space": a.id, "other": b.id}))
    assert len(walls(p, rev2.id)) == 1  # the A|B wall had no other use, so it went
    a2, b2, c2 = rooms(p, rev2.id)
    assert a2.adjacent == [] and [x.id for x in b2.adjacent] == [c.id]


def test_a_wall_shared_with_a_third_room_is_not_split(floor):
    p = floor
    a, b, c = rooms(p)
    rev, _ = p.edit(p.head(), ops({"op": "make_adjacent", "space": a.id, "other": b.id}))
    (wall,) = walls(p, rev.id)
    rev2, _ = p.edit(rev.id, ops({"op": "make_adjacent", "space": a.id, "other": c.id, "element": wall.id}))
    assert len(walls(p, rev2.id)) == 1
    with pytest.raises(OperationError, match="also next to"):
        p.build_candidate(rev2.id, ops({"op": "unmake_adjacent", "space": a.id, "other": b.id}))


def test_adjacency_is_rec_only_and_owned(floor, workspace):
    p = floor
    a, b, _ = rooms(p)
    rev, _ = p.edit(p.head(), ops({"op": "make_adjacent", "space": a.id, "other": b.id}))
    (wall,) = walls(p, rev.id)
    with pytest.raises(OperationError, match="make_adjacent"):
        p.build_candidate(rev.id, ops({"op": "relate", "subject": a.id, "relation": "rec:adjacentElement",
                                       "object": wall.id}))
    q = workspace.create("223P", "223p")
    q.edit(q.head(), ops({"op": "create_space", "id": "new:x", "label": "X", "type": "s223:PhysicalSpace"},
                         {"op": "create_space", "id": "new:y", "label": "Y", "type": "s223:PhysicalSpace"}))
    x, y = q.view(q.head()).spaces
    with pytest.raises(OperationError, match="RealEstateCore"):
        q.build_candidate(q.head(), ops({"op": "make_adjacent", "space": x.id, "other": y.id}))


def test_adjacency_adds_no_violations(floor):
    p = floor
    a, b, _ = rooms(p)
    before = {i.explanation for i in p.issues(p.head()) if i.severity == "violation"}
    rev, _ = p.edit(p.head(), ops({"op": "make_adjacent", "space": a.id, "other": b.id}))
    after = {i.explanation for i in p.issues(rev.id) if i.severity == "violation"}
    assert after <= before, after - before


def test_resolved_adjacency_resolves_to_itself(floor):
    # Applying a stored proposal re-resolves its (already resolved) operations.
    from workbench.operations import resolve

    p = floor
    a, b, _ = rooms(p)
    pg = p.graph(p.head())
    once = resolve(pg, p.vocab, ops({"op": "make_adjacent", "space": a.id, "other": b.id}))
    twice = resolve(pg, p.vocab, once)
    assert [o.model_dump() for o in once] == [o.model_dump() for o in twice] and once[0].create_element
