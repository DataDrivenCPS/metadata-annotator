"""Typed application contracts shared by the API, the project service, and the agent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from .operations import Operation


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------- sources

class CsvImportConfig(BaseModel):
    """Deterministic CSV structure. The model interprets contents, never the structure."""

    layout: Literal["header_points", "row_points", "column_points"]
    delimiter: str = ","
    # header_points: the row whose cells are point names; columns to exclude.
    # row_points: the row naming the metadata columns, or None when there is no header.
    header_row: int | None = 0
    excluded_columns: list[int] = Field(default_factory=list)
    # row_points: which column holds the point name; metadata columns
    name_column: int | None = None
    metadata_columns: list[int] = Field(default_factory=list)
    first_data_row: int | None = None  # default: the row after header_row
    # column_points: which row holds point names; metadata rows
    name_row: int | None = None
    metadata_rows: list[int] = Field(default_factory=list)
    first_data_column: int = 1


class Source(BaseModel):
    id: str
    kind: Literal["csv", "image", "pdf", "document", "rdf"]
    filename: str
    sha256: str
    created_at: str
    import_config: CsvImportConfig | None = None
    status: Literal["uploaded", "configured", "extracting", "extracted", "failed"] = "uploaded"
    width: int | None = None
    height: int | None = None
    page_count: int | None = None


class SourceLocation(BaseModel):
    source_id: str
    kind: Literal["csv_row", "csv_column", "csv_cell", "image_region", "document"]
    row: int | None = None
    column: int | None = None
    # image regions: x, y, width, height in source pixels
    bbox: list[float] | None = None
    page: int | None = None


class Observation(BaseModel):
    """Something a source says, kept separate from accepted model assertions."""

    id: str
    source_id: str
    run_id: str | None = None
    kind: Literal["point_record", "equipment", "label", "connection", "association"]
    content: dict[str, Any]
    location: SourceLocation
    status: Literal["unresolved", "accepted", "rejected", "superseded"] = "unresolved"
    entity_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


# ------------------------------------------------------------------ revisions

class ValidationSummary(BaseModel):
    conforms: bool
    violations: int
    warnings: int
    suggestions: int
    duration_s: float


class Revision(BaseModel):
    id: str
    parent_id: str | None
    created_at: str
    author: Literal["user", "agent", "import", "extraction", "system"]
    kind: Literal["initial", "import", "edit", "proposal", "extraction"]
    summary: str
    proposal_id: str | None = None
    operations: list[Operation] = Field(default_factory=list)
    validation: ValidationSummary | None = None


# ------------------------------------------------------------------ selection

class SourceRegion(BaseModel):
    source_id: str
    bbox: list[float] | None = None
    rows: list[int] | None = None
    pages: list[int] | None = None  # PDF page numbers, starting at 1


class SelectionScope(BaseModel):
    entity_ids: list[str] = Field(default_factory=list)
    relationship_ids: list[str] = Field(default_factory=list)
    field_ids: list[str] = Field(default_factory=list)
    # Evidence, not model objects.
    source_regions: list[SourceRegion] = Field(default_factory=list)


class Selection(BaseModel):
    base_revision: str
    selection: SelectionScope = Field(default_factory=SelectionScope)


# ---------------------------------------------------------------- proposals

class FieldChange(BaseModel):
    field: str
    before: Any = None
    after: Any = None


class EntityChange(BaseModel):
    entity_id: str
    entity_kind: str
    label: str
    change: Literal["created", "updated", "deleted"]
    fields: list[FieldChange] = Field(default_factory=list)
    in_selection: bool = True
    overrides_locked: list[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    kind: Literal["observation", "source_region", "model", "guidance", "vocabulary"]
    ref: str
    summary: str


class TripleDiff(BaseModel):
    added: list[str]
    removed: list[str]


class ValidationDelta(BaseModel):
    before: ValidationSummary
    after: ValidationSummary
    resolved: list[str] = Field(default_factory=list)  # issue explanations
    introduced: list[str] = Field(default_factory=list)


class IssueDismissal(BaseModel):
    """A review issue the assistant proposes to dismiss; applied with the proposal."""
    id: str
    explanation: str
    severity: str
    reason: str


class ChangeProposal(BaseModel):
    id: str
    base_revision: str
    created_at: str
    selection: SelectionScope
    instruction: str
    operations: list[Operation]
    affected_ids: list[str]
    out_of_scope_ids: list[str]
    changes: list[EntityChange]
    diff: TripleDiff
    evidence: list[EvidenceRef] = Field(default_factory=list)
    validation: ValidationDelta | None = None
    explanation: str = ""
    notes: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    status: Literal["pending", "applied", "dismissed", "stale", "failed"] = "pending"
    agent_run_id: str | None = None
    applied_revision: str | None = None
    # "build": an initial model built from source records (not a person's correction, so
    # applying it does not lock fields). build_summary describes the mapping for review.
    kind: Literal["correction", "build"] = "correction"
    build_summary: dict[str, Any] | None = None
    followup_issues: list[dict[str, Any]] = Field(default_factory=list)
    parent_proposal_id: str | None = None
    conversation: list[dict[str, str]] = Field(default_factory=list)
    issue_dismissals: list[IssueDismissal] = Field(default_factory=list)
    # The repair engine's soundness gate on this change: sound, progress, fixed, introduced.
    gate: dict[str, Any] | None = None


# ------------------------------------------------------------------- issues

class ReviewIssue(BaseModel):
    id: str
    affected_ids: list[str]
    category: Literal[
        "missing_information", "invalid_value", "topology", "unassigned",
        "unresolved_extraction", "ambiguous_association", "conflict", "validation", "other",
    ]
    severity: Literal["violation", "warning", "suggestion"]
    explanation: str
    resolution_state: Literal["open", "resolved", "dismissed"] = "open"
    origin: Literal["validation", "extraction", "association", "correction"]
    details: dict[str, Any] = Field(default_factory=dict)
    # Who dismissed it and why, while the dismissal applies to this revision.
    dismissal: dict[str, Any] | None = None


# --------------------------------------------------------------- agent runs

class ProgressEvent(BaseModel):
    at: str = Field(default_factory=now)
    stage: str
    message: str
    data: dict[str, Any] = Field(default_factory=dict)


class AgentRun(BaseModel):
    id: str
    kind: Literal["correction", "build", "image_extraction", "association"]
    input_revision: str
    selection: SelectionScope | None = None
    source_ids: list[str] = Field(default_factory=list)
    instruction: str = ""
    # How the run was requested, and the earlier exchange it continues (oldest first).
    mode: Literal["assist", "reply", "reconsider", "build"] = "assist"
    parent_run_id: str | None = None
    conversation: list[dict[str, str]] = Field(default_factory=list)
    provider: str
    model: str
    skill_version: str
    status: Literal["queued", "running", "succeeded", "failed", "cancelled"] = "queued"
    progress: list[ProgressEvent] = Field(default_factory=list)
    outcome: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: str = Field(default_factory=now)
    finished_at: str | None = None
