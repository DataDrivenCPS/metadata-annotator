"""Which supplied sources name an object the model created without citing any.

An operation from a source run that cites nothing used to be linked to every source supplied
with it, so a reviewer could not tell its links from a citation. Instead it is linked to the
observations that name it: its whole label as a phrase, else the tags in its label (``P-101``
also matches ``P101``, ``P 101`` and ``P-101-STS``, never ``P-1010``). Only when nothing names
it does it keep every supplied source. Matching is done in code: no model calls.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)*")
_SPACED = re.compile(r"\b([A-Za-z]{1,5}) (?=\d)")  # "P 101" -> "P-101"; harmless for "page 2"


def _phrase(text: str) -> str:
    return " " + " ".join(re.findall(r"[a-z0-9]+", text.lower())) + " "


def tags(text: str) -> set[str]:
    """Tag-like tokens (letters and digits) in ``text``, without separators or case, with each
    leading part that ends in a number: "P-101-STS" gives P101STS and P101."""
    found: set[str] = set()
    for token in _TOKEN.findall(_SPACED.sub(r"\1-", text)):
        parts = re.split(r"[-_.]", token.upper())
        for end in range(1, len(parts) + 1):
            joined = "".join(parts[:end])
            if re.search(r"[A-Z]", joined) and re.search(r"\d", joined) and (end == len(parts) or re.search(r"\d", parts[end - 1])):
                found.add(joined)
    return found


def observation_text(body: dict[str, Any] | None) -> str:
    """What an observation says: a passage's or page's text, a record's name and values."""
    if not body:
        return ""
    content = body.get("content") or {}
    parts = [content.get("text"), content.get("name"), *(content.get("metadata") or {}).values()]
    return "\n".join(str(p) for p in parts if isinstance(p, (str, int, float)) and str(p).strip())


def naming(label: Any, texts: dict[str, str]) -> list[str]:
    """The observation ids among ``texts`` (id -> text) that name ``label``: those containing the
    whole label if any do, else those with the most of its tags. Empty when none names it."""
    if not isinstance(label, str) or not label.strip():
        return []
    phrase = _phrase(label)
    if phrase.strip():
        whole = [oid for oid, text in texts.items() if phrase in _phrase(text)]
        if whole:
            return sorted(whole)
    wanted = tags(label)
    if not wanted:
        return []
    scores = {oid: len(wanted & tags(text)) for oid, text in texts.items()}
    best = max(scores.values(), default=0)
    return sorted(oid for oid, score in scores.items() if score == best) if best else []
