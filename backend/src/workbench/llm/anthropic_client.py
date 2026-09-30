"""Anthropic Messages API adapter (official SDK).

Structured output uses ``output_config.format`` (JSON schema), so the first text block is
schema-valid JSON. Requests stream so cancellation can stop generation. Server-side
refusal fallbacks are enabled by default (``fallbacks: "default"``); set
``request_options = { fallbacks = false }`` in the provider config to turn them off.
"""

from __future__ import annotations

import base64
from typing import Any

import anthropic

from ..config import ProviderConfig
from .base import CancelToken, ImageInput, LLMError, LLMResult, ProgressFn, parse_json_text

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicClient:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.provider = cfg.name
        self.model = cfg.model or "claude-opus-5-5"
        self.supports_images = True
        opts = dict(cfg.request_options)
        self.use_fallbacks = bool(opts.pop("fallbacks", True))
        self.effort = opts.pop("effort", "medium")
        self.extra = opts
        kwargs: dict[str, Any] = {"timeout": cfg.timeout_s, "max_retries": 2}
        if cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = anthropic.Anthropic(**kwargs)

    def complete_json(self, system, messages, schema, *, images: list[ImageInput] | None = None,
                      cancel: CancelToken | None = None, on_progress: ProgressFn | None = None,
                      max_tokens: int = 16000) -> LLMResult:
        msgs: list[dict[str, Any]] = []
        first_user = True
        for m in messages:
            if m["role"] == "user" and first_user and images:
                content: list[dict[str, Any]] = [
                    {"type": "image", "source": {"type": "base64", "media_type": img.media_type,
                                                 "data": base64.standard_b64encode(img.data).decode()}}
                    for img in images
                ]
                content.append({"type": "text", "text": m["content"]})
                msgs.append({"role": "user", "content": content})
                first_user = False
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": msgs,
            "output_config": {"effort": self.effort,
                              "format": {"type": "json_schema", "schema": schema}},
            **self.extra,
        }
        if self.use_fallbacks:
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        generated = 0
        try:
            with self.client.beta.messages.stream(**params) as stream:
                for event in stream:
                    if cancel:
                        cancel.check()
                    if getattr(event, "type", "") == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        text = getattr(delta, "text", None) or ""
                        generated += len(text)
                        if on_progress and text:
                            on_progress(generated)
                message = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise LLMError(f"{self.provider}: authentication failed (check {self.cfg.api_key_env})") from None
        except anthropic.RateLimitError:
            raise LLMError(f"{self.provider}: rate limited; try again shortly") from None
        except anthropic.BadRequestError as exc:
            raise LLMError(f"{self.provider}: request rejected: {exc.message}") from None
        except anthropic.APIStatusError as exc:
            raise LLMError(f"{self.provider}: HTTP {exc.status_code}: {exc.message}") from None
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"{self.provider}: could not connect: {exc}") from None

        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMError(f"the model declined this request ({getattr(details, 'category', None) or 'refusal'})")
        if message.stop_reason == "max_tokens":
            raise LLMError("the model ran out of output tokens before finishing its answer")
        text = next((b.text for b in message.content if b.type == "text"), "")
        return LLMResult(
            data=parse_json_text(text), raw_text=text,
            input_tokens=message.usage.input_tokens, output_tokens=message.usage.output_tokens,
            extra={"model": message.model, "request_id": getattr(message, "_request_id", None)},
        )

    def health(self) -> dict[str, Any]:
        return {"ok": bool(self.cfg.api_key or self.cfg.api_key_env is None), "detail": ""}
