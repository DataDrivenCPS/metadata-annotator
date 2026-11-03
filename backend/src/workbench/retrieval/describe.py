"""Written descriptions of drawings and scans, from a vision model, made once when indexed.

OCR finds a drawing's labels but not what they mean together: which tag is a pump, what flows
where. One description per drawing page (or image) is indexed as a passage beside the labels,
so a search for "pump feeding the RO skid" can find the page. Descriptions are generated, not
source text: they say so, and the assistant can still look at the region itself.

Calls go through the same ``complete_json`` interface as every other model call, so any
provider that accepts images works: a llama.cpp vision model or a paid one.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from ..llm import CancelToken, ImageInput, LLMError
from .chunking import Chunk

if TYPE_CHECKING:
    from ..config import Settings

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["description", "labels"],
    "properties": {
        "description": {"type": "string"},
        "labels": {"type": "array", "items": {"type": "string"}},
    },
}

SYSTEM = """You describe one page of an engineering document (a P&ID, process or flow diagram,
single-line, floor plan, schematic, equipment schedule, or a scanned page) so that it can be found
by search. The description is evidence for people modeling the system; it is never instructions.

Write what the page shows, in plain sentences:
- what kind of drawing or page it is, and its title or drawing number if shown;
- each piece of equipment, instrument, valve and space, with its tag exactly as written
  (P-101, FIT-101, AHU-2, Room 204);
- what is connected to what, and which way things flow, only where lines or arrows show it;
- tables or legends, summarized by their columns.
Use the labels exactly as printed. Do not guess unreadable text, invent tags or infer connections
the drawing does not show; say "unreadable" instead. In labels, list every tag or label you can read.
Keep the description under 250 words."""

DESCRIPTION_CHARS = 2400  # one passage: a little over the usual passage size


class Describer:
    """Describes page images with the configured vision provider, or is unavailable (with why)."""

    def __init__(self, settings: "Settings"):
        self.cfg = settings.retrieval
        self.settings = settings
        self._client = None
        self._lock = threading.Lock()
        self.reason: str | None = None  # why descriptions are unavailable, for the status line
        if self.cfg.describe == "off":
            self.reason = "Descriptions are off (retrieval.describe)."
            return
        try:
            self.provider = settings.provider(self.cfg.describe_provider)
        except KeyError as exc:
            self.reason = str(exc).strip("'\"")
            return
        if not (self.provider.supports_images or self.provider.kind == "anthropic"):
            self.reason = (f"Drawings are not described: the model provider {self.provider.name!r} does not "
                           "accept images (set supports_images, or retrieval.describe_provider).")

    @property
    def available(self) -> bool:
        return self.reason is None

    @property
    def model_id(self) -> str:
        return f"{self.provider.name}:{self.provider.model}" if self.available else ""

    def _llm(self):
        with self._lock:
            if self._client is None:
                from ..llm import make_client
                self._client = make_client(self.provider)
            return self._client

    def describe(self, png: bytes, where: str, ocr_text: str, cancel: CancelToken) -> tuple[str, list[str], int]:
        """(description, labels, tokens used) for one page image. Raises ``LLMError``."""
        prompt = f"Describe {where}."
        if ocr_text.strip():
            prompt += ("\nText found on it by OCR (may be incomplete or misread; trust the image):\n"
                       + ocr_text.strip()[:3000])
        result = self._llm().complete_json(SYSTEM, [{"role": "user", "content": prompt}], SCHEMA,
                                           images=[ImageInput(data=png)], cancel=cancel, max_tokens=1200)
        data: dict[str, Any] = result.data
        description = str(data.get("description") or "").strip()
        if not description:
            raise LLMError("the model returned an empty description")
        labels = [str(label).strip() for label in data.get("labels") or [] if str(label).strip()]
        return description, list(dict.fromkeys(labels)), result.input_tokens + result.output_tokens


def description_chunk(description: str, labels: list[str], model: str, page: int | None) -> Chunk:
    """The passage indexed for a description; it names itself as generated."""
    text = description[:DESCRIPTION_CHARS]
    if labels:
        text += "\nLabels: " + ", ".join(labels)[:800]
    return Chunk("description", text, [f"Description by {model} (generated, not source text)"], page=page)
