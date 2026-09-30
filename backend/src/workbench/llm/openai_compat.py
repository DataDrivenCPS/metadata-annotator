"""OpenAI-compatible chat completions: llama.cpp ``llama-server`` or a remote endpoint.

llama-server turns ``response_format: {type: json_schema}`` into a GBNF grammar, so a
local model's output is constrained to the schema token by token. Requests stream so a
cancelled run stops generation promptly (closing the connection aborts the slot in
llama-server).
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
from urllib.parse import urlparse

from ..config import ProviderConfig
from .base import CancelToken, ImageInput, LLMError, LLMResult, ProgressFn, parse_json_text


def unreachable_message(provider: str, base_url: str, exc: object) -> str:
    port = urlparse(base_url).port
    hint = (f" Start the local model server, e.g. `llama serve -hf <repo>:<quant> --port {port}`,"
            if urlparse(base_url).hostname in ("127.0.0.1", "localhost") else "")
    return (f"Cannot reach model endpoint '{provider}' at {base_url}.{hint}"
            f" or choose another model in the assistant panel. ({exc})")


class OpenAICompatClient:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.provider = cfg.name
        self.model = cfg.model
        self.supports_images = cfg.supports_images
        if not cfg.base_url:
            raise ValueError(f"provider {cfg.name!r} needs base_url")
        self.base_url = cfg.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        key = self.cfg.api_key
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def complete_json(self, system, messages, schema, *, images: list[ImageInput] | None = None,
                      cancel: CancelToken | None = None, on_progress: ProgressFn | None = None,
                      max_tokens: int = 8000) -> LLMResult:
        if images and not self.supports_images:
            raise LLMError(f"the configured model ({self.provider}) does not accept images")
        msgs: list[dict[str, Any]] = [{"role": "system", "content": system}]
        first_user = True
        for m in messages:
            if m["role"] == "user" and first_user and images:
                parts: list[dict[str, Any]] = [
                    {"type": "image_url", "image_url": {
                        "url": f"data:{img.media_type};base64,{base64.b64encode(img.data).decode()}"}}
                    for img in images
                ]
                parts.append({"type": "text", "text": m["content"]})
                msgs.append({"role": "user", "content": parts})
                first_user = False
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        body: dict[str, Any] = {
            "model": self.model,
            "messages": msgs,
            "max_tokens": max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "response", "strict": True, "schema": schema},
            },
            **self.cfg.request_options,
        }
        text_parts: list[str] = []
        usage: dict[str, Any] = {}
        finish = None
        try:
            with httpx.Client(timeout=httpx.Timeout(self.cfg.timeout_s, connect=10.0)) as client:
                with client.stream("POST", f"{self.base_url}/chat/completions",
                                   headers=self._headers(), json=body) as resp:
                    if resp.status_code >= 400:
                        detail = resp.read().decode(errors="replace")[:500]
                        raise LLMError(f"{self.provider} returned HTTP {resp.status_code}: {detail}")
                    for line in resp.iter_lines():
                        if cancel:
                            cancel.check()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("error"):
                            raise LLMError(f"{self.provider}: {chunk['error']}")
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            if delta.get("content"):
                                text_parts.append(delta["content"])
                                if on_progress:
                                    on_progress(sum(map(len, text_parts)))
                            finish = choice.get("finish_reason") or finish
        except httpx.ConnectError as exc:
            raise LLMError(unreachable_message(self.provider, self.base_url, exc)) from None
        except httpx.TimeoutException:
            raise LLMError(f"{self.provider} timed out after {self.cfg.timeout_s:.0f}s") from None
        text = "".join(text_parts)
        if finish == "length":
            raise LLMError("the model ran out of output tokens before finishing its answer")
        return LLMResult(
            data=parse_json_text(text), raw_text=text,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )

    def health(self) -> dict[str, Any]:
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=5.0)
            ok = r.status_code < 400
            models = [m.get("id") for m in (r.json().get("data") or [])] if ok else []
            # llama-server serves exactly one model whatever name is requested; a remote
            # service must actually offer the configured one.
            available = None if len(models) <= 1 else self.model in models
            detail = "" if ok else f"HTTP {r.status_code}"
            if available is False:
                ok, detail = False, f"'{self.model}' is not offered by {self.base_url}"
            return {"ok": ok, "detail": detail, "model": models[0] if len(models) == 1 else self.model,
                    "model_count": len(models)}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "detail": unreachable_message(self.provider, self.base_url, exc)}
