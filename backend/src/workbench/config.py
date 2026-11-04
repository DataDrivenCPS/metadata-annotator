"""Application configuration.

Settings come from a TOML file (``WORKBENCH_CONFIG`` or ``./workbench.toml``) with
defaults for everything, so the app runs with no config file at all. Paths are
resolved relative to the config file's directory, or the backend directory when
there is no file.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

BACKEND_DIR = Path(__file__).resolve().parents[2]


@dataclass
class ProviderConfig:
    name: str
    kind: Literal["openai", "anthropic", "litellm"]
    # Empty for an OpenAI-compatible server (llama-server): use the model(s) it lists, chosen in
    # the assistant panel as "<provider>/<model>".
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    # Unset: asked per model (llama-server router and OpenRouter list input modalities, a
    # single-model llama-server reports its projector, LiteLLM's catalog knows native routes).
    supports_images: bool | None = None
    timeout_s: float = 300.0
    # Provider request fields for legacy kinds; LiteLLM SDK options for kind="litellm".
    request_options: dict = field(default_factory=dict)
    # Requests sent at once by work that can run in parallel (a source build's mapping batches).
    # Match a local llama-server's -np; remote providers handle several.
    concurrency: int = 4
    # Tokens one request may hold (prompt + reply). Unset: asked from the endpoint (llama-server
    # /props, Lemonade /health, a /models entry, Anthropic's Models API), else a conservative default.
    context_tokens: int | None = None
    # False permits optional keys with providers that enforce OpenAI's strict schema subset.
    # Keep omission distinct from explicit null (graph updates use null to clear a field).
    strict_schema: bool = True
    # Mark the system prompt and latest message for prompt caching (cached input is billed at a
    # fraction). Unset: on for Claude (native, LiteLLM or OpenRouter) and Gemini on OpenRouter,
    # which need explicit markers; OpenAI and llama.cpp cache repeated prefixes on their own.
    prompt_cache: bool | None = None

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
        # and watr-0.2.ttl return 404; watr-0.3.rc1.ttl is the current versioned release.
        "sources": ["https://open223.info/223p.ttl", "https://watermetadata.org/watr-0.3.rc1.ttl"],
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
        # The Brick nightly (1.5.x), which carries RealEstateCore (rec:) for spaces and assets;
        # Brick 1.4 already deprecated its own location classes in favour of REC's. The nightly is
        # fetched once and cached under its URL; delete cache/*brick* to pick up a newer build.
        # (BuildingMOTIF's builtin copy is "brick/Brick.ttl".)
        "sources": ["https://github.com/BrickSchema/Brick/releases/download/nightly/Brick.ttl"],
        "description": "Building systems and BMS points: equipment, typed points, feeds relationships; "
                       "RealEstateCore spaces and assets.",
    },
}


@dataclass
class AgentConfig:
    max_steps: int = 8
    max_repairs: int = 2

    def __post_init__(self) -> None:
        for name, minimum in (("max_steps", 1), ("max_repairs", 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise ValueError(f"agent.{name} must be an integer >= {minimum}")


@dataclass
class EmbeddingConfig:
    """How source passages become vectors. ``fastembed`` runs a small ONNX model on the CPU in this
    process (downloaded once into the data directory); ``openai`` calls any OpenAI-compatible
    ``/embeddings`` endpoint, e.g. llama-server started with ``--embeddings``; ``none`` keeps
    keyword search only."""

    kind: Literal["fastembed", "openai", "none"] = "fastembed"
    model: str = "BAAI/bge-small-en-v1.5"
    base_url: str | None = None
    api_key_env: str | None = None
    batch_size: int = 32
    timeout_s: float = 120.0

    def __post_init__(self) -> None:
        if self.kind not in ("fastembed", "openai", "none"):
            raise ValueError("retrieval.embedding.kind must be fastembed, openai or none")
        if self.kind == "openai" and not self.base_url:
            raise ValueError("retrieval.embedding.base_url is required for kind = \"openai\"")

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) if self.api_key_env else None


@dataclass
class RetrievalConfig:
    """Uploaded documents are parsed into passages and indexed when they are uploaded.

    ``parser = "docling"`` uses Docling (layout, tables, OCR for scans and drawings) through a
    docling-serve process the workbench installs into the data directory on first use and runs
    on demand, or the one at ``docling_url``. ``basic`` reads only embedded text (no OCR, no
    images). When Docling cannot be installed or started, text documents fall back to basic."""

    enabled: bool = True
    parser: Literal["docling", "basic"] = "docling"
    docling_url: str | None = None
    docling_api_key_env: str | None = None
    docling_version: str = "1.38.2"
    docling_idle_minutes: float = 10.0
    # Pages sent to the parser per request: progress is reported, and kept, page batch by page batch.
    pages_per_batch: int = 10
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    # Drawings and scans (images, PDF pages with little text or with a drawing) get one written
    # description from a vision model when indexed, so search finds what they show. "auto": when
    # the describing provider accepts images. The provider defaults to [llm] default.
    describe: Literal["auto", "off"] = "auto"
    describe_provider: str | None = None
    describe_max_pages: int = 30  # per source: bounds the one-time cost of a large drawing set
    describe_min_text: int = 200  # a PDF page with less embedded text than this counts as a scan

    def __post_init__(self) -> None:
        if isinstance(self.embedding, dict):
            self.embedding = EmbeddingConfig(**self.embedding)
        if self.parser not in ("docling", "basic"):
            raise ValueError("retrieval.parser must be docling or basic")
        if self.pages_per_batch < 1:
            raise ValueError("retrieval.pages_per_batch must be at least 1")


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
    # [views.<id>] tables: extra or overriding table views (see views.py)
    views: dict[str, dict] = field(default_factory=dict)
    # [virtual.<id>] tables: extra or overriding virtual relations (see relations.py)
    virtual: dict[str, dict] = field(default_factory=dict)
    agent: AgentConfig = field(default_factory=AgentConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    def provider(self, name: str | None = None) -> ProviderConfig:
        """A configured provider, or ``"<provider>/<model>"``: that provider with another of its
        server's models (model ids may contain slashes too)."""
        key = name or self.default_provider
        if key in self.providers:
            return self.providers[key]
        base, _, model = key.partition("/")
        if model and base in self.providers:
            return replace(self.providers[base], model=model)
        raise KeyError(f"unknown model provider {key!r}; configured: {sorted(self.providers)}")


