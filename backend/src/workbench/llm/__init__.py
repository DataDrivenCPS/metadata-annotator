"""Model provider adapters behind one interface: ``complete_json``.

* ``openai``    - any OpenAI-compatible chat completions endpoint: a local llama.cpp
                  ``llama-server`` (the default), or a remote service.
* ``anthropic`` - the Anthropic Messages API through the official SDK.
"""

from __future__ import annotations

from ..config import ProviderConfig
from .base import Cancelled, CancelToken, ImageInput, LLMClient, LLMError, LLMResult


def make_client(cfg: ProviderConfig) -> LLMClient:
    if cfg.kind == "openai":
        from .openai_compat import OpenAICompatClient

        return OpenAICompatClient(cfg)
    if cfg.kind == "anthropic":
        from .anthropic_client import AnthropicClient

        return AnthropicClient(cfg)
    raise ValueError(f"unknown provider kind {cfg.kind!r}")


__all__ = ["Cancelled", "CancelToken", "ImageInput", "LLMClient", "LLMError", "LLMResult", "make_client"]
