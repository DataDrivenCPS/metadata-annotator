import logging
from pathlib import Path

import pytest

from workbench.config import load_settings
from workbench.events import EventBus
from workbench.project import Workspace
from workbench.vocabulary import Vocabulary, VocabularyRegistry

SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "ro-train"
logging.getLogger("rdflib.term").setLevel(logging.ERROR)


@pytest.fixture(scope="session")
def registry() -> VocabularyRegistry:
    s = load_settings()
    return VocabularyRegistry(s.profiles, s.cache_dir)


@pytest.fixture(scope="session")
def vocab(registry) -> Vocabulary:
    return registry.get("watr")


@pytest.fixture
def workspace(tmp_path, registry) -> Workspace:
    return Workspace(tmp_path / "projects", registry, EventBus())


@pytest.fixture
def sample_project(workspace):
    p = workspace.create("RO train", "watr")
    p.import_model((SAMPLES / "model.ttl").read_bytes(), "model.ttl")
    return p


def by_label(rows, label):
    matches = [r for r in rows if r.label == label] or [r for r in rows if r.label.startswith(label + " ")]
    assert len(matches) == 1, (label, [r.label for r in rows])
    return matches[0]
