"""Retrieval over the vendored BuildingMOTIF skill (backend/skill/).

The skill is written for a coding agent that writes and runs Python. The workbench does
not let the model execute code; instead its modeling rules are compiled into the
application's operations, and the reference text is available to the model as guidance
it can look up by topic (see docs/architecture.md, "Using the BuildingMOTIF skill").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import yaml

# Topic -> (reference file, heading substrings). Only sections relevant to the operations
# the workbench supports are exposed.
TOPICS: dict[str, tuple[str, list[str]]] = {
    "points": ("223p_vocabulary.md", ["Properties and sensors"]),
    "connections": ("223p_vocabulary.md", ["The connection pattern"]),
    "equipment_vs_system": ("223p_vocabulary.md", ["Core concepts"]),
    "enumerations": ("223p_vocabulary.md", ["Enumerated values"]),
    "watr_layers": ("watr_vocabulary.md", ["The three layers", "Equipment and unit processes", "Treatment process types"]),
    "watr_media": ("watr_vocabulary.md", ["Water media"]),
    "watr_sensors": ("watr_vocabulary.md", ["Sensors, properties, and water-quality"]),
    "units": ("watr_vocabulary.md", ["Sensors, properties, and water-quality"]),
    "point_labels": ("point_labels.md", ["Workflow", "Non-BMS inputs", "Reporting"]),
    "evidence": ("evidence.md", ["Read the hit in context", "Report evidence honestly"]),
    "gotchas": ("watr_vocabulary.md", ["Gotchas"]),
    "brick_point_mappings": ("point_labels.md", ["Starter Brick mapping patterns"]),
    "brick_classes": ("brick_vocabulary.md", ["Confirm a candidate class", "List point classes by family"]),
    "brick_output": ("brick_vocabulary.md", ["Output to produce for the user"]),
}

FAMILY_TOPICS = {
    "s223": ["points", "connections", "equipment_vs_system", "enumerations", "watr_layers", "watr_media",
             "watr_sensors", "units", "point_labels", "evidence", "gotchas"],
    "brick": ["brick_point_mappings", "brick_classes", "brick_output", "point_labels", "evidence"],
}


@dataclass
class Section:
    file: str
    heading: str
    text: str


class SkillGuidance:
    def __init__(self, skill_dir: Path):
        self.dir = skill_dir

    @cached_property
    def version(self) -> str:
        meta = self.dir / "UPSTREAM.yml"
        if meta.exists():
            data = yaml.safe_load(meta.read_text(encoding="utf-8"))
            return f"{data.get('branch')}@{str(data.get('commit'))[:10]}"
        return "unknown"

    @cached_property
    def sections(self) -> list[Section]:
        out: list[Section] = []
        for path in sorted((self.dir / "references").glob("*.md")):
            text = path.read_text(encoding="utf-8")
            parts = re.split(r"^(#{2,3} .+)$", text, flags=re.M)
            for i in range(1, len(parts), 2):
                out.append(Section(path.name, parts[i].lstrip("# ").strip(), parts[i + 1].strip()))
        return out

    def topic(self, name: str, limit: int = 3500) -> str:
        if name not in TOPICS:
            return f"Unknown topic {name!r}. Available: {', '.join(TOPICS)}"
        file, headings = TOPICS[name]
        chunks = []
        for s in self.sections:
            if s.file == file and any(h.lower() in s.heading.lower() for h in headings):
                chunks.append(f"## {s.heading}\n{s.text}")
        text = "\n\n".join(chunks)
        return text[:limit] + ("\n[...truncated]" if len(text) > limit else "")
