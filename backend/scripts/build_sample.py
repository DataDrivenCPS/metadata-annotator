"""Build the sample project inputs in samples/ro-train/ deterministically.

    uv run python scripts/build_sample.py

Outputs:
  model.ttl         a WaTr/223P model of a small brackish-water RO train, built with the
                    app's own operations. It contains three deliberate mistakes used in
                    the walkthrough (docs/walkthrough.md):
                      * CT-201 (permeate conductivity) has unit mg/L instead of uS/cm
                      * FT-201 and FT-301 are assigned to P-201 instead of RO-1
                      * the concentrate pipe runs RO-1 -> TK-201 instead of RO-1 -> TK-301
  points.csv        the plant point list (rows describe points)
  historian.csv     a historian export (headers are point names)
  points_wide.csv   a vendor sheet (columns describe points)
  diagram.png       a simple P&ID-style diagram of the same train
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
OUT = ROOT.parent / "samples" / "ro-train"

from workbench import operations as O  # noqa: E402
from workbench.config import load_settings  # noqa: E402
from workbench.graph import ProjectGraph  # noqa: E402
from workbench.vocabulary import Vocabulary, VocabularyRegistry  # noqa: E402

W = "watr:"
EQUIPMENT = [
    # id, label, type, process
    ("TK-101", "TK-101 Raw Water Tank", W + "Tank", None),
    ("P-101", "P-101 Feed Pump", W + "Pump", None),
    ("CF-101", "CF-101 Cartridge Filter", W + "CartridgeFiltrationUnit", W + "Process-Filtration"),
    ("P-201", "P-201 High Pressure Pump", W + "Pump", None),
    ("RO-1", "RO-1 Reverse Osmosis Skid", W + "ReverseOsmosisMembrane", W + "Process-ReverseOsmosis"),
    ("TK-201", "TK-201 Permeate Tank", W + "Tank", None),
    ("TK-301", "TK-301 Concentrate Tank", W + "Tank", None),
]
CONNECTIONS = [
    # id, from, to, medium
    ("L-01", "TK-101", "P-101", W + "Water-Brackish"),
    ("L-02", "P-101", "CF-101", W + "Water-Brackish"),
    ("L-03", "CF-101", "P-201", W + "Water-Brackish"),
    ("L-04", "P-201", "RO-1", W + "Water-Brackish"),
    ("L-05", "RO-1", "TK-201", W + "Water-Freshwater"),
    # deliberate mistake: concentrate should go to TK-301
    ("L-06", "RO-1", "TK-201", W + "Water-Brine"),
]
QK = "quantitykind:"
U = "unit:"
POINTS = [
    # tag, description, kind, qk, unit, equipment, sensor, units text, io, extra
    ("LT-101", "Raw water tank level", "measurement", QK + "Length", U + "FT", "TK-101", W + "LevelSensor", "ft", "AI", {}),
    ("TT-101", "Raw water temperature", "measurement", QK + "Temperature", U + "DEG_C", "TK-101", "s223:TemperatureSensor", "degC", "AI", {}),
    ("P-101-STS", "Feed pump run status", "status", None, None, "P-101", None, "", "DI", {"enumeration_kind": "s223:Binary-OnOff"}),
    ("P-101-CMD", "Feed pump start/stop", "command", None, None, "P-101", None, "", "DO", {"enumeration_kind": "s223:Binary-OnOff"}),
    ("PT-101", "Feed pump discharge pressure", "measurement", QK + "Pressure", U + "PSI", "P-101", "s223:PressureSensor", "psi", "AI", {}),
    ("AT-101", "Feed pH", "measurement", QK + "Acidity", U + "PH", "P-101", W + "pHSensor", "pH", "AI", {}),
    ("PDT-101", "Cartridge filter differential pressure", "measurement", QK + "Pressure", U + "PSI", "CF-101", "s223:PressureSensor", "psid", "AI", {}),
    ("P-201-SPD", "HP pump speed command", "setpoint", QK + "DimensionlessRatio", U + "PERCENT", "P-201", None, "%", "AO", {}),
    ("PT-201", "RO feed pressure", "measurement", QK + "Pressure", U + "PSI", "P-201", "s223:PressureSensor", "psi", "AI", {}),
    ("CT-101", "RO feed conductivity", "measurement", QK + "ElectrolyticConductivity", U + "MicroS-PER-CentiM", "RO-1", W + "ConductivitySensor", "uS/cm", "AI", {}),
    # deliberate mistake: wrong unit (mg/L instead of uS/cm)
    ("CT-201", "Permeate conductivity", "measurement", QK + "ElectrolyticConductivity", U + "MilliGM-PER-L", "RO-1", W + "ConductivitySensor", "uS/cm", "AI", {}),
    # deliberate mistake: both flows assigned to the HP pump instead of the RO skid
    ("FT-201", "Permeate flow", "measurement", QK + "VolumeFlowRate", U + "GAL_US-PER-MIN", "P-201", W + "FlowSensor", "gpm", "AI", {}),
    ("FT-301", "Concentrate flow", "measurement", QK + "VolumeFlowRate", U + "GAL_US-PER-MIN", "P-201", W + "FlowSensor", "gpm", "AI", {}),
    ("LT-201", "Permeate tank level", "measurement", QK + "Length", U + "FT", "TK-201", W + "LevelSensor", "ft", "AI", {}),
]


def build_model(vocab: Vocabulary) -> ProjectGraph:
    from rdflib import Literal, URIRef
    from rdflib.namespace import OWL, RDF, RDFS

    ns = "urn:example:ro-train/"
    pg = ProjectGraph(ns)
    onto = URIRef(ns.rstrip("/"))
    pg.model.add((onto, RDF.type, OWL.Ontology))
    pg.model.add((onto, RDFS.label, Literal("Brackish water RO train (sample)")))
    pg.model.add((onto, OWL.imports, URIRef(vocab.root_ontology)))
    raw = []
    for eid, label, typ, proc in EQUIPMENT:
        raw.append({"op": "create_equipment", "id": eid, "label": label, "type": typ,
                    **({"process": proc} if proc else {})})
    for cid, a, b, medium in CONNECTIONS:
        raw.append({"op": "create_connection", "id": cid, "label": cid, "from_equipment": a,
                    "to_equipment": b, "medium": medium})
    for tag, desc, kind, qk, unit, eq, sensor, *_rest, extra in POINTS:
        op = {"op": "create_point", "id": tag, "label": tag, "point_kind": kind, "equipment": eq, **extra}
        if qk:
            op["quantity_kind"] = qk
        if unit:
            op["unit"] = unit
        if sensor:
            op["sensor_type"] = sensor
        if kind == "measurement":
            op["medium"] = W + ("Water-Freshwater" if tag in ("CT-201", "FT-201", "LT-201") else "Water-Brackish")
        raw.append(op)
    for prefix, iri in (("watr", vocab.namespaces["watr"]), ("unit", "http://qudt.org/vocab/unit/"),
                        ("quantitykind", "http://qudt.org/vocab/quantitykind/"),
                        ("qudt", "http://qudt.org/schema/qudt/")):
        pg.model.bind(prefix, iri)
    ops = O.resolve(pg, vocab, O.OperationList.validate_python(raw))
    result = O.apply(pg, vocab, ops, lock=False)
    for n in result.notes:
        print("note:", n)
    return pg


def write_csvs() -> None:
    with open(OUT / "points.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Tag", "Description", "Units", "IO Type", "Equipment"])
        for tag, desc, *_x, eq, _s, units, io, _e in POINTS:
            # the point list is correct; the seed model is what has mistakes
            w.writerow([tag, desc, units, io, "RO-1" if tag in ("FT-201", "FT-301") else eq])
        # a duplicate label that must remain a separate source record
        w.writerow(["PT-201", "RO feed pressure (redundant transmitter)", "psi", "AI", "P-201"])
    with open(OUT / "historian.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        tags = [p[0] for p in POINTS if p[2] == "measurement"]
        w.writerow(["Timestamp", *tags, "Quality"])
        for i in range(5):
            w.writerow([f"2026-09-01T00:{i:02d}:00Z", *[round(10 + j * 1.5 + i * 0.1, 2) for j in range(len(tags))], "Good"])
    with open(OUT / "points_wide.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        sub = POINTS[:6]
        w.writerow(["Field", *[p[0] for p in sub]])
        w.writerow(["Description", *[p[1] for p in sub]])
        w.writerow(["Units", *[p[7] for p in sub]])
        w.writerow(["Signal", *[p[8] for p in sub]])


def draw_diagram() -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    fig, ax = plt.subplots(figsize=(14, 6), dpi=100)
    ax.set_xlim(0, 14)
    ax.set_ylim(0, 6)
    ax.axis("off")
    boxes = {
        "TK-101": (0.4, 2.6, "RAW WATER\nTANK\nTK-101"),
        "P-101": (2.4, 2.6, "FEED PUMP\nP-101"),
        "CF-101": (4.4, 2.6, "CARTRIDGE\nFILTER\nCF-101"),
        "P-201": (6.4, 2.6, "HP PUMP\nP-201"),
        "RO-1": (8.6, 2.6, "RO SKID\nRO-1"),
        "TK-201": (11.4, 3.9, "PERMEATE\nTANK\nTK-201"),
        "TK-301": (11.4, 0.9, "CONCENTRATE\nTANK\nTK-301"),
    }
    for _, (x, y, text) in boxes.items():
        ax.add_patch(FancyBboxPatch((x, y), 1.5, 1.1, boxstyle="round,pad=0.05", fc="white", ec="black", lw=1.5))
        ax.text(x + 0.75, y + 0.55, text, ha="center", va="center", fontsize=9, family="monospace")

    def arrow(a, b, label, dy_a=0.55, dy_b=0.55):
        xa, ya, _ = boxes[a]
        xb, yb, _ = boxes[b]
        ax.add_patch(FancyArrowPatch((xa + 1.55, ya + dy_a), (xb - 0.05, yb + dy_b),
                                     arrowstyle="-|>", mutation_scale=14, lw=1.4))
        ax.text((xa + 1.55 + xb) / 2, (ya + dy_a + yb + dy_b) / 2 + 0.12, label, ha="center", fontsize=7)

    arrow("TK-101", "P-101", "L-01")
    arrow("P-101", "CF-101", "L-02")
    arrow("CF-101", "P-201", "L-03")
    arrow("P-201", "RO-1", "L-04")
    arrow("RO-1", "TK-201", "L-05 PERMEATE", 0.8, 0.5)
    arrow("RO-1", "TK-301", "L-06 CONC.", 0.3, 0.6)
    tags = [("LT-101", 0.5, 4.2), ("TT-101", 1.3, 4.2), ("PT-101", 3.0, 4.2), ("AT-101", 3.6, 1.8),
            ("PDT-101", 4.8, 4.2), ("PT-201", 7.3, 4.2), ("CT-101", 8.4, 4.2), ("FT-201", 10.4, 4.6),
            ("CT-201", 10.4, 5.3), ("FT-301", 10.4, 1.1), ("LT-201", 12.0, 5.4)]
    for tag, x, y in tags:
        ax.add_patch(plt.Circle((x, y), 0.32, fc="white", ec="black", lw=1.1))
        ax.text(x, y, tag.replace("-", "\n"), ha="center", va="center", fontsize=6.5, family="monospace")
    ax.text(0.3, 5.7, "BRACKISH WATER RO TRAIN - PROCESS FLOW (SAMPLE)", fontsize=11, weight="bold")
    fig.savefig(OUT / "diagram.png", bbox_inches="tight")


def main() -> int:
    logging.getLogger("rdflib.term").setLevel(logging.ERROR)
    OUT.mkdir(parents=True, exist_ok=True)
    s = load_settings()
    vocab = VocabularyRegistry(s.profiles, s.cache_dir).get("watr")
    pg = build_model(vocab)
    (OUT / "model.ttl").write_bytes(pg.export_turtle())
    run = vocab.validate(pg.model)
    print(f"model.ttl: {len(pg.model)} triples; conforms={run.conforms}; "
          f"{sum(f.severity == 'Violation' for f in run.findings)} violations")
    for f in run.findings:
        if f.severity == "Violation":
            print("  ", f.focus, f.message[:140])
    write_csvs()
    draw_diagram()
    print("wrote", sorted(p.name for p in OUT.iterdir()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
