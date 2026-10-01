"""Build an initial model from confirmed source records.

Follows the BuildingMOTIF skill's point-list workflow (``point_labels.md``,
``building_models.md``) instead of asking a model to classify every row:

1. **Source pattern.** The model looks at a sample and says how to read a record: a regular
   expression with named groups and/or which metadata columns hold the equipment identifier,
   the point token (suffix or description) and units. Code applies it to every record and
   reports coverage; unmatched examples go back to the model (bounded retries).
2. **Mapping table.** Distinct tokens - not rows - are mapped to verified vocabulary terms,
   using the same term-search tools as the correction assistant ("the first job is mapping
   source tokens to verified terms"). Equipment is grouped by the kinds of points it carries,
   and each group is mapped to an equipment class.
3. **Build.** Operations are generated deterministically from the mapping tables, applied to
   a candidate and validated. Pattern-level validation failures go back to the model once, as
   the skill's "validate the representative, fix the reusable pattern" step.
4. **Unmapped stays unresolved.** Tokens the model could not map with a verified term are
   reported, never guessed: in Brick they become generic ``brick:Point``s with a review
   issue (so they appear in the tables to fix); in 223P/WaTr their records stay unmodeled.

The result is one reviewable build proposal whose points cite their source records.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import Counter, defaultdict
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import operations as ops_mod
from ..llm import CancelToken, LLMClient, LLMError
from ..project import Project
from ..schemas import EvidenceRef, Observation, SelectionScope
from .correction import TOOL_ACTIONS, _call_tool, _fmt_args, _fmt_result
from .guidance import SkillGuidance
from .tools import AgentTools, curie

Progress = Callable[[str, str, dict], None]

SAMPLE_SIZE = 60
BATCH = 30
MAX_TOOL_STEPS = 6
MIN_COVERAGE = 0.9


@dataclass
class Rec:
    obs: Observation
    name: str
    meta: dict[str, str]
    equip: str | None = None
    token: str | None = None
    units: str | None = None
    parsed: bool = True


@dataclass
class TokenGroup:
    id: str
    token: str
    units: str | None
    recs: list[Rec] = field(default_factory=list)
    mapping: dict[str, Any] | None = None


@dataclass
class EquipGroup:
    id: str
    equipment: list[str]
    tokens: list[str]  # token-group ids
    mapping: dict[str, Any] | None = None


@dataclass
class BuildOutcome:
    proposal: Any = None
    questions: list[str] = field(default_factory=list)
    explanation: str = ""
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


# ------------------------------------------------------------------- prompts

PARSE_SYSTEM = """\
You are setting up how to read a point list, following the BuildingMOTIF skill's point-label
workflow: identify the source pattern - the owning equipment identifier, the point token
(suffix, abbreviation or description that says what the point is) and units - so that code
can apply it to every record.

Each record has a name (the point label exactly as written in the source) and metadata
columns. Reply with one JSON object:
  pattern: a Python regular expression that must fullmatch the name, with named groups
           (for example "equipment" and "point"), or null if the columns are enough
  equipment_from: "group:<name>", "column:<column name>", or "none"
  token_from: "group:<name>", "column:<column name>", or "name"
  units_from: "column:<column name>" or "none"
  explanation: one or two sentences describing the convention
Rules:
- The equipment identifier is the source's own name for the equipment (e.g. "A1.RM1105",
  "TK-101"), not a type. Use "none" if the records do not say which equipment a point is on.
- The token is the part that repeats across equipment and says what kind of point it is
  (e.g. "DMPR COMD", "Zone Air Temp", "Feed pH"). A description column is a good token.
- The pattern must match (nearly) every name. Prefer simple patterns.
"""

MAP_COMMON = """
Rules (from the BuildingMOTIF skill):
- Verify every term. Use search_terms / units_for / describe_class before choosing; write terms
  as prefixed names. Never invent a term or rebuild one from a guessed local name.
- Treat starter mappings as examples, not universal truth; use units, I/O type and the
  description together. Prefer the most specific term the evidence supports.
- If a token is ambiguous (sensor vs setpoint, command vs status) or you cannot verify a term,
  leave its fields null: unmapped tokens are reported to the person, which is better than a
  wrong guess.

Each reply is one JSON object: a tool call {"action": "<tool>", "args": {...}} or the answer
{"action": "map", "mappings": [...], "explanation": "..."} with one mapping per id listed.

