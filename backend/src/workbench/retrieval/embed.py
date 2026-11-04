"""Text embeddings for passages and queries. Vectors are float32 and unit length, so a dot
product is the cosine similarity."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol

import httpx
import numpy as np

from ..config import EmbeddingConfig


class EmbeddingError(Exception):
    """Embeddings are unavailable; search falls back to keywords. ``str()`` is user-facing."""


class Embedder(Protocol):
    model_id: str  # stored with every vector; a different id means the vectors must be recomputed

    def documents(self, texts: list[str]) -> np.ndarray: ...

    def query(self, text: str) -> np.ndarray: ...


def normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class FastEmbedEmbedder:
    """A small ONNX model on the CPU (fastembed). The model downloads once into ``cache_dir``."""

    def __init__(self, cfg: EmbeddingConfig, cache_dir: Path):
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.model_id = f"fastembed:{cfg.model}"
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._model is None:
                try:
                    from fastembed import TextEmbedding
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    self._model = TextEmbedding(self.cfg.model, cache_dir=str(self.cache_dir))
                except Exception as exc:  # noqa: BLE001 - download or runtime problems
                    raise EmbeddingError(f"Could not load the embedding model {self.cfg.model}: {exc}") from None
            return self._model

    def documents(self, texts: list[str]) -> np.ndarray:
        model = self._load()
        return normalize(np.array(list(model.embed(texts, batch_size=self.cfg.batch_size))))

    def query(self, text: str) -> np.ndarray:
        return normalize(np.array(list(self._load().query_embed(text)))[0])


class OpenAIEmbedder:
    """Any OpenAI-compatible ``/embeddings`` endpoint: llama-server ``--embeddings``, OpenAI, OpenRouter…"""

    def __init__(self, cfg: EmbeddingConfig):
        self.cfg = cfg
        self.url = (cfg.base_url or "").rstrip("/") + "/embeddings"
        self.model_id = f"openai:{cfg.base_url}:{cfg.model}"

    def _embed(self, texts: list[str]) -> np.ndarray:
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        out: list[list[float]] = []
        try:
            with httpx.Client(timeout=self.cfg.timeout_s, headers=headers) as client:
                for start in range(0, len(texts), self.cfg.batch_size):
                    batch = texts[start:start + self.cfg.batch_size]
                    response = client.post(self.url, json={"model": self.cfg.model, "input": batch})
                    if response.status_code >= 400:
                        raise EmbeddingError(f"The embedding endpoint answered {response.status_code}: "
                                             f"{response.text[:300]}")
                    data = sorted(response.json()["data"], key=lambda d: d.get("index", 0))
                    out.extend(d["embedding"] for d in data)
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"The embedding endpoint {self.url} is not reachable: {exc}") from None
        except (KeyError, ValueError, TypeError) as exc:
            raise EmbeddingError(f"The embedding endpoint returned an unexpected reply: {exc}") from None
        return normalize(np.array(out))

    def documents(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts)

    def query(self, text: str) -> np.ndarray:
        return self._embed([text])[0]


def make_embedder(cfg: EmbeddingConfig, cache_dir: Path) -> Embedder | None:
    if cfg.kind == "none":
        return None
    if cfg.kind == "openai":
        return OpenAIEmbedder(cfg)
    return FastEmbedEmbedder(cfg, cache_dir)
