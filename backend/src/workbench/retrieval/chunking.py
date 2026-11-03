"""Passages ("chunks") of uploaded documents, with where they came from.

Two producers:

* ``docling_chunks`` walks a parsed DoclingDocument (docling-serve's JSON) in reading order.
  Prose is packed into passages under its section headings, never across pages. Tables become
  rows keyed by their column headers ("Tag: FIT-101 | Units: gpm"), so a point list row is
  findable by any of its values. Text inside drawings (OCR of P&IDs, single-lines) is grouped
  into labels by proximity ("FEED PUMP P-101") — Docling's own chunker drops it.
* ``text_chunks`` splits plain text (text files, Word text, a PDF page's embedded text) at
  paragraph boundaries, keeping character offsets: the same ``text_range`` the source viewer
  and the build workflow already use.

Boxes are ``[x, y, width, height]`` from the top-left: PDF points for PDF pages, pixels for
images (the units ``SourceRegion.bbox`` uses for images).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterator

TARGET_CHARS = 1200  # about 300 tokens: inside every embedding model's window, small enough to cite
MAX_CHARS = 1800

PROSE = {"text", "paragraph", "list_item", "caption", "footnote", "code", "formula", "reference",
         "checkbox_selected", "checkbox_unselected", "key_value_region", "form", "document_index"}
HEADINGS = {"title", "section_header"}
SKIPPED = {"page_header", "page_footer"}


@dataclass
class Chunk:
    kind: str  # text | table | figure
    text: str
    headings: list[str] = field(default_factory=list)
    page: int | None = None
    bbox: list[float] | None = None
    text_range: list[int] | None = None
    labels: list[dict[str, Any]] | None = None  # figure labels, each with its own box

    def embedding_text(self) -> str:
        """What is embedded and keyword-indexed: the section path gives a passage its context."""
        return (" > ".join(self.headings) + "\n" + self.text) if self.headings else self.text

    def location(self) -> dict[str, Any]:
        return {k: v for k, v in (("page", self.page), ("bbox", self.bbox), ("text_range", self.text_range)) if v is not None}


# ------------------------------------------------------------------ plain text

_PARAGRAPH = re.compile(r"\n[ \t]*\n+")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def _paragraph_spans(text: str) -> Iterator[tuple[int, int]]:
    start = 0
    for m in _PARAGRAPH.finditer(text):
        yield start, m.start()
        start = m.end()
    yield start, len(text)


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _split_long(text: str, start: int, end: int) -> Iterator[tuple[int, int]]:
    """Windows of at most MAX_CHARS, cut at a line break, sentence end or space when there is one."""
    while end - start > MAX_CHARS:
        window = text[start:start + MAX_CHARS]
        cut = max(window.rfind("\n", TARGET_CHARS // 2), window.rfind(". ", TARGET_CHARS // 2) + 1,
                  window.rfind(" ", TARGET_CHARS // 2))
        cut = cut if cut > 0 else MAX_CHARS
        yield _trim(text, start, start + cut)
        start += cut
    yield _trim(text, start, end)


def text_chunks(text: str, *, page: int | None = None, markdown: bool = False) -> list[Chunk]:
    """Paragraph-packed passages of ``text`` with character offsets into it."""
    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []
    span: tuple[int, int] | None = None

    def flush() -> None:
        nonlocal span
        if span and span[1] > span[0]:
            chunks.append(Chunk("text", text[span[0]:span[1]], [h for _, h in headings], page=page,
                                text_range=[span[0], span[1]]))
        span = None

    for p_start, p_end in _paragraph_spans(text):
        p_start, p_end = _trim(text, p_start, p_end)
        if p_end <= p_start:
            continue
        if markdown and (m := _MD_HEADING.match(text[p_start:p_end].split("\n", 1)[0])):
            flush()
            level = len(m.group(1))
            headings[:] = [h for h in headings if h[0] < level] + [(level, m.group(2))]
            if "\n" not in text[p_start:p_end]:
                continue
        for start, end in _split_long(text, p_start, p_end):
            if span and end - span[0] > TARGET_CHARS:
                flush()
            span = (span[0], end) if span else (start, end)
    flush()
    return chunks


# ------------------------------------------------------------------ Docling

class _Page:
    """Converts Docling boxes on one page to top-left source coordinates."""

    def __init__(self, size: dict[str, float] | None, scale: float = 1.0):
        self.height = float((size or {}).get("height") or 0)
        self.scale = scale

    def box(self, bbox: dict[str, Any] | None) -> list[float] | None:
        if not bbox:
            return None
        left, right = float(bbox["l"]), float(bbox["r"])
        top, bottom = float(bbox["t"]), float(bbox["b"])
        if str(bbox.get("coord_origin", "")).upper() == "BOTTOMLEFT":
            top, bottom = self.height - top, self.height - bottom
        if top > bottom:
            top, bottom = bottom, top
        s = self.scale
        return [round(left * s, 1), round(top * s, 1), round((right - left) * s, 1), round((bottom - top) * s, 1)]


def _union(boxes: list[list[float]]) -> list[float] | None:
    if not boxes:
        return None
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[0] + b[2] for b in boxes)
    y1 = max(b[1] + b[3] for b in boxes)
    return [round(x0, 1), round(y0, 1), round(x1 - x0, 1), round(y1 - y0, 1)]


def cluster_labels(items: list[tuple[str, list[float]]]) -> list[dict[str, Any]]:
    """Group OCR words of a drawing into labels: words on one line close together, or lines
    stacked tightly (a tag under an equipment name in the same box), are one label."""
    n = len(items)
    if n > 4000:  # a dense scan: proximity grouping is quadratic; keep words in reading order
        return [{"text": t, "bbox": b} for t, b in sorted(items, key=lambda i: (round(i[1][1]), i[1][0]))]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        xa, ya, wa, ha = items[i][1]
        for j in range(i + 1, n):
            xb, yb, wb, hb = items[j][1]
            h = max(min(ha, hb), 1.0)
            v_overlap = min(ya + ha, yb + hb) - max(ya, yb)
            h_overlap = min(xa + wa, xb + wb) - max(xa, xb)
            v_gap, h_gap = -v_overlap, -h_overlap
            same_line = v_overlap > 0.5 * h and h_gap < 1.2 * h
            stacked = h_overlap > 0.3 * min(wa, wb) and v_gap < 0.8 * h
            if same_line or stacked:
                parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    labels = []
    for members in groups.values():
        words = sorted((items[i] for i in members), key=lambda it: (it[1][1], it[1][0]))
        lines: list[list[tuple[str, list[float]]]] = []
        for word in words:  # rows by vertical overlap, then left to right
            if lines and min(lines[-1][0][1][1] + lines[-1][0][1][3], word[1][1] + word[1][3]) - \
                    max(lines[-1][0][1][1], word[1][1]) > 0.5 * min(lines[-1][0][1][3], word[1][3]):
                lines[-1].append(word)
            else:
                lines.append([word])
        text = " ".join(" ".join(w[0] for w in sorted(line, key=lambda w: w[1][0])) for line in lines)
        labels.append({"text": text, "bbox": _union([items[i][1] for i in members])})
    return sorted(labels, key=lambda lab: (lab["bbox"][1], lab["bbox"][0]))


def _table_rows(table: dict[str, Any]) -> list[str]:
    grid = (table.get("data") or {}).get("grid") or []
    rows = [[(cell.get("text") or "").strip() for cell in row] for row in grid]
    header_rows = 0
    for row in grid:
        if row and all(cell.get("column_header") for cell in row if (cell.get("text") or "").strip()):
            header_rows += 1
        else:
            break
    width = max((len(r) for r in rows), default=0)
    headers = [" ".join(dict.fromkeys(rows[r][c] for r in range(header_rows) if c < len(rows[r]) and rows[r][c]))
               for c in range(width)]
    out = []
    for row in rows[header_rows:]:
        cells, seen = [], set()
        for c, value in enumerate(row):
            if not value or (c > 0 and value == row[c - 1] and (headers[c] if c < len(headers) else "") in seen):
                continue  # a cell spanning columns repeats its text
            name = headers[c] if c < len(headers) else ""
            seen.add(name)
            cells.append(f"{name}: {value}" if name and name != value else value)
        if cells:
            out.append(" | ".join(cells))
    if not out and header_rows:
        out.append(" | ".join(h for h in headers if h))
    return out


def docling_chunks(doc: dict[str, Any], *, image_size: tuple[int, int] | None = None) -> list[Chunk]:
    """Passages of a DoclingDocument in reading order. ``image_size`` (pixels) scales boxes of an
    image source from Docling's page units to pixels."""
    refs: dict[str, dict[str, Any]] = {}
    for key in ("texts", "tables", "pictures", "groups", "key_value_items", "form_items"):
        for item in doc.get(key) or []:
            refs[item["self_ref"]] = item
    pages: dict[int, _Page] = {}
    for number, page in (doc.get("pages") or {}).items():
        size = page.get("size") or {}
        scale = image_size[0] / float(size["width"]) if image_size and size.get("width") else 1.0
        pages[int(number)] = _Page(size, scale)

    def where(item: dict[str, Any]) -> tuple[int | None, list[float] | None]:
        prov = (item.get("prov") or [None])[0]
        if not prov:
            return None, None
        number = int(prov["page_no"])
        return number, pages.get(number, _Page(None)).box(prov.get("bbox"))

    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []
    buffer: list[str] = []
    boxes: list[list[float]] = []
    buffer_page: int | None = None

    def heading_path() -> list[str]:
        return [h for _, h in headings]

    def flush() -> None:
        nonlocal buffer, boxes, buffer_page
        if buffer:
            chunks.append(Chunk("text", "\n".join(buffer), heading_path(), page=buffer_page, bbox=_union(boxes)))
        buffer, boxes, buffer_page = [], [], None

    def add_text(text: str, page: int | None, box: list[float] | None) -> None:
        nonlocal buffer_page
        if buffer and (page != buffer_page or sum(map(len, buffer)) + len(text) > TARGET_CHARS):
            flush()
        if len(text) > MAX_CHARS:
            flush()
            for piece in text_chunks(text, page=page):
                chunks.append(Chunk("text", piece.text, heading_path(), page=page, bbox=box))
            return
        buffer.append(text)
        buffer_page = page
        if box:
            boxes.append(box)

    def captions(item: dict[str, Any]) -> str:
        return " ".join((refs.get(c.get("$ref"), {}).get("text") or "").strip() for c in item.get("captions") or []).strip()

    def table(item: dict[str, Any]) -> None:
        flush()
        page, box = where(item)
        title = captions(item)
        rows = _table_rows(item)
        piece: list[str] = []
        for row in rows + [None]:  # type: ignore[list-item]
            if piece and (row is None or sum(map(len, piece)) + len(row) > TARGET_CHARS):
                text = "\n".join(piece)
                chunks.append(Chunk("table", f"Table: {title}\n{text}" if title else text, heading_path(), page=page, bbox=box))
                piece = []
            if row is not None:
                piece.append(row)

    def figure(item: dict[str, Any]) -> None:
        flush()
        page, box = where(item)
        words = []
        for child in item.get("children") or []:
            text_item = refs.get(child.get("$ref"))
            if not text_item or not (text_item.get("text") or "").strip():
                continue
            _, word_box = where(text_item)
            if word_box:
                words.append(((text_item["text"] or "").strip(), word_box))
        labels = cluster_labels(words)
        title = captions(item)
        descriptions = [a.get("text", "") for a in (item.get("annotations") or []) if a.get("kind") == "description"]
        prefix = "Drawing" + (f": {title}" if title else "") + (" — " + " ".join(descriptions) if descriptions else "")
        if not labels:
            if title or descriptions:
                chunks.append(Chunk("figure", prefix, heading_path(), page=page, bbox=box))
            return
        group: list[dict[str, Any]] = []
        for label in labels + [None]:  # type: ignore[list-item]
            if group and (label is None or sum(len(g["text"]) + 2 for g in group) + len(label["text"]) > TARGET_CHARS):
                chunks.append(Chunk("figure", f"{prefix}\nLabels: " + "; ".join(g["text"] for g in group),
                                    heading_path(), page=page, bbox=_union([g["bbox"] for g in group]) or box,
                                    labels=group))
                group = []
            if label is not None:
                group.append(label)

    def walk(ref: str) -> None:
        item = refs.get(ref)
        if item is None or item.get("content_layer", "body") != "body":
            return
        label = item.get("label", "")
        if label in SKIPPED:
            return
        if ref.startswith("#/tables/"):
            table(item)
            return
        if ref.startswith("#/pictures/"):
            figure(item)
            return
        if label in HEADINGS and (text := (item.get("text") or "").strip()):
            flush()
            level = 0 if label == "title" else int(item.get("level") or 1)
            headings[:] = [h for h in headings if h[0] < level] + [(level, text)]
        elif label in PROSE or (ref.startswith("#/texts/") and label not in HEADINGS):
            text = (item.get("text") or "").strip()
            if text:
                page, box = where(item)
                add_text(f"- {text}" if label == "list_item" else text, page, box)
        for child in item.get("children") or []:
            walk(child.get("$ref", ""))

    for child in (doc.get("body") or {}).get("children") or []:
        walk(child.get("$ref", ""))
    flush()
    return chunks
