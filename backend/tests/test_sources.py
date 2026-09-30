"""CSV structure presets: deterministic records with coordinates; duplicates stay separate."""

from pathlib import Path

from workbench.schemas import CsvImportConfig
from workbench.sources import extract_records, read_grid, suggest_config

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "ro-train"


def grid(name):
    return read_grid((SAMPLES / name).read_bytes())


def test_rows_describe_points_keeps_duplicates_and_coordinates():
    rows, delim = grid("points.csv")
    cfg, _ = suggest_config(rows, delim)
    assert cfg.layout == "row_points" and cfg.name_column == 0 and cfg.header_row == 0
    recs = extract_records(rows, cfg)
    pt201 = [r for r in recs if r["name"] == "PT-201"]
    assert len(pt201) == 2  # the redundant transmitter is a separate record
    assert pt201[0]["location"]["row"] != pt201[1]["location"]["row"]
    assert pt201[0]["metadata"]["Units"] == "psi"


def test_headers_are_point_names():
    rows, delim = grid("historian.csv")
    cfg, _ = suggest_config(rows, delim)
    assert cfg.layout == "header_points"
    names = [r["name"] for r in extract_records(rows, cfg)]
    assert "Timestamp" not in names and "Quality" not in names and "LT-101" in names


def test_columns_describe_points():
    rows, delim = grid("points_wide.csv")
    cfg, _ = suggest_config(rows, delim)
    assert cfg.layout == "column_points"
    recs = extract_records(rows, cfg)
    assert recs[0]["name"] == "LT-101" and recs[0]["metadata"]["Units"] == "ft"
    assert recs[0]["location"] == {"kind": "csv_column", "column": 1}


def test_single_column_without_header():
    rows, delim = read_grid(b"A1.RM1105:DMPR COMD\nA1.RM1105.Zone Air Temp\nA1.RM1105:DMPR COMD\n")
    cfg, _ = suggest_config(rows, delim)
    assert cfg.layout == "row_points" and cfg.header_row is None
    recs = extract_records(rows, cfg)
    assert [r["location"]["row"] for r in recs] == [0, 1, 2]


def test_explicit_config_overrides():
    rows, delim = grid("points.csv")
    cfg = CsvImportConfig(layout="row_points", delimiter=delim, name_column=1, header_row=0, metadata_columns=[0])
    recs = extract_records(rows, cfg)
    assert recs[0]["name"] == "Raw water tank level" and recs[0]["metadata"] == {"Tag": "LT-101"}


def test_confirm_creates_observations_and_supersedes(sample_project):
    p = sample_project
    src = p.add_source("points.csv", (SAMPLES / "points.csv").read_bytes())
    rows, delim = p.source_grid(src.id)
    cfg, _ = suggest_config(rows, delim)
    first = p.confirm_csv_mapping(src.id, cfg)
    assert first["observations"] == 15
    again = p.confirm_csv_mapping(src.id, cfg)
    assert again["superseded"] == 15
    live = [o for o in p.observations(src.id) if o.status == "unresolved"]
    assert len(live) == 15 and p.source(src.id).status == "configured"
