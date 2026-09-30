"""Application configuration.

Settings come from a TOML file (``WORKBENCH_CONFIG`` or ``./workbench.toml``) with
defaults for everything, so the app runs with no config file at all. Paths are
resolved relative to the config file's directory, or the backend directory when
there is no file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass
class ProviderConfig:
    name: str
    kind: Literal["openai", "anthropic"]
    model: str
    base_url: str | None = None
    api_key_env: str | None = None
    supports_images: bool = False
    timeout_s: float = 300.0
    # Extra fields merged into every request body (e.g. temperature, top_p).
    request_options: dict = field(default_factory=dict)

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass
class ProfileConfig:
    """A target vocabulary. ``sources`` load in order, as the BuildingMOTIF skill says to
    load them (e.g. 223P before WaTr); BuildingMOTIF's OntoEnv fetches their imports."""

    name: str
    label: str
    family: Literal["s223", "brick"]
    sources: list[str]
    description: str = ""


DEFAULT_PROFILES = {
    "watr": {
        "label": "WaTr (water treatment, on ASHRAE 223P)",
        "family": "s223",
        # 223P first, as the skill prescribes. The skill's https://watermetadata.org/water.ttl
        # now returns 404; watr-0.2.ttl is the current versioned release.
        "sources": ["https://open223.info/223p.ttl", "https://watermetadata.org/watr-0.2.ttl"],
        "description": "Treatment trains: equipment with treatment processes, pipes with water media, sensors.",
    },
    "223p": {
        "label": "ASHRAE 223P",
        "family": "s223",
        "sources": ["https://open223.info/223p.ttl"],
        "description": "Building and plant topology: equipment, connection points, connections, properties.",
    },
    "brick": {
        "label": "Brick",
        "family": "brick",
        # BuildingMOTIF's builtin copy, loaded the way the skill prescribes.
        "sources": ["brick/Brick.ttl"],
        "description": "Building systems and BMS points: equipment, typed points, feeds relationships.",
    },
}


@dataclass
class Settings:
    data_dir: Path
    profiles: dict[str, ProfileConfig]
    default_profile: str
    skill_dir: Path
    host: str
    port: int
    default_provider: str
    providers: dict[str, ProviderConfig]
    frontend_dist: Path | None
    samples_dir: Path

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    def provider(self, name: str | None = None) -> ProviderConfig:
        key = name or self.default_provider
        if key not in self.providers:
            raise KeyError(f"unknown model provider {key!r}; configured: {sorted(self.providers)}")
        return self.providers[key]


DEFAULT_PROVIDERS = {
    # llama.cpp `llama-server` started separately, e.g.
    #   llama-server -m model.gguf --port 8081 --jinja
    "local": {
        "kind": "openai",
        "base_url": "http://127.0.0.1:8081/v1",
        "model": "local",
        "supports_images": False,
    },
}


def load_settings(path: str | Path | None = None) -> Settings:
    path = path or os.environ.get("WORKBENCH_CONFIG")
    if path is None and Path("workbench.toml").exists():
        path = "workbench.toml"
    raw: dict = {}
    base = BACKEND_DIR
    if path is not None:
        p = Path(path).resolve()
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        base = p.parent

    def resolve(value: str | None, default: Path) -> Path:
        if value is None:
            return default
        v = Path(os.path.expanduser(value))
        return v if v.is_absolute() else (base / v).resolve()

    wb = raw.get("workbench", {})
    llm = raw.get("llm", {})
    profile_tables = {k: dict(v) for k, v in DEFAULT_PROFILES.items()}
    for name, table in raw.get("vocabularies", {}).items():
        profile_tables[name] = {**profile_tables.get(name, {}), **table}
    profiles = {name: ProfileConfig(name=name, **t) for name, t in profile_tables.items()}

    provider_tables = {**DEFAULT_PROVIDERS, **llm.get("providers", {})}
    providers = {
        name: ProviderConfig(name=name, **table) for name, table in provider_tables.items()
    }
    dist = resolve(wb.get("frontend_dist"), BACKEND_DIR.parent / "frontend" / "dist")

    return Settings(
        data_dir=resolve(os.environ.get("WORKBENCH_DATA_DIR") or wb.get("data_dir"),
                         BACKEND_DIR.parent / "workbench-data"),
        profiles=profiles,
        default_profile=wb.get("default_vocabulary", "watr"),
        skill_dir=resolve(wb.get("skill_dir"), BACKEND_DIR / "skill"),
        host=wb.get("host", "127.0.0.1"),
        port=int(wb.get("port", 8765)),
        default_provider=os.environ.get("WORKBENCH_PROVIDER") or llm.get("default", "local"),
        providers=providers,
        frontend_dist=dist if dist.exists() else None,
        samples_dir=resolve(wb.get("samples_dir"), BACKEND_DIR.parent / "samples"),
    )
