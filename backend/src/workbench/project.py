"""The project service: the only component that publishes model revisions.

Every change - a direct cell edit, an accepted agent proposal, an import - goes through
``build_candidate`` (resolve operations, apply them to a copy of the base revision,
validate) and ``publish`` (check the base is still current, write the snapshot, record the
revision and move the head, all under the project lock). Agents never touch the active
graph; they can only produce candidates.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import OWL, RDF

from . import operations as ops_mod
from .events import EventBus
from .graph import ProjectGraph, model_shell
from .issues import group_findings, issues_from_validation, recount, render_iri, render_repair, summarize
from .operations import ApplyResult, OperationError, OperationList
from .projection import ModelView, ensure_ids, project
from .schemas import (
    ChangeProposal,
    CsvImportConfig,
    Observation,
    Source,
    EntityChange,
    FieldChange,
    ReviewIssue,
    Revision,
    SelectionScope,
    TripleDiff,
    ValidationDelta,
    ValidationSummary,
    now,
)
from . import sources as src_mod
from .store import ProjectStore
from .vocabulary import ValidationRun, Vocabulary, VocabularyRegistry

log = logging.getLogger(__name__)


class StaleRevision(Exception):
    def __init__(self, base: str, head: str):
        super().__init__(f"based on {base}, but the current revision is {head}")
        self.base, self.head = base, head


class ProposalMismatch(Exception):
    pass


@dataclass
class Candidate:
    base: str
    ops: list
    before: ProjectGraph
    after: ProjectGraph
    result: ApplyResult
    run: ValidationRun
    issues: list[ReviewIssue]
    summary: ValidationSummary
    changes: list[EntityChange]
    diff: TripleDiff


# Displayed fields per entity kind: (field id, getter producing a display value)
DISPLAY_FIELDS = {
    "equipment": [
        ("label", lambda r: r.label),
        ("type", lambda r: r.type.label if r.type else None),
        ("process", lambda r: r.process.label if r.process else None),
        ("contained_in", lambda r: r.contained_in.label if r.contained_in else None),
        ("location", lambda r: r.location.label if r.location else None),
    ],
    "space": [
        ("label", lambda r: r.label),
        ("type", lambda r: r.type.label if r.type else None),
        ("part_of", lambda r: r.part_of.label if r.part_of else None),
    ],
    "point": [
        ("label", lambda r: r.label),
        ("point_kind", lambda r: r.point_kind_label),
        ("point_type", lambda r: r.point_type.label if r.point_type else None),
        ("quantity_kind", lambda r: r.quantity_kind.label if r.quantity_kind else None),
        ("unit", lambda r: r.unit.label if r.unit else None),
        ("equipment", lambda r: r.equipment.label if r.equipment else None),
        ("medium", lambda r: r.medium.label if r.medium else None),
        ("substance", lambda r: r.substance.label if r.substance else None),
        ("sensor_type", lambda r: r.sensor_type.label if r.sensor_type else None),
    ],
    "connection": [
        ("label", lambda r: r.label),
        ("from_equipment", lambda r: r.from_equipment.label if r.from_equipment else None),
        ("to_equipment", lambda r: r.to_equipment.label if r.to_equipment else None),
        ("medium", lambda r: r.medium.label if r.medium else None),
        ("type", lambda r: r.type.label if r.type else None),
        ("from_point", lambda r: r.from_point.label if r.from_point else None),
        ("to_point", lambda r: r.to_point.label if r.to_point else None),
    ],
    "connection_point": [
        ("label", lambda r: r.label),
        ("equipment", lambda r: r.equipment.label if r.equipment else None),
        ("direction", lambda r: r.direction),
        ("medium", lambda r: r.medium.label if r.medium else None),
        ("connection", lambda r: r.connection.label if r.connection else None),
        ("paired_with", lambda r: r.paired_with.label if r.paired_with else None),
        ("maps_to", lambda r: r.maps_to.label if r.maps_to else None),
    ],
}


def compute_changes(before: ModelView, after: ModelView, selected: set[str],
                    before_pg: ProjectGraph) -> list[EntityChange]:
    """Every entity whose projection differs, whether or not the operations named it."""
    b, a = before.rows(), after.rows()
    out: list[EntityChange] = []
    changed_connections: set[str] = set()
    for eid in sorted(set(b) | set(a), key=lambda i: i.startswith("cp-")):  # connections first
        rb, ra = b.get(eid), a.get(eid)
        row = ra or rb
        kind = row.kind  # type: ignore[union-attr]
        # A port minted for a connection (<connection>.in/.out) is created, deleted and renamed
        # as part of that connection's change; only its own facts (pairing, mapsTo...) are shown.
        minted = kind == "connection_point" and row.iri.rsplit(".", 1)[0] in changed_connections  # type: ignore[union-attr]
        if minted and (rb is None or ra is None):
            continue
        getters = [(f, g) for f, g in DISPLAY_FIELDS[kind] if not (minted and f in ("label", "connection"))]
        if rb is None:
            change = "created"
            fields = [FieldChange(field=f, after=g(ra)) for f, g in getters if g(ra) not in (None, "")]
        elif ra is None:
            change = "deleted"
            fields = [FieldChange(field=f, before=g(rb)) for f, g in getters if g(rb) not in (None, "")]
        else:
            fields = [FieldChange(field=f, before=g(rb), after=g(ra)) for f, g in getters if g(rb) != g(ra)]
            if not fields:
                continue
            change = "updated"
        if kind == "connection":
            changed_connections.add(row.iri)  # type: ignore[union-attr]
        locked = set(rb.locked) if rb else set()  # type: ignore[union-attr]
        out.append(EntityChange(
            entity_id=eid, entity_kind=kind, label=row.label, change=change,  # type: ignore[arg-type,union-attr]
            fields=fields, in_selection=(not selected) or eid in selected or _part_of_selection(row, selected),
            overrides_locked=sorted(locked & {f.field for f in fields}) if change == "updated" else [],
        ))
    return sorted(out, key=lambda c: c.entity_id)


def _part_of_selection(row, selected: set[str]) -> bool:
    """A selected equipment's own connection points are part of what was selected."""
    owner = getattr(row, "equipment", None) if getattr(row, "kind", None) == "connection_point" else None
    return owner is not None and owner.id in selected


