"""Per-project storage: one directory holding a SQLite metadata database and files.

    <data_dir>/projects/<project-id>/
        project.sqlite          metadata (revisions, proposals, sources, observations, runs, ...)
        revisions/<rev>.trig    full model + annotation snapshot per revision
        sources/<source-id>/    uploaded source files

A project directory is self-contained: copying it copies the project.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revisions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    author TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    proposal_id TEXT,
    operations TEXT NOT NULL,
    validation TEXT,
    issues TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    base_revision TEXT NOT NULL,
    status TEXT NOT NULL,
    agent_run_id TEXT,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS corrections (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    field TEXT NOT NULL,
    before TEXT,
    after TEXT,
    origin TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    run_id TEXT,
    status TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    state TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issue_states (issue_id TEXT PRIMARY KEY, state TEXT NOT NULL);  -- legacy, migrated
-- revision: set when the dismissal came with a published change; it applies only while that
-- revision is in the head's history, so undoing the change reopens the issue.
CREATE TABLE IF NOT EXISTS issue_dismissals (
    issue_id TEXT PRIMARY KEY,
    dismissed_by TEXT NOT NULL,
    reason TEXT,
    proposal_id TEXT,
    revision TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS layout (entity_id TEXT PRIMARY KEY, x REAL NOT NULL, y REAL NOT NULL);
"""


class ProjectStore:
    def __init__(self, root: Path):
        self.root = root
        self.db_path = root / "project.sqlite"
        (root / "revisions").mkdir(parents=True, exist_ok=True)
        (root / "sources").mkdir(parents=True, exist_ok=True)
        with self.tx() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A short-lived connection per transaction; safe across threads."""
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    # ------------------------------------------------------------------- meta

    def get_meta(self, key: str, default: Any = None) -> Any:
        with self.tx() as db:
            row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value: Any, db: sqlite3.Connection | None = None) -> None:
        stmt = ("INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        if db is not None:
            db.execute(stmt, (key, json.dumps(value)))
        else:
            with self.tx() as d:
                d.execute(stmt, (key, json.dumps(value)))

    # --------------------------------------------------------------- snapshots

    def snapshot_path(self, revision_id: str) -> Path:
        return self.root / "revisions" / f"{revision_id}.trig"

    def write_snapshot(self, revision_id: str, data: bytes) -> None:
        path = self.snapshot_path(revision_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)  # atomic on POSIX and Windows

    def read_snapshot(self, revision_id: str) -> bytes:
        return self.snapshot_path(revision_id).read_bytes()

    def next_revision_id(self, db: sqlite3.Connection) -> str:
        row = db.execute("SELECT COALESCE(MAX(seq), 0) AS n FROM revisions").fetchone()
        return f"rev-{row['n'] + 1}"

    # --------------------------------------------------------- generic bodies

    def put_body(self, table: str, id_: str, body: dict, **cols: Any) -> None:
        names = ["id", *cols.keys(), "body"]
        values = [id_, *cols.values(), json.dumps(body)]
        updates = ", ".join(f"{n}=excluded.{n}" for n in names[1:])
        with self.tx() as db:
            db.execute(
                f"INSERT INTO {table}({', '.join(names)}) VALUES({', '.join('?' * len(names))}) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}", values)

    def get_body(self, table: str, id_: str) -> dict | None:
        with self.tx() as db:
            row = db.execute(f"SELECT body FROM {table} WHERE id=?", (id_,)).fetchone()
        return json.loads(row["body"]) if row else None

    def list_bodies(self, table: str, where: str = "", args: tuple = (), order: str = "") -> list[dict]:
        sql = f"SELECT body FROM {table}" + (f" WHERE {where}" if where else "") + (f" ORDER BY {order}" if order else "")
        with self.tx() as db:
            return [json.loads(r["body"]) for r in db.execute(sql, args).fetchall()]
