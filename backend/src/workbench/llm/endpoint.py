"""Endpoint health and deployed context sizes, independent of completion transport."""

from typing import Any
from urllib.parse import urlparse

import httpx

from ..config import ProviderConfig
from .base import detected_window
from .runtime import litellm


def unreachable_message(provider: str, base_url: str, exc: object) -> str:
    port = urlparse(base_url).port
    hint = (f" Start the local model server, e.g. `llama serve -hf <repo>:<quant> --port {port}`,"
            if urlparse(base_url).hostname in ("127.0.0.1", "localhost") else "")
    return (f"Cannot reach model endpoint '{provider}' at {base_url}.{hint}"
            f" or choose another model in the assistant panel. ({exc})")


class EndpointInfo:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.base_url = (cfg.base_url or "").rstrip("/")

    def _headers(self) -> dict[str, str]:
        if self.cfg.kind == "anthropic":
            return {"anthropic-version": "2023-06-01", **({"x-api-key": self.cfg.api_key} if self.cfg.api_key else {})}
        return {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            r = httpx.get(url, headers=self._headers(), timeout=5.0)
            value = r.json() if r.status_code < 400 else {}
        except (httpx.HTTPError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def context_tokens(self) -> int | None:
        return self.cfg.context_tokens or detected_window(
            (f"{self.cfg.kind}:{self.base_url}", self.cfg.model), self._detect_context)

    def _detect_context(self) -> int | None:
        if self.cfg.kind == "openai":
            root = self.base_url.removesuffix("/v1")
            for url in dict.fromkeys([f"{root}/props", f"{self.base_url}/props"]):
                props = self._get_json(url)
                n = (props.get("default_generation_settings") or {}).get("n_ctx") or props.get("n_ctx")
                if _positive(n):
                    return n
            for m in self._get_json(f"{self.base_url}/health").get("all_models_loaded") or []:
                n = (m.get("recipe_options") or {}).get("ctx_size")
                if m.get("model_name") == self.cfg.model and _positive(n):
                    return n
            models = [m for m in self._get_json(f"{self.base_url}/models").get("data") or [] if isinstance(m, dict)]
            entry = next((m for m in models if m.get("id") == self.cfg.model), models[0] if len(models) == 1 else None)
            for key in ("context_length", "max_model_len", "max_context_window"):
                if entry and _positive(entry.get(key)):
                    return entry[key]
        elif self.cfg.kind == "anthropic":
            root = self.base_url or "https://api.anthropic.com"
            entry = self._get_json(f"{root.removesuffix('/v1')}/v1/models/{self.cfg.model}")
            if _positive(entry.get("max_input_tokens")):
                return entry["max_input_tokens"]
        if self.cfg.kind != "openai":
            try:
                model = f"anthropic/{self.cfg.model}" if self.cfg.kind == "anthropic" else self.cfg.model
                info = litellm.get_model_info(model)
                n = info.get("max_input_tokens") or info.get("max_tokens")
                if _positive(n):
                    return n
            except Exception:
                pass  # unknown model: the caller uses its conservative context default
        return None

    def health(self) -> dict[str, Any]:
        if self.cfg.kind != "openai":
            ok = bool(self.cfg.api_key or self.cfg.api_key_env is None)
            return {"ok": ok, "detail": "" if ok else f"Set {self.cfg.api_key_env} to use {self.cfg.name}."}
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=5.0)
            ok = r.status_code < 400
            models = [m.get("id") for m in (r.json().get("data") or [])] if ok else []
            available = None if len(models) <= 1 else self.cfg.model in models
            detail = "" if ok else f"HTTP {r.status_code}"
            if available is False:
                ok, detail = False, f"'{self.cfg.model}' is not offered by {self.base_url}"
            return {"ok": ok, "detail": detail, "model": models[0] if len(models) == 1 else self.cfg.model,
                    "model_count": len(models), "context_tokens": self.context_tokens() if ok else None}
        except (httpx.HTTPError, ValueError) as exc:
            return {"ok": False, "detail": unreachable_message(self.cfg.name, self.base_url, exc)}


def _positive(n: Any) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n > 0