DEFAULT_PROVIDERS = {
    # llama.cpp `llama-server` started separately, e.g.
    #   llama-server -m model.gguf --port 8081 --jinja
    "local": {
        "kind": "openai",
        "base_url": "http://127.0.0.1:8081/v1",
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
    for name, cfg in providers.items():
        if "/" in name:
            raise ValueError(f"model provider name {name!r} cannot contain '/' (it separates a provider from its model)")
        if not cfg.model and cfg.kind != "openai":
            raise ValueError(f"model provider {name!r} needs a model; only OpenAI-compatible servers list their own")
    dist = resolve(wb.get("frontend_dist"), BACKEND_DIR.parent / "frontend" / "dist")
    retrieval_table = dict(raw.get("retrieval", {}))
    if os.environ.get("WORKBENCH_OFFLINE", "").lower() in ("1", "true", "yes"):
        # Tests and CI: indexing installs, downloads and calls nothing (embedded text, keyword search).
        retrieval_table |= {"parser": "basic", "describe": "off",
                            "embedding": {**retrieval_table.get("embedding", {}), "kind": "none"}}

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
        views={k: dict(v) for k, v in raw.get("views", {}).items()},
        virtual={k: dict(v) for k, v in raw.get("virtual", {}).items()},
        agent=AgentConfig(**raw.get("agent", {})),
        retrieval=RetrievalConfig(**retrieval_table),
    )