Tools:
"""

BRICK_POINTS = """\
You map point tokens from a building's BMS point list to Brick point classes. Each token id
(T1, T2, ...) stands for many records. For each, give point_type (a Brick point class such as
brick:Zone_Air_Temperature_Sensor, brick:Damper_Position_Command, brick:Valve_Command,
brick:Zone_Air_Temperature_Setpoint) and optionally a QUDT unit (unit:DEG_F) when the source
states one. Mapping item: {"id": "T1", "point_type": "brick:..." or null, "unit": "unit:..." or null}
"""

S223_POINTS = """\
You map point tokens from a point list to ASHRAE 223P{watr} point descriptions. Each token id
(T1, T2, ...) stands for many records. A point is a Property:
  point_kind: "measurement" (numeric, read-only), "setpoint" (numeric, commanded),
              "status" (on/off or mode, read-only), "command" (on/off or mode, commanded)
  quantity_kind: QUDT quantity kind for numeric points (quantitykind:Pressure, ...)
  unit: QUDT unit (unit:PSI, unit:DEG_C, ...), from the source's units where given
  sensor_type: a sensor class for measurements/status (s223:PressureSensor{watr_sensor})
  medium: what the measured fluid is, when the source says ({medium_eg})
  enumeration_kind: for status/command points (s223:Binary-OnOff)
