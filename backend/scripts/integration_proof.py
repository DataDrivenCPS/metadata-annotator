"""Integration proof: BuildingMOTIF (gtf-buildingmotif) + WaTr-on-223P.

Loads 223P and then WaTr from the sources the BuildingMOTIF skill prescribes (223P first);
OntoEnv fetches their imports.

Follows the skill's "one durable script" guidance: configuration, ontology loading,
namespace-preserving term checks, a representative build, validation, and export.

    uv run python scripts/integration_proof.py [--out build/proof.ttl]
"""

from __future__ import annotations

import argparse
import logging
import tempfile
import time
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

import buildingmotif
from buildingmotif import BuildingMOTIF
from buildingmotif.dataclasses import Library, Model

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ["https://open223.info/223p.ttl", "https://watermetadata.org/watr-0.2.ttl"]

S223 = Namespace("http://data.ashrae.org/standard223#")
QUDT = Namespace("http://qudt.org/schema/qudt/")
QK = Namespace("http://qudt.org/vocab/quantitykind/")
UNIT = Namespace("http://qudt.org/vocab/unit/")
BLDG = Namespace("urn:proof/")


def watr_namespace(graph: Graph) -> Namespace:
    """Read the WaTr namespace from the loaded ontology instead of hardcoding it."""
    for prefix, ns in graph.namespaces():
        if prefix == "watr":
            return Namespace(str(ns))
    raise RuntimeError("watr prefix not declared in loaded ontology")


def verify(graph: Graph, *terms: URIRef) -> None:
    missing = [t for t in terms if (t, None, None) not in graph]
    if missing:
        raise RuntimeError(f"unverified terms: {missing}")


def build(g: Graph, WATR: Namespace) -> None:
    def cp(owner: URIRef, name: str, kind: URIRef, medium: URIRef) -> URIRef:
        node = BLDG[f"{owner.split('/')[-1]}.{name}"]
        g.add((node, RDF.type, kind))
        g.add((node, S223.hasMedium, medium))
        g.add((owner, S223.hasConnectionPoint, node))
        return node

    def pipe(name: str, a: URIRef, b: URIRef) -> None:
        p = BLDG[name]
        g.add((p, RDF.type, S223.Pipe))
        g.add((p, S223.hasMedium, S223["Fluid-Water"]))
        g.add((p, S223.cnx, a))
        g.add((p, S223.cnx, b))

    water = S223["Fluid-Water"]
    tank, pump, ro = BLDG["FeedTank"], BLDG["P-101"], BLDG["RO-1"]
    for node, cls, label in (
        (tank, WATR.Tank, "Feed Tank"),
        (pump, WATR.Pump, "P-101"),
        (ro, WATR.ReverseOsmosisMembrane, "RO-1"),
    ):
        g.add((node, RDF.type, cls))
        g.add((node, RDFS.label, Literal(label)))
    g.add((ro, WATR.hasProcess, WATR["Process-ReverseOsmosis"]))

    tank_in = cp(tank, "in", S223.InletConnectionPoint, water)
    tank_out = cp(tank, "out", S223.OutletConnectionPoint, water)
    pump_in = cp(pump, "in", S223.InletConnectionPoint, water)
    pump_out = cp(pump, "out", S223.OutletConnectionPoint, water)
    ro_in = cp(ro, "feed", S223.InletConnectionPoint, water)
    cp(ro, "permeate", S223.OutletConnectionPoint, water)
    cp(ro, "concentrate", S223.OutletConnectionPoint, water)
    _ = tank_in
    pipe("pipe-tank-pump", tank_out, pump_in)
    pipe("pipe-pump-ro", pump_out, ro_in)

    # A pressure measurement at the pump discharge, per the 223P sensor pattern.
    prop, sensor = BLDG["PT-101.value"], BLDG["PT-101"]
    g.add((prop, RDF.type, S223.QuantifiableObservableProperty))
    g.add((prop, QUDT.hasQuantityKind, QK.Pressure))
    g.add((prop, QUDT.hasUnit, UNIT.PSI))
    g.add((prop, RDFS.label, Literal("PT-101 discharge pressure")))
    g.add((sensor, RDF.type, S223.PressureSensor))
    g.add((sensor, S223.observes, prop))
    g.add((sensor, S223.hasObservationLocation, pump_out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "build" / "proof.ttl"))
    args = ap.parse_args()

    logging.getLogger("rdflib.term").setLevel(logging.ERROR)  # QUDT rdf:HTML literals
    print("buildingmotif:", buildingmotif.__file__)
    work = Path(tempfile.mkdtemp(prefix="wb-proof-"))
    t0 = time.perf_counter()
    with BuildingMOTIF(
        f"sqlite:///{(work / 'bm.db').as_posix()}",
        ontology_cache_path=work / "ontoenv",
        graph_store_path=work / "graphs",
    ) as bm:
        for source in SOURCES:
            watr_lib = Library.from_ontology(source, run_shacl_inference=False, infer_templates=False)
        t1 = time.perf_counter()
        print(f"loaded WaTr library in {t1 - t0:.1f}s")
        closure, names = bm.ontology_environment.closure_copy(watr_lib.name)
        print(f"import closure: {len(names)} graphs, {len(closure)} triples")

        WATR = watr_namespace(watr_lib.get_shape_collection().graph)
        print("WaTr namespace:", WATR)
        verify(
            closure,
            WATR.Tank, WATR.Pump, WATR.ReverseOsmosisMembrane,
            WATR["Process-ReverseOsmosis"], S223.Pipe, S223.PressureSensor,
            S223.QuantifiableObservableProperty, QK.Pressure, UNIT.PSI,
        )

        model = Model.create(BLDG, description="integration proof")
        g = Graph()
        build(g, WATR)
        model.add_graph(g)

        t2 = time.perf_counter()
        ctx = model.validate([watr_lib.get_shape_collection()])
        t3 = time.perf_counter()
        print(f"validated in {t3 - t2:.1f}s: valid={ctx.valid}")
        for focus, failures in ctx.diffset.items():
            for f in failures:
                print(f"  {focus}: {f.reason()}")

        # Negative check: an RO membrane without its process must fail.
        model.graph.remove((BLDG["RO-1"], WATR.hasProcess, None))
        ctx2 = model.validate([watr_lib.get_shape_collection()])
        print("without hasProcess: valid =", ctx2.valid)
        for w in ctx2.witnesses[:3]:
            print("  ", w.reason())
            for p in w.proposals(limit=3):
                print("     proposal", p.origin, "progress=", p.is_progress,
                      sorted(map(str, p.additions)))
        model.graph.add((BLDG["RO-1"], WATR.hasProcess, WATR["Process-ReverseOsmosis"]))

        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        model.graph.serialize(destination=out, format="turtle")
        print(f"exported {len(model.graph)} triples to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
