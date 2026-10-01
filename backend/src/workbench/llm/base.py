from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


class LLMError(Exception):
    """The provider failed or returned something unusable. ``message`` is user-facing."""


class MalformedOutput(LLMError):
    """The reply was not a usable JSON object. Sampling again usually fixes it."""

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0):
        super().__init__(message)
        self.input_tokens, self.output_tokens = input_tokens, output_tokens


class Cancelled(Exception):
    pass


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


@dataclass
class ImageInput:
    data: bytes
    media_type: str = "image/png"


@dataclass
class LLMResult:
    data: dict[str, Any]
    raw_text: str
    input_tokens: int = 0
    output_tokens: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


# (characters generated so far) -> None; used for progress display
ProgressFn = Callable[[int], None]


class LLMClient(Protocol):
    provider: str
    model: str
    supports_images: bool

    def complete_json(
        self,
        system: str,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
        *,
        images: list[ImageInput] | None = None,
        cancel: CancelToken | None = None,
        on_progress: ProgressFn | None = None,
        max_tokens: int = 8000,
    ) -> LLMResult:
        """Return a JSON object conforming to ``schema``.

        ``messages`` are plain ``{"role": "user"|"assistant", "content": str}`` turns;
        ``images`` attach to the first user turn.
        """
        ...


def parse_json_text(text: str) -> dict[str, Any]:
    """Parse model output as a JSON object, tolerating code fences and preamble."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if fence:
        t = fence.group(1)
    try:
        value = json.loads(t)
    except json.JSONDecodeError:
        start, end = t.find("{"), t.rfind("}")
        if start < 0 or end <= start:
            raise MalformedOutput("the model did not return JSON") from None
        try:
            value = json.loads(t[start:end + 1])
        except json.JSONDecodeError as exc:
            raise MalformedOutput(f"the model returned malformed JSON ({exc.msg})") from None
    if isinstance(value, list) and len(value) == 1:
        value = value[0]  # some providers wrap the object in a list despite the schema
    if not isinstance(value, dict):
        raise MalformedOutput("the model returned JSON that is not an object")
    return value


def parse_reply(text: str, input_tokens: int, output_tokens: int) -> dict[str, Any]:
    """``parse_json_text``, with the attempt's token usage carried on a failure."""
    try:
        return parse_json_text(text)
    except MalformedOutput as exc:
        exc.input_tokens, exc.output_tokens = input_tokens, output_tokens
        raise


def retry_malformed(call: Callable[[], LLMResult], attempts: int = 2) -> LLMResult:
    """Ask again when a reply is not a usable JSON object, counting every attempt's tokens."""
    spent_in = spent_out = 0
    for attempt in range(attempts):
        try:
            result = call()
        except MalformedOutput as exc:
            spent_in, spent_out = spent_in + exc.input_tokens, spent_out + exc.output_tokens
            if attempt == attempts - 1:
                raise
            continue
        result.input_tokens += spent_in
        result.output_tokens += spent_out
        return result
    raise AssertionError("unreachable")
