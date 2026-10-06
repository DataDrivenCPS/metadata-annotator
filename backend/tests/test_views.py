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
    assert [c["label"] for c in cols["spaces"]["candidates"] if c["fits"]] == ["Office 1"]  # physical spaces fit
    assert cols["spaces"]["candidates"][0]["label"] == "Office 1"  # first; the rest are offered after
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


def test_tabs_per_profile():
    specs, errors = views.load_specs()
    assert not errors
    tabs = {prof: [v.id for v in views.for_project(specs, fam, prof) if not v.builtin]
            for fam, prof in (("s223", "watr"), ("s223", "223p"), ("brick", "brick"))}
    # typed tables are views too, first and in order; each profile shows what fits it
    assert tabs["watr"][:4] == ["points", "equipment", "connections", "connection_points"]
    assert "processes" in tabs["watr"] and not {"spaces", "zones", "domain_spaces"} & set(tabs["watr"])
    assert {"spaces", "zones", "domain_spaces"} <= set(tabs["223p"]) and "processes" not in tabs["223p"]
    assert "connection_points" not in tabs["brick"] and "building_elements" in tabs["brick"]
    # workbench.toml can bring a hidden tab back
    specs, _ = views.load_specs({"spaces": {"exclude_profiles": []}})
    assert "spaces" in [v.id for v in views.for_project(specs, "s223", "watr")]


def test_processes_lists_vocabulary_terms_with_the_equipment_performing_them(sample_project):
    p = sample_project
    spec = next(s for s in views.load_specs()[0] if s.id == "processes")
    assert not views.check_spec(p.vocab, spec)
    out = views.evaluate(p.graph(p.head()), p.vocab, p.view(p.head()), spec)
    rows = {r["label"]: r for r in out["rows"]}
    assert len(rows) > 50 and all(r["kind"] == "term" for r in out["rows"])  # every process, used or not
    ro = rows["Reverse Osmosis (RO)"]
    assert [c["label"] for c in ro["cells"]["equipment"]] == ["RO-1 Reverse Osmosis Skid"]
    assert [c["label"] for c in ro["cells"]["kind"]] == ["Membrane Process"]  # from the vocabulary
    assert "desalination" in ro["cells"]["about"][0]["label"]
    col = next(c for c in out["columns"] if c["key"] == "equipment")
    assert col["editor"] == "relation" and col["inverse"]
    # assigning equipment to a process is relating it, from the process's row
    pump = by_label(p.view(p.head()).equipment, "P-101")
    rev, _ = p.edit(p.head(), ops({"op": "relate", "subject": pump.id, "relation": col["relation"],
                                   "object": rows["Filtration"]["id"]}))
    out = views.evaluate(p.graph(rev.id), p.vocab, p.view(rev.id), spec)
    filtration = next(r for r in out["rows"] if r["label"] == "Filtration")
    assert "P-101 Feed Pump" in [c["label"] for c in filtration["cells"]["equipment"]]


def test_media_lists_kinds_of_water_with_what_carries_and_measures_them(sample_project):
    p = sample_project
    spec = next(s for s in views.load_specs()[0] if s.id == "media")
    assert not views.check_spec(p.vocab, spec)
    out = views.evaluate(p.graph(p.head()), p.vocab, p.view(p.head()), spec)
    rows = {r["label"]: r["cells"] for r in out["rows"]}
    brine = rows["Water-Brine"]
    assert {c["label"] for c in brine["constituents"]} == {"H2O", "Salt-NaCl"}  # from the vocabulary
    assert [c["label"] for c in brine["carried_by"]] == ["L-06"]  # only = s223:Connection: no connection points
    assert "CT-201" in [c["label"] for c in rows["Water-Freshwater"]["measured_by"]]
    col = next(c for c in out["columns"] if c["key"] == "carried_by")
    assert col["candidates"] and all(c["label"].startswith("L-") for c in col["candidates"])
