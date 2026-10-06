"""Render assistant questions, including structured responses from older runs."""

import ast
import json
from typing import Any


def readable_questions(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    result = []
    for item in items:
        # Earlier versions stored str(dict). Recover those saved questions safely.
        if isinstance(item, str) and item.strip().startswith("{"):
            for parse in (json.loads, ast.literal_eval):
                try:
                    parsed = parse(item)
                except (ValueError, SyntaxError, TypeError, RecursionError):
                    continue
                if isinstance(parsed, dict) and isinstance(parsed.get("question"), str):
                    item = parsed
                    break
        if isinstance(item, dict):
            question = item.get("question")
            if not isinstance(question, str) or not question.strip():
                continue
            lines = [question.strip()]
            options = item.get("options")
            for option in options if isinstance(options, list) else []:
                label = option.get("label") if isinstance(option, dict) else option
                if isinstance(label, str) and label.strip():
                    lines.append(f"• {label.strip()}")
            item = "\n".join(lines)
        if isinstance(item, str) and item.strip():
            result.append(item)
    return result
