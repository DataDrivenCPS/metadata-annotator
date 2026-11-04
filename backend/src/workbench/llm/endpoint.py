"""Endpoint health and deployed context sizes, independent of completion transport."""

from dataclasses import replace
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


class ModelListError(Exception):
    """The server's model list could not be read; the message says why."""


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
            n = launch_context(entry)  # a router's per-model --ctx-size: what the server will run with
            if n:
                return n
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

    def models(self) -> list[str]:
        """Model ids the server lists (OpenAI-compatible /models). Raises ModelListError."""
        return [m["id"] for m in self.model_entries()]

    def model_entries(self) -> list[dict[str, Any]]:
        """The server's /models entries (each with an ``id``). Raises ModelListError."""
        try:
            r = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=5.0)
        except httpx.HTTPError as exc:
            raise ModelListError(unreachable_message(self.cfg.name, self.base_url, exc)) from exc
        if r.status_code >= 400:
            raise ModelListError(f"HTTP {r.status_code}")
        try:
            data = r.json().get("data") or []
        except (ValueError, AttributeError) as exc:
            raise ModelListError(f"{self.base_url}/models did not return a model list") from exc
        return [m for m in data if isinstance(m, dict) and m.get("id")]

    def supports_images(self) -> bool | None:
        """Whether this provider's model reads images: as configured, else as the server or
        LiteLLM's catalog says. None when the server cannot be asked right now."""
        if self.cfg.supports_images is not None:
            return self.cfg.supports_images
        if self.cfg.kind == "anthropic":
            return True
        if self.cfg.kind == "litellm":
            try:
                return bool(litellm.supports_vision(model=self.cfg.model))
            except Exception:
                return False  # not in the catalog: set supports_images to say so
        try:
            entries = self.model_entries()
        except ModelListError:
            return None
        model = resolved_model(self.cfg, [e["id"] for e in entries]).model
        return self._vision(next((e for e in entries if e["id"] == model), None))

    def _vision(self, entry: dict[str, Any] | None) -> bool:
        """llama-server's router and OpenRouter list input modalities per model; a single-model
        llama-server says whether its projector is loaded in /props."""
        modalities = ((entry or {}).get("architecture") or {}).get("input_modalities")
        if isinstance(modalities, list):
            return "image" in modalities
        props = self._get_json(f"{self.base_url.removesuffix('/v1')}/props")
        return bool((props.get("modalities") or {}).get("vision"))

    def resolved(self) -> ProviderConfig:
        """This provider with the model it will actually run: the configured one, or the one a
        model-less server lists first (a single-model llama-server ignores the requested name)."""
        if self.cfg.kind != "openai":
            return self.cfg
        try:
            models = self.models()
        except ModelListError:
            return self.cfg if self.cfg.model else replace(self.cfg, model="local")
        return resolved_model(self.cfg, models)

    def health(self) -> dict[str, Any]:
        if self.cfg.kind != "openai":
            ok = bool(self.cfg.api_key or self.cfg.api_key_env is None)
            return {"ok": ok, "detail": "" if ok else f"Set {self.cfg.api_key_env} to use {self.cfg.name}."}
        try:
            entries = self.model_entries()
        except ModelListError as exc:
            return {"ok": False, "detail": str(exc)}
        models = [e["id"] for e in entries]
        ok, detail = True, ""
        if not models:
            ok, detail = False, f"{self.base_url} lists no models; load one into the server"
        elif self.cfg.model and len(models) > 1 and self.cfg.model not in models:
            ok, detail = False, f"'{self.cfg.model}' is not offered by {self.base_url}"
        info = resolved_model(self.cfg, models)  # with that model's own settings
        model = info.model
        images = info.supports_images
        if images is None:
            images = self._vision(next((e for e in entries if e["id"] == model), None)) if ok else False
        return {"ok": ok, "detail": detail, "model": model, "models": models, "model_count": len(models),
                "supports_images": images, "context_tokens": EndpointInfo(info).context_tokens() if ok else None}


def resolved_model(cfg: ProviderConfig, models: list[str]) -> ProviderConfig:
    """The model a request to this server runs on, given the models it lists, with its settings."""
    if len(models) == 1 and models[0] != cfg.model:
        return cfg.for_model(models[0])  # what answers, whatever name was asked for
    if not cfg.model and models:
        return cfg.for_model(models[0])
    return cfg


def launch_context(entry: dict[str, Any] | None) -> int | None:
    """Tokens per slot a llama-server router starts this model with (--ctx-size / --parallel)."""
    args = [str(a) for a in ((entry or {}).get("status") or {}).get("args") or []]

    def arg(*names: str) -> str | None:
        return next((args[i + 1] for i, a in enumerate(args[:-1]) if a in names), None)

    try:
        ctx, parallel = int(arg("--ctx-size", "-c") or 0), int(arg("--parallel", "-np") or 1)
    except ValueError:
        return None
    return ctx // max(parallel, 1) if ctx > 0 else None  # 0 = the model's own maximum


def _positive(n: Any) -> bool:
    return isinstance(n, int) and not isinstance(n, bool) and n > 0
