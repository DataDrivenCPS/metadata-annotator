"""Uploaded sources: storage, CSV structure, and deterministic record extraction.

CSV structure is parsed deterministically from an explicit, user-confirmed configuration
(one of three layouts); the model is only ever asked to interpret the *contents* of the
records this produces. Every record keeps its original label, raw metadata and source
coordinates, and duplicate names stay separate records.

Layouts (``CsvImportConfig.layout``):

* ``row_points``    - each row describes a point: one column holds the point name, other
                      columns are metadata. ``header_row`` may be None (no header).
* ``header_points`` - a header row whose cells are point names (e.g. a historian export);
                      non-point columns (timestamps, quality flags) are excluded.
* ``column_points`` - each column describes a point: one row holds the names, other rows
                      are metadata (a vendor sheet).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
from pathlib import Path
from typing import Any

from .schemas import CsvImportConfig, Observation, Source, SourceLocation, now

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
CSV_TYPES = {".csv", ".tsv"}
DOCUMENT_TYPES = {".txt", ".md", ".json", ".yaml", ".yml", ".log", ".docx"}
MAX_UPLOAD = 50 * 1024 * 1024


class SourceError(ValueError):
    pass


def safe_filename(name: str) -> str:
    name = Path(name.replace("\\", "/")).name
    return re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "upload"


def source_kind(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_TYPES:
        return "image"
    if ext in CSV_TYPES:
        return "csv"
    if ext == ".pdf":
        return "pdf"
    if ext in DOCUMENT_TYPES:
        return "document"
    raise SourceError(f"Unsupported file type {ext or '(none)'}: upload CSV/TSV, an image, PDF, Word (.docx), or a text document.")


def store_source(root: Path, filename: str, data: bytes) -> Source:
    if len(data) > MAX_UPLOAD:
        raise SourceError("File is larger than 50 MB.")
    if not data:
        raise SourceError("File is empty.")
    kind = source_kind(filename)
    sid = f"src-{secrets.token_hex(4)}"
    fname = safe_filename(filename)
    folder = root / "sources" / sid
    width = height = None
    page_count = None
    if kind == "image":
        from PIL import Image, ImageOps

        try:
            with Image.open(io.BytesIO(data)) as im:
                width, height = ImageOps.exif_transpose(im).size
        except Exception as exc:
            raise SourceError(f"Could not read the image: {exc}") from None
    elif kind == "pdf":
        from .documents import pdf_page_count
        page_count = pdf_page_count(data)
    elif kind == "document":
        from .documents import document_text
        document_text(data, fname)
    else:
        decode_text(data)  # fail early on binary files
    folder.mkdir(parents=True, exist_ok=True)
    (folder / fname).write_bytes(data)
    return Source(id=sid, kind=kind, filename=fname, sha256=hashlib.sha256(data).hexdigest(),  # type: ignore[arg-type]
                  created_at=now(), width=width, height=height, page_count=page_count)


def source_path(root: Path, src: Source) -> Path:
    return root / "sources" / src.id / src.filename


# ------------------------------------------------------------------ CSV grid

def decode_text(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16", "cp1252"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            return text
        except UnicodeDecodeError:
            continue
    raise SourceError("The file is not readable text (expected a CSV).")


def read_grid(data: bytes, delimiter: str | None = None) -> tuple[list[list[str]], str]:
    text = decode_text(data)
    if delimiter is None:
        sample = text[:20000]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    return rows, delimiter


def cell(rows: list[list[str]], r: int, c: int) -> str:
    return rows[r][c].strip() if r < len(rows) and c < len(rows[r]) else ""


_NAME_HEADERS = re.compile(r"^(tag|tag ?name|point|point ?name|name|object ?name|label|id|point ?id)$", re.I)
_NUMERIC = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}")


def suggest_config(rows: list[list[str]], delimiter: str) -> tuple[CsvImportConfig, str]:
    """A starting configuration plus the reason, for the user to confirm or change."""
    width = max((len(r) for r in rows[:50]), default=0)
    if width <= 1:
        first = cell(rows, 0, 0)
        has_header = bool(_NAME_HEADERS.match(first))
        return (CsvImportConfig(layout="row_points", delimiter=delimiter, name_column=0,
                                header_row=0 if has_header else None, first_data_row=1 if has_header else 0),
                "One column of names" + (" under a header." if has_header else " with no header row."))
    header = [cell(rows, 0, c) for c in range(width)]
    body = rows[1:30]
    numeric_share = [
        sum(bool(_NUMERIC.match(cell(body, r, c))) for r in range(len(body))) / max(len(body), 1)
        for c in range(width)
    ]
    for c, h in enumerate(header):
        if _NAME_HEADERS.match(h):
            return (CsvImportConfig(layout="row_points", delimiter=delimiter, name_column=c, header_row=0,
                                    first_data_row=1, metadata_columns=[i for i in range(width) if i != c]),
                    f"Column \"{h}\" looks like point names; other columns become metadata.")
    if body and sum(s > 0.8 for s in numeric_share) >= max(2, width // 2):
        excluded = [c for c in range(width) if numeric_share[c] <= 0.8
                    or re.search(r"time|date|quality|status", header[c], re.I)]
        return (CsvImportConfig(layout="header_points", delimiter=delimiter, header_row=0, excluded_columns=excluded),
                "Mostly numeric columns: the header row looks like point names (a data export).")
    first_col = [cell(rows, r, 0) for r in range(min(len(rows), 12))]
    if len(rows) <= 12 and width > len(rows) and all(v and not _NUMERIC.match(v) for v in first_col):
        return (CsvImportConfig(layout="column_points", delimiter=delimiter, name_row=0,
                                metadata_rows=list(range(1, len(rows))), first_data_column=1),
                "Wide and short with row labels: each column looks like a point.")
    return (CsvImportConfig(layout="row_points", delimiter=delimiter, name_column=0, header_row=0,
                            first_data_row=1, metadata_columns=list(range(1, width))),
            "Defaulting to one point per row, names in the first column.")


def extract_records(rows: list[list[str]], cfg: CsvImportConfig) -> list[dict[str, Any]]:
    """Deterministic records: {name, metadata, location}. Empty names are skipped."""
    out: list[dict[str, Any]] = []
    width = max((len(r) for r in rows), default=0)
    if cfg.layout == "row_points":
        if cfg.name_column is None:
            raise SourceError("Choose the column that holds point names.")
        hdr = cfg.header_row
        names = {c: (cell(rows, hdr, c) if hdr is not None else "") or f"column {c + 1}" for c in range(width)}
        start = cfg.first_data_row if cfg.first_data_row is not None else (0 if hdr is None else hdr + 1)
        for r in range(start, len(rows)):
            if hdr is not None and r == hdr:
                continue
            name = cell(rows, r, cfg.name_column)
            if not name:
                continue
            meta = {names[c]: cell(rows, r, c) for c in cfg.metadata_columns
                    if c != cfg.name_column and cell(rows, r, c)}
            out.append({"name": name, "metadata": meta, "location": {"kind": "csv_row", "row": r}})
    elif cfg.layout == "header_points":
        if cfg.header_row is None:
            raise SourceError("Choose the header row that holds point names.")
        for c in range(width):
            if c in cfg.excluded_columns:
                continue
            name = cell(rows, cfg.header_row, c)
            if not name:
                continue
            samples = [cell(rows, r, c) for r in range(cfg.header_row + 1, min(len(rows), cfg.header_row + 4))]
            out.append({"name": name, "metadata": {"sample values": ", ".join(s for s in samples if s)} if any(samples) else {},
                        "location": {"kind": "csv_cell", "row": cfg.header_row, "column": c}})
    elif cfg.layout == "column_points":
        if cfg.name_row is None:
            raise SourceError("Choose the row that holds point names.")
        for c in range(cfg.first_data_column, width):
            name = cell(rows, cfg.name_row, c)
            if not name:
                continue
            meta = {cell(rows, r, 0) or f"row {r + 1}": cell(rows, r, c) for r in cfg.metadata_rows
                    if r != cfg.name_row and cell(rows, r, c)}
            out.append({"name": name, "metadata": meta, "location": {"kind": "csv_column", "column": c}})
    return out


def preview(rows: list[list[str]], cfg: CsvImportConfig, limit: int = 200) -> dict[str, Any]:
    records = extract_records(rows, cfg)
    counts: dict[str, int] = {}
    for rec in records:
        counts[rec["name"]] = counts.get(rec["name"], 0) + 1
    dupes = {n: k for n, k in counts.items() if k > 1}
    keys: list[str] = []
    for rec in records:
        for k in rec["metadata"]:
            if k not in keys:
                keys.append(k)
    return {"total": len(records), "duplicates": dupes, "metadata_keys": keys, "records": records[:limit]}


def observations_from(source: Source, rows: list[list[str]], cfg: CsvImportConfig) -> list[Observation]:
    obs = []
    for rec in extract_records(rows, cfg):
        loc = rec["location"]
        obs.append(Observation(
            id=f"obs-{secrets.token_hex(5)}", source_id=source.id, kind="point_record",
            content={"name": rec["name"], "metadata": rec["metadata"]},
            location=SourceLocation(source_id=source.id, kind=loc["kind"], row=loc.get("row"), column=loc.get("column")),
        ))
    return obs