def triple_diff(before: Graph, after: Graph) -> TripleDiff:
    def nt(g: Graph) -> set[str]:
        return {f"{s.n3()} {p.n3()} {o.n3()} ." for s, p, o in g}
    b, a = nt(before), nt(after)
    return TripleDiff(added=sorted(a - b), removed=sorted(b - a))


def describe_changes(changes: list[EntityChange]) -> str:
    if not changes:
        return "No visible change"
    parts = []
    for c in changes[:3]:
        if c.change == "updated":
            parts.append(f"{c.label}: {', '.join(f.field.replace('_', ' ') for f in c.fields)}")
        else:
            parts.append(f"{c.change} {c.label}")
    more = f" (+{len(changes) - 3} more)" if len(changes) > 3 else ""
    return "; ".join(parts) + more


class Project:
    def __init__(self, root: Path, registry: VocabularyRegistry, bus: EventBus):
        self.root = root
        self.store = ProjectStore(root)
        # Projects created before profiles existed are WaTr projects.
        self.profile: str = self.store.get_meta("profile") or "watr"
        self.vocab: Vocabulary = registry.get(self.profile)
        self.bus = bus
        self.lock = threading.RLock()
        self.id: str = self.store.get_meta("id")
        self.name: str = self.store.get_meta("name")
        self.namespace: str = self.store.get_meta("namespace")
        self._graph_cache: dict[str, ProjectGraph] = {}
        self._view_cache: dict[str, ModelView] = {}
        self._repair_cache: dict[str, dict[str, dict]] = {}
        self._repair_sessions: dict[str, object] = {}
        self._migrate_issue_states()

    # ------------------------------------------------------------- creation

    @classmethod
    def create(cls, root: Path, registry: VocabularyRegistry, bus: EventBus, project_id: str, name: str,
               profile: str) -> "Project":
        vocab = registry.get(profile)
        store = ProjectStore(root)
        namespace = f"urn:workbench:{project_id}/"
        with store.tx() as db:
            store.set_meta("id", project_id, db)
            store.set_meta("name", name, db)
            store.set_meta("namespace", namespace, db)
            store.set_meta("created_at", now(), db)
            store.set_meta("vocabulary", vocab.root_ontology, db)
            store.set_meta("profile", profile, db)
        p = cls(root, registry, bus)
        pg = model_shell(namespace, vocab.root_ontology, name)
        p._publish_graph(pg, parent=None, author="system", kind="initial",
                         summary="Empty model", operations=[])
        return p

    # ------------------------------------------------------------- reading

    def head(self) -> str:
        return self.store.get_meta("head")

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "namespace": self.namespace, "head": self.head(),
                "profile": self.profile, "profile_label": self.vocab.profile.label, "family": self.vocab.family,
                "can_undo": self._undo_target() is not None,
                "can_redo": bool(self.store.get_meta("redo", [])),
                "updated_at": self.revision(self.head()).created_at}

    def revision(self, rid: str) -> Revision:
        with self.store.tx() as db:
            row = db.execute("SELECT * FROM revisions WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise KeyError(f"no revision {rid}")
        return self._row_to_revision(row)

    def _row_to_revision(self, row) -> Revision:
        validation = ValidationSummary.model_validate_json(row["validation"]) if row["validation"] else None
        if validation is not None:
            validation = recount(json.loads(row["issues"] or "[]"), validation)
        return Revision(
            id=row["id"], parent_id=row["parent_id"], created_at=row["created_at"],
            author=row["author"], kind=row["kind"], summary=row["summary"],
            proposal_id=row["proposal_id"],
            operations=OperationList.validate_json(row["operations"]),
            validation=validation,
        )

    def revisions(self) -> list[Revision]:
        with self.store.tx() as db:
            rows = db.execute("SELECT * FROM revisions ORDER BY seq DESC").fetchall()
        return [self._row_to_revision(r) for r in rows]

    def graph(self, rid: str) -> ProjectGraph:
        """Cached, shared instance: callers that mutate must use ``.copy()``."""
        pg = self._graph_cache.get(rid)
        if pg is None:
            pg = ProjectGraph.from_trig(self.namespace, self.store.read_snapshot(rid))
            if len(self._graph_cache) > 16:
                self._graph_cache.pop(next(iter(self._graph_cache)))
            self._graph_cache[rid] = pg
        return pg

    def view(self, rid: str) -> ModelView:
        v = self._view_cache.get(rid)
        if v is None:
            v = project(self.graph(rid), self.vocab)
            if len(self._view_cache) > 16:
                self._view_cache.pop(next(iter(self._view_cache)))
            self._view_cache[rid] = v
        return v

    def _stored_issues(self, db, rid: str) -> list[ReviewIssue]:
        """A revision's validation issues, rebuilt from its stored findings so every revision
        uses the current issue ids and wording."""
        row = db.execute("SELECT issues FROM revisions WHERE id=?", (rid,)).fetchone()
        stored = [ReviewIssue.model_validate(i) for i in json.loads(row["issues"] or "[]")]
        findings = [(i.affected_ids[0] if i.affected_ids else None, d)
                    for i in stored for d in i.details.get("findings", [])]
        return group_findings(self.graph(rid), self.vocab, findings)

    def issues(self, rid: str) -> list[ReviewIssue]:
        with self.store.tx() as db:
            validation = self._stored_issues(db, rid)
            parents = {r["id"]: r["parent_id"] for r in db.execute("SELECT id, parent_id FROM revisions")}
            dismissals = {r["issue_id"]: dict(r) for r in db.execute("SELECT * FROM issue_dismissals")}
        history, node = set(), rid
        while node and node not in history:
            history.add(node)
            node = parents.get(node)
        persistent = [ReviewIssue.model_validate(b) for b in self.store.list_bodies("issues")]
        live = self.view(rid).rows()
        out = []
        for issue in validation + persistent:
            d = dismissals.get(issue.id)
            if d and (d["revision"] is None or d["revision"] in history):
                issue.resolution_state = "dismissed"
                issue.dismissal = {k: d[k] for k in ("dismissed_by", "reason", "proposal_id", "revision", "created_at")}
            if issue.origin != "validation" and issue.affected_ids and not any(a in live for a in issue.affected_ids):
                continue
            out.append(issue)
        return out

    def repairs(self, rid: str) -> dict[str, dict]:
        """Repair information for each validation issue in a revision, from pyshifty's algebraic
        repair witnesses (joined on focus and statement id). Computed once per revision."""
        cached = self._repair_cache.get(rid)
        if cached is not None:
            return cached
        pg = self.graph(rid)
        try:
            witnesses = self.vocab.repair_witnesses(self._repair_session(rid))
        except Exception:  # noqa: BLE001 - optional detail; issues still show the validator message
            log.exception("repair witnesses failed for %s %s", self.id, rid)
            return {}
        by_statement = {(w["focus"], w["statement_id"]): w for w in witnesses}
        by_shape = {(w["focus"], w["shape"]): w for w in witnesses}
        out: dict[str, dict] = {}
        for issue in self.issues(rid):
            for d in issue.details.get("findings", []):
                w = (by_statement.get((d["focus"], d["statement_id"])) if d.get("statement_id") is not None
                     else by_shape.get((d["focus"], d.get("shape"))))  # findings stored before statement ids
                if w is not None:
                    out[issue.id] = render_repair(pg, self.vocab, w)
                    break
        if len(self._repair_cache) > 8:
            self._repair_cache.pop(next(iter(self._repair_cache)))
        self._repair_cache[rid] = out
        return out

    def _repair_session(self, rid: str):
        session = self._repair_sessions.get(rid)
        if session is None:
            session = self.vocab.repair_session(self.graph(rid).model)
            if len(self._repair_sessions) > 4:
                self._repair_sessions.pop(next(iter(self._repair_sessions)))
            self._repair_sessions[rid] = session
        return session

    def gate(self, cand: "Candidate") -> dict | None:
        """The repair engine's soundness gate for a candidate against its base revision, with
        fixed/introduced violations shown as entity labels and shapes."""
        try:
            out = self.vocab.gate(self._repair_session(cand.base), cand.after.model)
        except Exception:  # noqa: BLE001 - optional verdict; the proposal's own validation still applies
            log.exception("repair gate failed for %s on %s", self.id, cand.base)
            return None
        for key, pg in (("fixed", cand.before), ("introduced", cand.after)):
            out[key] = [render_iri(pg, self.vocab, v["focus"])
                        + (f" · {self.vocab.curie(v['shape'])}" if v["shape"] else "") for v in out[key]]
        return out

    def set_issue_state(self, issue_id: str, state: str, dismissed_by: str = "person", reason: str | None = None,
                        proposal_id: str | None = None, revision: str | None = None, publish: bool = True) -> None:
        with self.store.tx() as db:
            if state == "dismissed":
                db.execute("INSERT INTO issue_dismissals(issue_id, dismissed_by, reason, proposal_id, revision, created_at)"
                           " VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(issue_id) DO UPDATE SET"
                           " dismissed_by=excluded.dismissed_by, reason=excluded.reason, proposal_id=excluded.proposal_id,"
                           " revision=excluded.revision, created_at=excluded.created_at",
                           (issue_id, dismissed_by, reason, proposal_id, revision, now()))
            else:
                db.execute("DELETE FROM issue_dismissals WHERE issue_id=?", (issue_id,))
        if publish:
            self.bus.publish(self.id, {"type": "issues", "head": self.head()})

    def _migrate_issue_states(self) -> None:
        """Carry dismissals from the old per-id state table to the current issue ids."""
        with self.store.tx() as db:
            legacy = [r["issue_id"] for r in db.execute("SELECT issue_id FROM issue_states WHERE state='dismissed'")]
            if not legacy:
                return
            revisions = [r["id"] for r in db.execute("SELECT id FROM revisions ORDER BY created_at DESC")]
            stored = {}
            for rid in revisions:
                row = db.execute("SELECT issues FROM revisions WHERE id=?", (rid,)).fetchone()
                for i in json.loads(row["issues"] or "[]"):
                    stored.setdefault(i["id"], (rid, i))
            for old_id in legacy:
                if old_id in stored:
                    rid, issue = stored[old_id]
                    eid = issue["affected_ids"][0] if issue["affected_ids"] else None
                    new_ids = [i.id for i in group_findings(self.graph(rid), self.vocab,
                                                            [(eid, d) for d in issue["details"].get("findings", [])])]
                else:
                    new_ids = [old_id]
                for new_id in new_ids:
                    db.execute("INSERT OR IGNORE INTO issue_dismissals(issue_id, dismissed_by, created_at)"
                               " VALUES(?, 'person', ?)", (new_id, now()))
            db.execute("DELETE FROM issue_states")

    # -------------------------------------------------------- candidates

    def validate_graph(self, pg: ProjectGraph) -> tuple[ValidationRun, list[ReviewIssue], ValidationSummary]:
        run = self.vocab.validate(pg.model)
        issues = issues_from_validation(pg, self.vocab, run)
        return run, issues, summarize(issues, run)

    def build_candidate(self, base: str, operations: list, selection: SelectionScope | None = None,
                        lock: bool = True) -> Candidate:
        """Resolve + apply operations on a copy of ``base`` and validate the result.

        Raises OperationError for malformed operations. Does not publish anything.
        """
        before = self.graph(base)
        resolved = ops_mod.resolve(before, self.vocab, operations)
        after = before.copy()
        result = ops_mod.apply(after, self.vocab, resolved, lock=lock)
        run, issues, summary = self.validate_graph(after)
        selected = set(selection.entity_ids + selection.relationship_ids) if selection else set()
        changes = compute_changes(self.view(base), project(after, self.vocab), selected, before)
        return Candidate(base, resolved, before, after, result, run, issues, summary, changes,
                         triple_diff(before.model, after.model))

    # ------------------------------------------------------------- publishing

    def _publish_graph(self, pg: ProjectGraph, parent: str | None, author: str, kind: str,
                       summary: str, operations: list, proposal_id: str | None = None,
                       validated: tuple[list[ReviewIssue], ValidationSummary] | None = None,
                       changes: list[EntityChange] | None = None) -> Revision:
        with self.lock:
            if validated is None:
                _, issues, vsum = self.validate_graph(pg)
            else:
                issues, vsum = validated
            with self.store.tx() as db:
                current = db.execute("SELECT value FROM meta WHERE key='head'").fetchone()
                head = json.loads(current["value"]) if current else None
                if head != parent:
                    raise StaleRevision(parent or "(none)", head or "(none)")
                rid = self.store.next_revision_id(db)
                self.store.write_snapshot(rid, pg.to_trig())
                created = now()
                db.execute(
                    "INSERT INTO revisions(id, parent_id, created_at, author, kind, summary, proposal_id,"
                    " operations, validation, issues) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (rid, parent, created, author, kind, summary, proposal_id,
                     OperationList.dump_json(operations).decode(), vsum.model_dump_json(),
                     json.dumps([i.model_dump() for i in issues])))
                for c in changes or []:
                    for f in c.fields:
                        db.execute(
                            "INSERT INTO corrections(revision_id, entity_id, field, before, after, origin, created_at)"
                            " VALUES(?,?,?,?,?,?,?)",
                            (rid, c.entity_id, f.field if c.change == "updated" else c.change,
                             json.dumps(f.before), json.dumps(f.after), author, created))
                self.store.set_meta("head", rid, db)
                self.store.set_meta("redo", [], db)
            self._graph_cache[rid] = pg
        self.bus.publish(self.id, {"type": "revision", "head": rid, "summary": summary})
        return self.revision(rid)

    def publish(self, cand: Candidate, author: str, kind: str, summary: str | None = None,
                proposal_id: str | None = None) -> Revision:
        return self._publish_graph(
            cand.after, parent=cand.base, author=author, kind=kind,
            summary=summary or describe_changes(cand.changes), operations=cand.ops,
            proposal_id=proposal_id, validated=(cand.issues, cand.summary), changes=cand.changes)

    def edit(self, base: str, operations: list, summary: str | None = None) -> tuple[Revision, Candidate]:
        """A direct edit: applied immediately (undoable) through the same mutation path."""
        with self.lock:
            if base != self.head():
                raise StaleRevision(base, self.head())
            cand = self.build_candidate(base, operations)
            return self.publish(cand, author="user", kind="edit", summary=summary), cand

    # ------------------------------------------------------------ undo/redo

    def _undo_target(self) -> str | None:
        rev = self.revision(self.head())
        return rev.parent_id if rev.kind != "initial" else None

    def undo(self) -> str:
        with self.lock:
            head = self.head()
            target = self._undo_target()
            if target is None:
                raise ValueError("nothing to undo")
            redo = self.store.get_meta("redo", [])
            with self.store.tx() as db:
                self.store.set_meta("head", target, db)
                self.store.set_meta("redo", [head, *redo], db)
        self.bus.publish(self.id, {"type": "revision", "head": target, "summary": f"Undid {head}"})
        return target

    def redo(self) -> str:
        with self.lock:
            redo = self.store.get_meta("redo", [])
            if not redo:
                raise ValueError("nothing to redo")
            target, rest = redo[0], redo[1:]
            if self.revision(target).parent_id != self.head():
                raise ValueError("redo history no longer applies")
            with self.store.tx() as db:
                self.store.set_meta("head", target, db)
                self.store.set_meta("redo", rest, db)
        self.bus.publish(self.id, {"type": "revision", "head": target, "summary": f"Redid {target}"})
        return target

    # ------------------------------------------------------------- proposals

    def save_proposal(self, cand: Candidate, selection: SelectionScope, instruction: str,
                      explanation: str, evidence: list, questions: list[str],
                      agent_run_id: str | None, before_summary: ValidationSummary,
                      before_issues: list[ReviewIssue], kind: str = "correction",
                      build_summary: dict | None = None, followup_issues: list[dict] | None = None,
                      parent_proposal_id: str | None = None,
                      conversation: list[dict[str, str]] | None = None,
                      issue_dismissals: list | None = None,
                      gate: dict | None = None,
                      ) -> ChangeProposal:
        before_expl = {i.explanation for i in before_issues if i.severity != "suggestion"}
        after_expl = {i.explanation for i in cand.issues if i.severity != "suggestion"}
        selected = set(selection.entity_ids + selection.relationship_ids)
        affected = sorted(cand.result.changes)
        prop = ChangeProposal(
            id=f"prop-{secrets.token_hex(4)}",
            base_revision=cand.base,
            created_at=now(),
            selection=selection,
            instruction=instruction,
            operations=cand.ops,
            affected_ids=affected,
            out_of_scope_ids=sorted({c.entity_id for c in cand.changes if selected and c.entity_id not in selected}),
            changes=cand.changes,
            diff=cand.diff,
            evidence=evidence,
            validation=ValidationDelta(
                before=before_summary, after=cand.summary,
                resolved=sorted(before_expl - after_expl), introduced=sorted(after_expl - before_expl)),
            explanation=explanation,
            notes=cand.result.notes,
            questions=questions,
            agent_run_id=agent_run_id,
            kind=kind,  # type: ignore[arg-type]
            build_summary=build_summary,
            followup_issues=followup_issues or [],
            parent_proposal_id=parent_proposal_id,
            conversation=conversation or [],
            issue_dismissals=issue_dismissals or [],
            gate=gate or (self.gate(cand) if cand.diff.added or cand.diff.removed else None),
        )
        self._put_proposal(prop)
        return prop

    def _put_proposal(self, prop: ChangeProposal) -> None:
        self.store.put_body("proposals", prop.id, prop.model_dump(mode="json"),
                            created_at=prop.created_at, base_revision=prop.base_revision,
                            status=prop.status, agent_run_id=prop.agent_run_id)

    def proposal(self, pid: str) -> ChangeProposal:
        body = self.store.get_body("proposals", pid)
        if body is None:
            raise KeyError(f"no proposal {pid}")
        prop = ChangeProposal.model_validate(body)
        if prop.status == "pending" and prop.base_revision != self.head():
            prop.status = "stale"
        return prop

    def proposals(self, status: str | None = None) -> list[ChangeProposal]:
        bodies = self.store.list_bodies("proposals", order="created_at DESC")
        props = [ChangeProposal.model_validate(b) for b in bodies]
        head = self.head()
        for p in props:
            if p.status == "pending" and p.base_revision != head:
                p.status = "stale"
        return [p for p in props if status is None or p.status == status]

    def apply_proposal(self, pid: str) -> Revision:
        with self.lock:
            prop = self.proposal(pid)
            head = self.head()
            rebasing = prop.base_revision != head
            if prop.status not in ("pending", "stale"):
                raise ValueError(f"proposal is {prop.status}")
            build = prop.kind == "build"
            try:
                # A build is extraction, not a person's correction: it doesn't lock fields.
                if rebasing:
                    live = self.view(head).rows()
                    selection = prop.selection.model_copy(update={
                        "entity_ids": [i for i in prop.selection.entity_ids if i in live],
                        "relationship_ids": [i for i in prop.selection.relationship_ids if i in live],
                    })
                    cand = self.build_candidate(head, prop.operations, selection, lock=not build)
                    # Refresh the preview to describe exactly what replaying these operations
                    # on the current revision will do.
                    before_issues = self.issues(head)
                    before = self.revision(head).validation
                    if before is None:
                        raise ProposalMismatch("the current revision has no validation summary")
                    before_expl = {i.explanation for i in before_issues if i.severity != "suggestion"}
                    after_expl = {i.explanation for i in cand.issues if i.severity != "suggestion"}
                    selected = set(selection.entity_ids + selection.relationship_ids)
                    prop.base_revision = head
                    prop.selection = selection
                    prop.operations = cand.ops
                    prop.affected_ids = sorted(cand.result.changes)
                    prop.out_of_scope_ids = sorted(
                        {c.entity_id for c in cand.changes if selected and c.entity_id not in selected})
                    prop.changes = cand.changes
                    prop.diff = cand.diff
                    prop.validation = ValidationDelta(
                        before=before, after=cand.summary,
                        resolved=sorted(before_expl - after_expl),
                        introduced=sorted(after_expl - before_expl))
                    prop.notes = cand.result.notes
                    prop.gate = self.gate(cand) if cand.diff.added or cand.diff.removed else None
                else:
                    cand = self.build_candidate(head, prop.operations, prop.selection, lock=not build)
            except OperationError:
                if not rebasing:
                    prop.status = "failed"
                    self._put_proposal(prop)
                raise
            if not rebasing and cand.diff != prop.diff:
                raise ProposalMismatch("applying the proposal would not produce the previewed change")
            if (not cand.diff.added and not cand.diff.removed
                    and set(cand.before.ann) == set(cand.after.ann)):
                self._dismiss_issues(prop, None)
                prop.status = "applied"
                prop.applied_revision = head
                self._put_proposal(prop)
                return self.revision(head)
            if build:
                summary = (prop.build_summary or {}).get("title") or "Built model from sources"
            else:
                summary = f"Assistant: {prop.instruction[:80]}" if prop.instruction else None
            rev = self.publish(cand, author="agent", kind="extraction" if build else "proposal",
                               summary=summary, proposal_id=pid)
            for issue in prop.followup_issues:
                body = {**issue, "id": f"{issue['id']}-{rev.id}"}
                self.store.put_body("issues", body["id"], body, origin=body.get("origin", "extraction"),
                                    state=body.get("resolution_state", "open"))
            self._dismiss_issues(prop, rev.id)
            prop.status = "applied"
            prop.applied_revision = rev.id
            self._put_proposal(prop)
            return rev

    def _dismiss_issues(self, prop: ChangeProposal, revision: str | None) -> None:
        for d in prop.issue_dismissals:
            self.set_issue_state(d.id, "dismissed", dismissed_by="assistant", reason=d.reason,
                                 proposal_id=prop.id, revision=revision, publish=False)
        if prop.issue_dismissals:
            self.bus.publish(self.id, {"type": "issues", "head": self.head()})

    def dismiss_proposal(self, pid: str) -> ChangeProposal:
        prop = self.proposal(pid)
        if prop.status in ("pending", "stale"):
            prop.status = "dismissed"
            self._put_proposal(prop)
        return prop

    # ---------------------------------------------------------- import/export

    def import_model(self, data: bytes, filename: str) -> Revision:
        """Replace-merge an existing RDF model into the current model as a new revision."""
        with self.lock:
            fmt = "turtle" if filename.endswith((".ttl", ".turtle")) else None
            incoming = Graph().parse(data=data, format=fmt or "turtle")
            base = self.head()
            pg = self.graph(base).copy()
            # The project keeps its own ontology declaration; fold in the imports only.
            project_onto = next(pg.model.subjects(RDF.type, OWL.Ontology), None)
            for onto in list(incoming.subjects(RDF.type, OWL.Ontology)):
                for imp in incoming.objects(onto, OWL.imports):
                    if project_onto is not None:
                        pg.model.add((project_onto, OWL.imports, imp))
                incoming.remove((onto, None, None))
            for t in incoming:
                pg.model.add(t)
            for p, n in incoming.namespaces():
                if p:
                    pg.model.bind(p, n, override=False)
            pg.invalidate()
            added = ensure_ids(pg, self.vocab)
            return self._publish_graph(pg, parent=base, author="import", kind="import",
                                       summary=f"Imported {filename} ({len(incoming)} triples, {added} entities)",
                                       operations=[])

    def export_turtle(self, rid: str) -> bytes:
        return self.graph(rid).export_turtle()

    def export_points_csv(self, rid: str) -> str:
        view = self.view(rid)
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["point_id", "point", "kind", "point_type", "measurement", "unit", "unit_symbol", "equipment",
                    "equipment_type", "medium", "substance", "sensor_type", "iri"])
        eq = {e.id: e for e in view.equipment}
        for p in view.points:
            e = eq.get(p.equipment.id) if p.equipment else None
            w.writerow([p.id, p.label, p.point_kind_label, p.point_type.label if p.point_type else "",
                        p.quantity_kind.label if p.quantity_kind else "",
                        p.unit.label if p.unit else "", p.unit_symbol,
                        p.equipment.label if p.equipment else "",
                        e.type.label if e and e.type else "",
                        p.medium.label if p.medium else "", p.substance.label if p.substance else "",
                        p.sensor_type.label if p.sensor_type else "", p.iri])
        return buf.getvalue()

    def evidence_map(self, rid: str) -> dict[str, str]:
        """observation id -> id of the live entity that cites it, in a revision."""
        pg = self.graph(rid)
        out: dict[str, str] = {}
        for eid in pg.entity_ids():
            node = pg.iri(eid)
            if node is not None:
                for obs in pg.evidence(node):
                    out.setdefault(obs, eid)
        live = self.view(rid).rows()
        return {o: e for o, e in out.items() if e in live}

    # --------------------------------------------------------------- sources

    def add_source(self, filename: str, data: bytes) -> Source:
        src = src_mod.store_source(self.root, filename, data)
        self._put_source(src)
        return src

    def _put_source(self, src: Source) -> None:
        self.store.put_body("sources", src.id, src.model_dump(mode="json"), created_at=src.created_at)
        self.bus.publish(self.id, {"type": "sources"})

    def sources(self) -> list[Source]:
        return [Source.model_validate(b) for b in self.store.list_bodies("sources", order="created_at")]

    def source(self, sid: str) -> Source:
        body = self.store.get_body("sources", sid)
        if body is None:
            raise KeyError(f"no source {sid}")
        return Source.model_validate(body)

    def source_file(self, sid: str) -> Path:
        return src_mod.source_path(self.root, self.source(sid))

    def source_grid(self, sid: str, delimiter: str | None = None) -> tuple[list[list[str]], str]:
        src = self.source(sid)
        if src.kind != "csv":
            raise ValueError("not a CSV source")
        return src_mod.read_grid(self.source_file(sid).read_bytes(), delimiter)

    def confirm_csv_mapping(self, sid: str, cfg: CsvImportConfig) -> dict:
        """Record the confirmed structure and create one observation per record.

        Re-confirming supersedes the unresolved observations of the previous mapping; ones
        already linked to model entities are kept.
        """
        with self.lock:
            src = self.source(sid)
            rows, _ = self.source_grid(sid, cfg.delimiter)
            new = src_mod.observations_from(src, rows, cfg)
            superseded = 0
            with self.store.tx() as db:
                for row in db.execute("SELECT id, body FROM observations WHERE source_id=? AND status='unresolved'", (sid,)):
                    body = json.loads(row["body"])
                    if body.get("entity_ids"):
                        continue
                    body["status"] = "superseded"
                    db.execute("UPDATE observations SET status='superseded', body=? WHERE id=?", (json.dumps(body), row["id"]))
                    superseded += 1
                db.executemany(
                    "INSERT INTO observations(id, source_id, run_id, status, body) VALUES(?,?,?,?,?)",
                    [(o.id, o.source_id, o.run_id, o.status, o.model_dump_json()) for o in new])
            src.import_config = cfg
            src.status = "configured"
            self._put_source(src)
        return {"observations": len(new), "superseded": superseded}

    def observations(self, sid: str | None = None, status: str | None = None) -> list[Observation]:
        where, args = [], []
        if sid:
            where.append("source_id=?"); args.append(sid)
        if status:
            where.append("status=?"); args.append(status)
        with self.store.tx() as db:
            rows = db.execute("SELECT body FROM observations" + (" WHERE " + " AND ".join(where) if where else "")
                              + " ORDER BY rowid", tuple(args)).fetchall()
        return [Observation.model_validate_json(r["body"]) for r in rows]

    # ---------------------------------------------------------------- layout

    def layout(self) -> dict[str, list[float]]:
        with self.store.tx() as db:
            return {r["entity_id"]: [r["x"], r["y"]] for r in db.execute("SELECT * FROM layout")}

    def save_layout(self, positions: dict[str, list[float]]) -> None:
        with self.store.tx() as db:
            db.executemany(
                "INSERT INTO layout(entity_id, x, y) VALUES(?,?,?) "
                "ON CONFLICT(entity_id) DO UPDATE SET x=excluded.x, y=excluded.y",
                [(k, float(v[0]), float(v[1])) for k, v in positions.items()])

    def corrections(self, entity_id: str | None = None) -> list[dict]:
        with self.store.tx() as db:
            q = "SELECT * FROM corrections" + (" WHERE entity_id=?" if entity_id else "") + " ORDER BY seq DESC LIMIT 500"
            rows = db.execute(q, (entity_id,) if entity_id else ()).fetchall()
        return [dict(r) for r in rows]


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:32]
    return s or "project"


class Workspace:
    def __init__(self, projects_dir: Path, registry: VocabularyRegistry, bus: EventBus):
        self.dir = projects_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry
        self.bus = bus
        self._open: dict[str, Project] = {}
        self._lock = threading.Lock()

    def create(self, name: str, profile: str = "watr") -> Project:
        pid = f"{slugify(name)}-{secrets.token_hex(2)}"
        with self._lock:
            p = Project.create(self.dir / pid, self.registry, self.bus, pid, name, profile)
            self._open[pid] = p
            return p

    def get(self, pid: str) -> Project:
        with self._lock:
            if pid not in self._open:
                root = self.dir / pid
                if not (root / "project.sqlite").exists() or "/" in pid or "\\" in pid or pid.startswith("."):
                    raise KeyError(f"no project {pid}")
                self._open[pid] = Project(root, self.registry, self.bus)
            return self._open[pid]

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.dir.iterdir()):
            if (d / "project.sqlite").exists():
                try:
                    out.append(self.get(d.name).info())
                except Exception as exc:  # a broken project shouldn't hide the others
                    out.append({"id": d.name, "name": d.name, "error": str(exc)})
        return sorted(out, key=lambda p: p.get("updated_at", ""), reverse=True)
