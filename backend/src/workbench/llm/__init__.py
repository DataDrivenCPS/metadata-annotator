"""LiteLLM model providers behind one interface: ``complete_json``.

* ``openai``    - any OpenAI-compatible chat completions endpoint: a local llama.cpp
                  ``llama-server`` (the default), or a remote service.
* ``anthropic`` - the Anthropic Messages API through LiteLLM.
* ``litellm``   - any native LiteLLM provider/model route.
"""

from __future__ import annotations

import os

# Use the versioned bundled model catalog; startup must work without network access.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from ..config import ProviderConfig
from .base import Cancelled, CancelToken, ImageInput, LLMClient, LLMError, LLMResult, context_window, reply_tokens
# Finish SDK initialization before the server starts its event loop or request workers.
from .litellm_client import LiteLLMClient


def make_client(cfg: ProviderConfig) -> LLMClient:
    if cfg.kind not in ("openai", "anthropic", "litellm"):
        raise ValueError(f"unknown provider kind {cfg.kind!r}")
    return LiteLLMClient(cfg)


__all__ = ["Cancelled", "CancelToken", "ImageInput", "LLMClient", "LLMError", "LLMResult", "context_window", "make_client", "reply_tokens"]