Mapping item: {{"id": "T1", "point_kind": ..., "quantity_kind": ..., "unit": ..., "sensor_type": ...,
"medium": ..., "enumeration_kind": ...}} with null for anything unknown; point_kind null = unmapped.
"""

EQUIP_PROMPT = """\
You classify equipment for a {vocab} model. Each group id (G1, G2, ...) is equipment whose
source identifiers and points are listed; the points are what the equipment is instrumented
with. Give each group an equipment class ({examples}){process}, or null if the evidence does
not identify the kind of equipment.
Mapping item: {{"id": "G1", "type": "<class>" or null{process_field}}}
"""


def _nullable(t: str = "string") -> dict:
    return {"anyOf": [{"type": t}, {"type": "null"}]}


def parse_schema() -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {"thought": {"type": "string"}, "pattern": _nullable(),
                           "equipment_from": {"type": "string"}, "token_from": {"type": "string"},
                           "units_from": {"type": "string"}, "explanation": {"type": "string"}},
            "required": ["equipment_from", "token_from"]}


def map_schema(item_props: dict) -> dict:
    item = {"type": "object", "additionalProperties": False,
            "properties": {"id": {"type": "string"}, **{k: _nullable() for k in item_props}},
            "required": ["id"]}
    return {"type": "object", "additionalProperties": False, "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": [*TOOL_ACTIONS, "map"]},
        "args": {"type": "object", "additionalProperties": False, "properties": {
            k: {"type": "string"} for k in ("query", "kind", "term", "quantity_kind", "topic", "observation_id")}},
        "mappings": {"type": "array", "items": item},
        "explanation": {"type": "string"},
    }, "required": ["action"]}


POINT_FIELDS = {
    "brick": ["point_type", "unit"],
    "s223": ["point_kind", "quantity_kind", "unit", "sensor_type", "medium", "enumeration_kind"],
}
EQUIP_FIELDS = {"brick": ["type"], "s223": ["type", "process"]}


# ------------------------------------------------------------------ helpers

class Session:
    """One build run: LLM access, token accounting, progress, cancellation."""

    def __init__(self, project: Project, llm: LLMClient, guidance: SkillGuidance, rid: str,
                 progress: Progress, cancel: CancelToken):
        self.project, self.llm, self.guidance, self.rid = project, llm, guidance, rid
        self.vocab = project.vocab
        self._progress, self.cancel = progress, cancel
        self.tools = AgentTools(project, rid, guidance)
        self.outcome = BuildOutcome()
        self._lock = threading.Lock()  # batches run concurrently
        # Requests the provider can serve at once (a local llama-server matches its -np).
        self.workers = max(1, int(getattr(getattr(llm, "cfg", None), "concurrency", 1)))

    def progress(self, stage: str, message: str, data: dict) -> None:
        with self._lock:
            self._progress(stage, message, data)

    def ask(self, system: str, messages: list[dict], schema: dict) -> dict:
        self.cancel.check()
        res = self.llm.complete_json(system, messages, schema, cancel=self.cancel)
        with self._lock:
            self.outcome.steps += 1
            self.outcome.input_tokens += res.input_tokens
            self.outcome.output_tokens += res.output_tokens
        messages.append({"role": "assistant", "content": json.dumps(res.data)})
        return res.data

    def run_batches(self, jobs: list[Callable[[], None]]) -> None:
        """Independent mapping batches, up to ``workers`` at a time; the first failure stops the rest."""
        if self.workers == 1 or len(jobs) < 2:
            for job in jobs:
                job()
            return
        with ThreadPoolExecutor(max_workers=min(self.workers, len(jobs)), thread_name_prefix="build") as pool:
            futures = [pool.submit(job) for job in jobs]
            done, pending = wait(futures, return_when=FIRST_EXCEPTION)
            for future in pending:
                future.cancel()
            for future in futures:
                if future.done() and not future.cancelled() and future.exception():
                    raise future.exception()  # type: ignore[misc]

    def ask_with_tools(self, system: str, messages: list[dict], schema: dict, final: str) -> dict:
        for step in range(MAX_TOOL_STEPS + 1):
            if step == MAX_TOOL_STEPS:
                messages.append({"role": "user", "content": f'You must now answer with action "{final}".'})
            data = self.ask(system, messages, schema)
            action = data.get("action")
            if action == final:
                return data
            if action in TOOL_ACTIONS and step < MAX_TOOL_STEPS:
                args = data.get("args") or {}
                result = _call_tool(self.tools, action, args)
                self.progress("tool", f"{action}({_fmt_args(args)})", {"result_preview": str(result)[:200]})
                messages.append({"role": "user", "content": f"Result of {action}:\n{_fmt_result(result)}"})
            else:
                messages.append({"role": "user", "content": f'Reply with a tool call or action "{final}".'})
        raise LLMError(f"the model did not produce a {final} answer")


def _source_value(spec: str, rec: Rec, m: re.Match | None) -> str | None:
    kind, _, name = (spec or "none").partition(":")
    if kind == "column":
        v = rec.meta.get(name)
        return v.strip() if v and v.strip() else None
    if kind == "group":
        if m is None or name not in m.re.groupindex:
            return None
        v = m.group(name)
        return v.strip() if v and v.strip() else None
    if kind == "name":
        return rec.name
    return None


def apply_spec(spec: dict, recs: list[Rec]) -> tuple[float, list[str], str | None]:
    """Apply a parse spec to every record. Returns (coverage, failures, error)."""
    pattern = None
    if spec.get("pattern"):
        try:
            pattern = re.compile(spec["pattern"])
        except re.error as exc:
            return 0.0, [], f"the pattern is not a valid regular expression: {exc}"
    uses_groups = any(str(spec.get(k, "")).startswith("group:") for k in ("equipment_from", "token_from"))
    if uses_groups and pattern is None:
        return 0.0, [], "a group is referenced but no pattern was given"
    for key in ("equipment_from", "token_from", "units_from"):
        v = str(spec.get(key) or "none")
        if v.startswith("group:") and pattern is not None and v[6:] not in pattern.groupindex:
            return 0.0, [], f"{key} refers to group {v[6:]!r}, which the pattern does not define"
    failures: list[str] = []
    for r in recs:
        m = pattern.fullmatch(r.name) if pattern is not None else None
        r.parsed = m is not None if pattern is not None else not uses_groups
        r.equip = _source_value(spec.get("equipment_from", "none"), r, m) if r.parsed else None
        token = _source_value(spec.get("token_from", "name"), r, m) if r.parsed else None
        r.token = re.sub(r"\s+", " ", token).strip() if token else None
        r.units = _source_value(spec.get("units_from", "none"), r, m) if r.parsed else None
        if not r.parsed or not r.token:
            r.parsed = False
            failures.append(r.name)
    return (len(recs) - len(failures)) / max(len(recs), 1), failures, None


def _sample(recs: list[Rec], n: int) -> list[Rec]:
    ordered = sorted(recs, key=lambda r: r.name)
    if len(ordered) <= n:
        return ordered
    step = len(ordered) / n
    return [ordered[int(i * step)] for i in range(n)]


def _rec_line(r: Rec) -> str:
    meta = "; ".join(f"{k}={v}" for k, v in r.meta.items() if v)
    return f"{r.name}" + (f"  |  {meta}" if meta else "")


# ---------------------------------------------------------------- the run

def run_build(project: Project, llm: LLMClient, guidance: SkillGuidance, rid: str, source_ids: list[str],
              instruction: str, run_id: str | None, progress: Progress, cancel: CancelToken) -> BuildOutcome:
    s = Session(project, llm, guidance, rid, progress, cancel)
    vocab = project.vocab
    family = vocab.family

    # ---- records not yet in the model
    modeled = project.evidence_map(rid)
    sources = {sid: project.source(sid) for sid in source_ids}
    all_obs = [o for sid in source_ids for o in project.observations(sid)
               if o.status != "superseded" and o.kind == "point_record"]
    recs = [Rec(o, str(o.content.get("name", "")), dict(o.content.get("metadata") or {}))
            for o in all_obs if o.id not in modeled]
    skipped_existing = len(all_obs) - len(recs)
    if not recs:
        raise LLMError("every record from these sources is already in the model" if all_obs
                       else "no confirmed records: confirm the CSV structure in the Sources pane first")
    columns = sorted({k for r in recs for k in r.meta})
    progress("inventory", f"{len(recs)} records to model from {', '.join(x.filename for x in sources.values())}"
             + (f" ({skipped_existing} already modeled)" if skipped_existing else ""), {})

    # ---- 1. source pattern
    msgs = [{"role": "user", "content":
             f"Model vocabulary: {vocab.profile.label}\nMetadata columns: {columns or 'none'}\n"
             f"{len(recs)} records; a sample:\n" + "\n".join(_rec_line(r) for r in _sample(recs, SAMPLE_SIZE))
             + (f"\n\nThe person says: {instruction}" if instruction else "")}]
    spec: dict = {}
    coverage, failures = 0.0, []
    for attempt in range(3):
        progress("pattern", "Working out the naming convention" if attempt == 0 else "Refining the naming convention", {})
        spec = s.ask(PARSE_SYSTEM, msgs, parse_schema())
        coverage, failures, error = apply_spec(spec, recs)
        tokens = Counter(r.token for r in recs if r.parsed)
        progress("pattern", f"Pattern reads {coverage:.0%} of records ({len(tokens)} distinct tokens)",
                 {"pattern": spec.get("pattern"), "equipment_from": spec.get("equipment_from"),
                  "token_from": spec.get("token_from")})
        problems = []
        if error:
            problems.append(error)
        elif coverage < MIN_COVERAGE:
            problems.append(f"only {coverage:.0%} of names were read. Examples that failed:\n"
                            + "\n".join(failures[:25]))
        elif len(recs) > 60 and len(tokens) > 0.5 * len(recs):
            problems.append(f"{len(tokens)} distinct tokens for {len(recs)} records: the token should be "
                            "the part that repeats across equipment, not the whole name")
        if not problems:
            break
        msgs.append({"role": "user", "content": "That does not work yet: " + "; ".join(problems)
                     + "\nRevise the reply."})
    if coverage == 0:
        raise LLMError("could not work out how to read these records; check the CSV structure")
    parsed = [r for r in recs if r.parsed]

    # ---- 2. mapping table for point tokens
    groups: dict[tuple, TokenGroup] = {}
    for r in parsed:
        key = (r.token, r.units if family == "s223" or r.units else None)
        if key not in groups:
            groups[key] = TokenGroup(f"T{len(groups) + 1}", r.token or "", key[1])
        groups[key].recs.append(r)
    tgroups = sorted(groups.values(), key=lambda g: -len(g.recs))
    for i, g in enumerate(tgroups):
        g.id = f"T{i + 1}"
    watr = "watr" in vocab.namespaces
    if family == "brick":
        point_system = BRICK_POINTS + MAP_COMMON + AgentTools.catalog("brick")
        starter = guidance.topic("brick_point_mappings", 2500)
    else:
        point_system = S223_POINTS.format(
            watr=" / WaTr" if watr else "", watr_sensor=", watr:pHSensor" if watr else "",
            medium_eg="watr:Water-Brackish, s223:Fluid-Water" if watr else "s223:Fluid-Water, s223:Fluid-Air",
        ) + MAP_COMMON + AgentTools.catalog("s223")
        starter = guidance.topic("points", 2500)
    def map_points(b: int, batch: list[TokenGroup]) -> None:
        s.progress("mapping", f"Mapping point tokens {b + 1}-{b + len(batch)} of {len(tgroups)}", {})
        lines = []
        for g in batch:
            ex = "; ".join(r.name for r in g.recs[:3])
            meta = Counter(json.dumps(r.meta, sort_keys=True) for r in g.recs).most_common(1)[0][0]
            hint = ", ".join(curie(vocab, t.iri) for t in vocab.search(g.token, ["point_class"] if family == "brick"
                                                                       else ["quantity_kind", "sensor"], 3))
            lines.append(f"{g.id} | \"{g.token}\"{f' [{g.units}]' if g.units else ''} | {len(g.recs)} records | "
                         f"e.g. {ex}" + (f" | metadata: {meta}" if meta != "{}" else "")
                         + (f" | search hits: {hint}" if hint else ""))
        msgs = [{"role": "user", "content":
                 f"Skill guidance (starter mappings):\n{starter}\n\nTokens:\n" + "\n".join(lines)
                 + (f"\n\nThe person says: {instruction}" if instruction else "")}]
        _map_batch(s, point_system, msgs, {g.id: g for g in batch}, POINT_FIELDS[family], "point")

    s.run_batches([lambda b=b: map_points(b, tgroups[b:b + BATCH]) for b in range(0, len(tgroups), BATCH)])

    # ---- equipment groups (by the points each piece of equipment carries)
    equip_tokens: dict[str, set[str]] = defaultdict(set)
    for g in tgroups:
        for r in g.recs:
            if r.equip:
                equip_tokens[r.equip].add(g.id)
    existing_equipment = {e.label: e.id for e in project.view(rid).equipment}
    by_sig: dict[frozenset, list[str]] = defaultdict(list)
    for eq, toks in equip_tokens.items():
        if eq not in existing_equipment:
            by_sig[frozenset(toks)].append(eq)
    egroups = [EquipGroup(f"G{i + 1}", sorted(eqs), sorted(sig)) for i, (sig, eqs) in
               enumerate(sorted(by_sig.items(), key=lambda kv: -len(kv[1])))]
    tg_by_id = {g.id: g for g in tgroups}

    def describe_points(ids: list[str]) -> str:
        parts = []
        for tid in ids:
            g = tg_by_id[tid]
            term = (g.mapping or {}).get("point_type") or (g.mapping or {}).get("sensor_type") \
                or (g.mapping or {}).get("quantity_kind")
            parts.append(f"\"{g.token}\"" + (f" ({vocab.label(term)})" if term else ""))
        return ", ".join(parts)

    if egroups:
        eq_system = EQUIP_PROMPT.format(
            vocab=vocab.profile.label,
            examples="brick:AHU, brick:Variable_Air_Volume_Box_With_Reheat, brick:Pump" if family == "brick"
            else ("watr:Tank, watr:Pump, watr:ReverseOsmosisMembrane" if watr else "s223:Pump, s223:Fan, s223:Damper"),
            process=", and for treatment equipment its process (watr:Process-*)" if watr else "",
            process_field=', "process": "watr:Process-..." or null' if watr else "",
        ) + MAP_COMMON + AgentTools.catalog(family)
        def map_equipment(b: int, batch: list[EquipGroup]) -> None:
            s.progress("mapping", f"Classifying equipment groups {b + 1}-{b + len(batch)} of {len(egroups)}", {})
            lines = [f"{g.id} | {len(g.equipment)} equipment, e.g. {', '.join(g.equipment[:4])} | "
                     f"points: {describe_points(g.tokens)}" for g in batch]
            msgs = [{"role": "user", "content": "Equipment groups:\n" + "\n".join(lines)
                     + (f"\n\nThe person says: {instruction}" if instruction else "")}]
            _map_batch(s, eq_system, msgs, {g.id: g for g in batch}, EQUIP_FIELDS[family] if watr or family == "brick"
                       else ["type"], "equipment")

        s.run_batches([lambda b=b: map_equipment(b, egroups[b:b + BATCH]) for b in range(0, len(egroups), BATCH)])

    # ---- 3. build, validate, one pattern-level refinement round
    before = project.revision(rid)
    before_issues = project.issues(rid)
    for round_ in range(2):
        ops, plan = _build_ops(project, rid, tgroups, egroups, existing_equipment)
        progress("candidate", f"Building a candidate model with {len(ops)} operations", {})
        cand = project.build_candidate(rid, ops_mod.OperationList.validate_python(ops), SelectionScope(), lock=False)
        progress("validated", f"Validated: {cand.summary.violations} problem(s)", {"conforms": cand.summary.conforms})
        if round_ == 1:
            break
        failing = _pattern_failures(cand, plan, tgroups, egroups)
        if not failing:
            break
        progress("refine", f"{len(failing)} mapping pattern(s) fail validation; asking the model to revise", {})
        _refine(s, family, failing, tg_by_id, {g.id: g for g in egroups}, point_system if any(
            k.startswith("T") for k in failing) else None, instruction)

    # ---- 4. proposal with a build summary
    summary, followups = _summarize(project, cand, sources, recs, parsed, failures, spec, coverage,
                                    tgroups, egroups, skipped_existing, plan)
    evidence = [EvidenceRef(kind="observation", ref=sid, summary=f"{src.filename}: {len(recs)} records")
                for sid, src in sources.items()]
    evidence.append(EvidenceRef(kind="guidance", ref="point_labels",
                                summary=f"BuildingMOTIF skill {guidance.version}: point-list workflow"))
    explanation = (f"Read the records with {summary['parse']['description']} and mapped "
                   f"{summary['mapped_tokens']} of {len(tgroups)} distinct point tokens to verified terms.")
    s.outcome.explanation = explanation
    s.outcome.proposal = project.save_proposal(
        cand, SelectionScope(), instruction or "Build the model from the uploaded records", explanation, evidence,
        [], run_id, before.validation, before_issues,  # type: ignore[arg-type]
        kind="build", build_summary=summary, followup_issues=followups)
    return s.outcome


def _map_batch(s: Session, system: str, msgs: list[dict], items: dict[str, Any], fields: list[str], what: str) -> None:
    """Ask for mappings, verify every term, and give one repair round with suggestions."""
    from .correction import suggest_terms

    schema = map_schema({f: None for f in fields})
    vocab = s.vocab
    kinds = {"point_type": "point_class", "unit": "unit", "quantity_kind": "quantity_kind", "sensor_type": "sensor",
             "medium": "medium", "enumeration_kind": "enumeration", "process": "process", "type": "equipment"}
    pending = dict(items)
    for attempt in range(2):
        data = s.ask_with_tools(system, msgs, schema, "map")
        problems: list[str] = []
        unknown: list[tuple[str, str, str | None]] = []
        for m in data.get("mappings") or []:
            item = pending.get(str(m.get("id")))
            if item is None:
                continue
            clean: dict[str, Any] = {}
            ok = True
            for f in fields:
                v = m.get(f)
                if v in (None, "", "null"):
                    continue
                if f == "point_kind":
                    if v not in ("measurement", "setpoint", "status", "command"):
                        problems.append(f"{item.id}: point_kind {v!r} is not one of measurement/setpoint/status/command")
                        ok = False
                    clean[f] = v
                    continue
                iri = ops_mod.expand_term(vocab, str(v))
                t = vocab.term(iri)
                if t is None or t.kind != kinds[f] or t.abstract:
                    problems.append(f"{item.id}: {f} {v!r} is not a valid {kinds[f].replace('_', ' ')} term")
                    unknown.append((f, iri, kinds[f]))
                    ok = False
                    continue
                clean[f] = iri
            required = "point_type" if s.vocab.family == "brick" and what == "point" else (
                "point_kind" if what == "point" else "type")
            if ok and clean.get(required):
                item.mapping = clean
                pending.pop(item.id)
            elif ok:
                pending.pop(item.id)  # deliberately left unmapped
        if not problems or attempt == 1:
            break
        msgs.append({"role": "user", "content":
                     "These mappings use terms that are not in the vocabulary:\n- " + "\n- ".join(problems)
                     + ("\n- " + "\n- ".join(suggest_terms(s.project, unknown)) if unknown else "")
                     + "\nAnswer again for just these ids, verifying terms with search_terms first."})
    mapped = sum(1 for i in items.values() if i.mapping)
    s.progress("mapping", f"Mapped {mapped} of {len(items)} {what} {'tokens' if what == 'point' else 'groups'}", {})


def _build_ops(project: Project, rid: str, tgroups: list[TokenGroup], egroups: list[EquipGroup],
               existing_equipment: dict[str, str]) -> tuple[list[dict], dict]:
    vocab = project.vocab
    family = vocab.family
    ops: list[dict] = []
    equip_ref: dict[str, str] = dict(existing_equipment)
    plan: dict[str, Any] = {"equipment": {}, "points": {}}
    for g in egroups:
        for eq in g.equipment:
            etype = (g.mapping or {}).get("type")
            if etype is None:
                fallback = "https://brickschema.org/schema/Brick#Equipment" if family == "brick" \
                    else "http://data.ashrae.org/standard223#Equipment"
                t = vocab.term(fallback)
                if t is None or t.abstract:
                    continue
                etype = fallback
            ref = f"new:e{len(equip_ref)}"
            op = {"op": "create_equipment", "id": ref, "label": eq, "type": etype}
            if (g.mapping or {}).get("process"):
                op["process"] = g.mapping["process"]  # type: ignore[index]
            ops.append(op)
            equip_ref[eq] = ref
            plan["equipment"][ref] = g.id
    for g in tgroups:
        for r in g.recs:
            m = g.mapping
            if m is None and family != "brick":
                continue  # 223P points need a kind; leave the record unresolved
            ref = f"new:p{len(plan['points'])}"
            op: dict[str, Any] = {"op": "create_point", "id": ref, "label": r.name, "evidence": [r.obs.id]}
            if r.equip and r.equip in equip_ref:
                op["equipment"] = equip_ref[r.equip]
            if family == "brick":
                op["point_type"] = (m or {}).get("point_type") or "https://brickschema.org/schema/Brick#Point"
                if m and m.get("unit"):
                    op["unit"] = m["unit"]
            else:
                op.update({k: v for k, v in (m or {}).items() if v})
            ops.append(op)
            plan["points"][ref] = g.id
    return ops, plan


def _entity_groups(cand, plan: dict) -> dict[str, str]:
    """Map created entity ids back to their token/equipment group ids."""
    created = [op.id for op in cand.ops if op.op.startswith("create_")]
    refs = [*plan["equipment"].keys(), *plan["points"].keys()]
    groups = {**plan["equipment"], **plan["points"]}
    return {eid: groups[ref] for eid, ref in zip(created, refs)}


def _pattern_failures(cand, plan: dict, tgroups: list[TokenGroup], egroups: list[EquipGroup]) -> dict[str, list[str]]:
    """Validation problems that hit most entities of a mapped group: a mapping-level problem."""
    ent_group = _entity_groups(cand, plan)
    sizes = Counter(ent_group.values())
    hits: dict[str, Counter] = defaultdict(Counter)
    for issue in cand.issues:
        if issue.severity != "violation":
            continue
        for eid in issue.affected_ids:
            gid = ent_group.get(eid)
            if gid:
                msg = re.sub(r"^[^:]+: ", "", issue.explanation)[:200]
                hits[gid][msg] += 1
    mapped = {g.id for g in tgroups if g.mapping} | {g.id for g in egroups if g.mapping}
    out = {}
    for gid, msgs in hits.items():
        top = [m for m, n in msgs.most_common(3) if n >= max(1, sizes[gid] // 2)]
        if top and gid in mapped:
            out[gid] = top
    return out


def _refine(s: Session, family: str, failing: dict[str, list[str]], tg: dict[str, TokenGroup],
            eg: dict[str, EquipGroup], point_system: str | None, instruction: str) -> None:
    pts = {k: v for k, v in failing.items() if k in tg}
    if pts and point_system:
        lines = [f"{k} | \"{tg[k].token}\" mapped to {json.dumps(tg[k].mapping)} | validation: {'; '.join(v)}"
                 for k, v in pts.items()]
        msgs = [{"role": "user", "content":
                 "Validating the model built from your mapping found these problems for every record of a token.\n"
                 "If a different verified term (or an added unit/quantity kind) fixes the problem, give the revised "
                 "mapping; if the problem needs information the source does not have, keep the mapping as it was.\n"
                 + "\n".join(lines)}]
        items = {k: tg[k] for k in pts}
        saved = {k: dict(g.mapping or {}) for k, g in items.items()}
        _map_batch(s, point_system, msgs, items, POINT_FIELDS[family], "point")
        for k, g in items.items():  # a revision that dropped the mapping keeps the original
            if not g.mapping:
                g.mapping = saved[k]


def _summarize(project: Project, cand, sources, recs, parsed, failures, spec, coverage, tgroups, egroups,
               skipped_existing: int, plan: dict) -> tuple[dict, list[dict]]:
    vocab = project.vocab
    ent_group = _entity_groups(cand, plan)
    by_group: dict[str, list[str]] = defaultdict(list)
    for eid, gid in ent_group.items():
        by_group[gid].append(eid)
    desc_parts = []
    if spec.get("pattern"):
        desc_parts.append(f"the pattern {spec['pattern']}")
    for k in ("equipment_from", "token_from", "units_from"):
        v = str(spec.get(k) or "none")
        if v.startswith("column:"):
            desc_parts.append(f"{k.split('_')[0]} from column \"{v[7:]}\"")
    points_created = len(plan["points"])
    equipment_created = len(plan["equipment"])
    term_key = "point_type" if vocab.family == "brick" else "point_kind"
    point_rows = []
    for g in tgroups:
        m = g.mapping or {}
        main = m.get("point_type") or m.get("sensor_type") or m.get("quantity_kind")
        point_rows.append({
            "id": g.id, "token": g.token, "units": g.units, "count": len(g.recs),
            "examples": [r.name for r in g.recs[:3]], "mapped": bool(m.get(term_key)),
            "term": curie(vocab, main) if main else None, "term_label": vocab.label(main) if main else None,
            "point_kind": m.get("point_kind"),
            "unit": vocab.label(m["unit"]) if m.get("unit") else None,
        })
    equip_rows = [{
        "id": g.id, "count": len(g.equipment), "examples": g.equipment[:4],
        "points": [tg.token for tg in tgroups if tg.id in g.tokens][:8],
        "mapped": bool((g.mapping or {}).get("type")),
        "term": curie(vocab, g.mapping["type"]) if g.mapping and g.mapping.get("type") else None,
        "term_label": vocab.label(g.mapping["type"]) if g.mapping and g.mapping.get("type") else None,
        "process": vocab.label(g.mapping["process"]) if g.mapping and g.mapping.get("process") else None,
    } for g in egroups]
    names = ", ".join(x.filename for x in sources.values())
    followups: list[dict] = []
    for g in tgroups:
        if g.mapping and g.mapping.get(term_key):
            continue
        ids = by_group.get(g.id, [])
        key = hashlib.sha1(f"{g.token}|{g.units}|{names}".encode()).hexdigest()[:10]
        if ids:
            text = (f"{len(ids)} point(s) with \"{g.token}\" (e.g. {g.recs[0].name}) have no specific type yet: "
                    "select them and tell the assistant what they are.")
        else:
            text = (f"{len(g.recs)} record(s) with \"{g.token}\" from {names} (e.g. {g.recs[0].name}) were not "
                    "modeled: the kind of point could not be determined.")
        followups.append({"id": f"build-{key}", "affected_ids": ids, "category": "unresolved_extraction",
                          "severity": "warning", "explanation": text, "resolution_state": "open",
                          "origin": "extraction", "details": {"token": g.token, "records": [r.obs.id for r in g.recs]}})
    for g in egroups:
        if g.mapping and g.mapping.get("type"):
            continue
        ids = by_group.get(g.id, [])
        key = hashlib.sha1(f"eq|{'|'.join(g.equipment[:5])}".encode()).hexdigest()[:10]
        followups.append({"id": f"build-{key}", "affected_ids": ids, "category": "unresolved_extraction",
                          "severity": "warning", "resolution_state": "open", "origin": "extraction",
                          "explanation": f"{len(g.equipment)} equipment (e.g. {', '.join(g.equipment[:3])}) "
                                         "could not be classified; they use a generic equipment type.",
                          "details": {}})
    unparsed = [r for r in recs if not r.parsed]
    if unparsed:
        followups.append({"id": f"build-unparsed-{hashlib.sha1(names.encode()).hexdigest()[:8]}",
                          "affected_ids": [], "category": "unresolved_extraction", "severity": "warning",
                          "resolution_state": "open", "origin": "extraction",
                          "explanation": f"{len(unparsed)} record(s) from {names} did not match the naming convention "
                                         f"and were not modeled (e.g. {', '.join(r.name for r in unparsed[:3])}).",
                          "details": {"records": [r.obs.id for r in unparsed]}})
    summary = {
        "title": f"Built {equipment_created} equipment and {points_created} points from {names}",
        "sources": [{"id": sid, "filename": src.filename} for sid, src in sources.items()],
        "records": len(recs) + skipped_existing, "already_modeled": skipped_existing,
        "parse": {"pattern": spec.get("pattern"), "equipment_from": spec.get("equipment_from"),
                  "token_from": spec.get("token_from"), "units_from": spec.get("units_from"),
                  "description": ", ".join(desc_parts) or "the point names", "coverage": round(coverage, 3),
                  "unparsed_examples": [r.name for r in unparsed[:10]], "explanation": spec.get("explanation", "")},
        "points_created": points_created, "equipment_created": equipment_created,
        "mapped_tokens": sum(1 for r in point_rows if r["mapped"]), "token_count": len(tgroups),
        "point_mappings": point_rows, "equipment_mappings": equip_rows,
        "unmapped_records": sum(r["count"] for r in point_rows if not r["mapped"]),
    }
    return summary, followups
