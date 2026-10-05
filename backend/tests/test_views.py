"""Declarative table views: curated specs, workbench.toml specs, path evaluation, editable cells."""

import pytest
from conftest import by_label
from workbench import views
from workbench.operations import OperationList
from workbench.relations import Path, PathError
from workbench.vocabulary import REC, S223


def ops(*raw):
    return OperationList.validate_python(list(raw))


def test_paths_parse_with_vocabulary_prefixes(registry):
    v = registry.get("brick")
    path = Path.parse(v, "rec:adjacentElement/^rec:adjacentElement | rec:isPartOf")
    assert path.alternatives == [[(str(REC.adjacentElement), False), (str(REC.adjacentElement), True)],
                                 [(str(REC.isPartOf), False)]]
    assert Path.parse(v, "^rec:adjacentElement").single_step == (str(REC.adjacentElement), True)
    assert Path.parse(v, "virtual:adjacent").single_step == ("urn:workbench:virtual#adjacent", False)
    with pytest.raises(PathError):
        Path.parse(v, "nope:thing")


def test_curated_and_configured_specs(registry):
    specs, errors = views.load_specs({"zones": {"label": "HVAC zones"},
                                      "mine": {"label": "Mine", "rows": ["s223:Zone"], "families": ["s223"],
                                               "columns": [{"key": "x", "label": "X", "path": "s223:nope"}]}})
    by_id = {s.id: s for s in specs}
    assert not errors and by_id["zones"].label == "HVAC zones" and by_id["zones"].rows == ["s223:Zone"]
    problems = views.check_spec(registry.get("watr"), by_id["mine"])
    assert any("s223:nope is not a relation" in p for p in problems)
    assert not views.check_spec(registry.get("watr"), by_id["zones"])
    assert not views.check_spec(registry.get("brick"), by_id["building_elements"])


@pytest.fixture
def zoned(workspace):
    p = workspace.create("Zoned", "watr")
    p.edit(p.head(), ops(
        {"op": "create_space", "id": "new:o1", "label": "Office 1", "type": "s223:PhysicalSpace"},
        {"op": "create_entity", "id": "new:d1", "label": "Office 1 HVAC", "type": "s223:DomainSpace"},
        {"op": "create_entity", "id": "new:z", "label": "Zone A", "type": "s223:Zone"},
        {"op": "relate", "subject": "new:o1", "relation": "s223:encloses", "object": "new:d1"},
        {"op": "relate", "subject": "new:z", "relation": "s223:hasDomainSpace", "object": "new:d1"},
        {"op": "relate", "subject": "new:z", "relation": "s223:hasDomain", "object": "s223:Domain-HVAC"},
    ))
    return p


def test_zone_view_follows_paths_and_offers_edits(zoned):
    p = zoned
    spec = next(s for s in views.load_specs()[0] if s.id == "zones")
    out = views.evaluate(p.graph(p.head()), p.vocab, p.view(p.head()), spec)
    cols = {c["key"]: c for c in out["columns"]}
    assert cols["domain_spaces"]["editor"] == "relation" and cols["spaces"]["editor"] == "relation"
    assert [c["label"] for c in cols["spaces"]["candidates"]] == ["Office 1"]  # physical spaces only
    assert any(c["label"] == "Office 1 HVAC" for c in cols["domain_spaces"]["candidates"])
    assert any(c.get("curie") == "s223:Domain-Lighting" for c in cols["domain"]["candidates"])
    (row,) = out["rows"]
    assert row["label"] == "Zone A"
    assert [c["label"] for c in row["cells"]["spaces"]] == ["Office 1"]  # virtual:serves_space
    member = row["cells"]["domain_spaces"][0]
    assert member["label"] == "Office 1 HVAC" and member["relationship"].startswith("rl-")
    # the relationship id in a cell removes exactly that fact
    rev, _ = p.edit(p.head(), ops({"op": "unrelate", "id": member["relationship"]}))
    out2 = views.evaluate(p.graph(rev.id), p.vocab, p.view(rev.id), spec)
    assert out2["rows"][0]["cells"]["domain_spaces"] == []


def test_inverse_columns_and_other_things(zoned):
    p = zoned
    specs = {s.id: s for s in views.load_specs()[0]}
    ds = views.evaluate(p.graph(p.head()), p.vocab, p.view(p.head()), specs["domain_spaces"])
    (row,) = ds["rows"]
    assert [c["label"] for c in row["cells"]["enclosed_by"]] == ["Office 1"]
    assert [c["label"] for c in row["cells"]["zone"]] == ["Zone A"]
    other = views.evaluate(p.graph(p.head()), p.vocab, p.view(p.head()), specs["other"])
    counts = {r["label"]: r["cells"]["relations"][0]["label"] for r in other["rows"]}
    assert counts == {"Office 1 HVAC": "2", "Zone A": "2"}


def test_typed_table_extensions_merge():
    specs, _ = views.load_specs({"space_notes": {"label": "Notes", "builtin": "spaces",
                                                 "columns": [{"key": "parts", "label": "Parts", "path": "rec:hasPart"}]}})
    brick = views.for_project(specs, "brick", "brick")
    (spaces,) = [s for s in brick if s.builtin == "spaces"]
    assert [c.key for c in spaces.columns] == ["adjacent", "parts"]
    assert "building_elements" in {s.id for s in brick} and "zones" not in {s.id for s in brick}
