"""One streamed completion adapter using LiteLLM's provider translation."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging

from openai import APIError as ProviderAPIError

from ..config import ProviderConfig
from .base import (
    CancelToken, ImageInput, LLMError, LLMResult, MalformedOutput, ProgressFn, TruncatedOutput,
    REPLY_TOKENS, context_window,
    json_retry_message, parse_reply, retry_malformed,
)
from .endpoint import EndpointInfo, unreachable_message
from .runtime import litellm

log = logging.getLogger(__name__)
RUNAWAY_BLANK = 500


def wants_cache_markers(cfg: ProviderConfig) -> bool:
    """Providers that only cache prompts marked with ``cache_control``."""
    model = cfg.model.lower()
    if cfg.kind == "anthropic":
        return True
    if cfg.kind == "litellm":
        return "claude" in model
    return "openrouter.ai" in (cfg.base_url or "") and model.startswith(("anthropic/", "google/gemini"))


def estimate_tokens(system: str, messages: list[dict], images: list | None = None) -> int:
    """Input tokens of a request, for when the endpoint does not say (about 3 characters each)."""
    attached = len(images or []) + sum(len(m.get("images") or []) for m in messages)
    return (len(system) + sum(len(str(m.get("content", ""))) for m in messages)) // 3 + 1500 * attached


class LiteLLMClient:
    def __init__(self, cfg: ProviderConfig):
        self.cfg = cfg
        self.provider = cfg.name
        self.model = cfg.model
        self._images = cfg.supports_images if cfg.supports_images is not None else (True if cfg.kind == "anthropic" else None)
        self.prompt_cache = cfg.prompt_cache if cfg.prompt_cache is not None else wants_cache_markers(cfg)
        if cfg.kind == "openai" and not cfg.base_url:
            raise ValueError(f"provider {cfg.name!r} needs base_url")
        self.endpoint = EndpointInfo(cfg)

    @property
    def supports_images(self) -> bool:
        """As configured, else asked of the server once (runs pass the answer in their config)."""
        if self._images is None:
            self._images = self.endpoint.supports_images()
            if self._images is None:  # unreachable now: no images, but ask again next time
                return False
        return self._images

    @supports_images.setter
    def supports_images(self, value: bool) -> None:
        self._images = value

    def context_tokens(self) -> int | None:
        return self.endpoint.context_tokens()

    def health(self) -> dict:
        return self.endpoint.health()

    def _native_output_limit(self) -> int | None:
        if self.cfg.kind == "openai":
            return None  # compatible deployments may have limits different from the catalog
        try:
            model = f"anthropic/{self.model}" if self.cfg.kind == "anthropic" else self.model
            value = litellm.get_model_info(model).get("max_output_tokens")
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                return value
        except Exception:
            pass  # unknown model: the deployment context still bounds retry growth
        return None

    def complete_json(self, system, messages, schema, *, images: list[ImageInput] | None = None,
                      cancel: CancelToken | None = None, on_progress: ProgressFn | None = None,
                      max_tokens: int | None = None) -> LLMResult:
        if not self.model:  # a server that lists its own model; runs resolve it first, others ask here
            told = self._images  # image support someone set on this client
            self.__init__(self.endpoint.resolved())  # that model, with its own settings
            if told is not None and self._images is None:
                self._images = told
        retry_messages = list(messages)
        limit = max_tokens if max_tokens is not None else REPLY_TOKENS
        native_limit = self._native_output_limit()
        if native_limit:
            limit = min(limit, native_limit)

        def on_retry(exc):
            nonlocal limit
            if isinstance(exc, TruncatedOutput):
                # Reported input usage is preferable to estimates, especially for images.
                prompt = exc.input_tokens or (estimate_tokens(system, retry_messages, images)
                                              + len(json.dumps(schema)) // 3)
                # Leave room for the corrective message and estimation error. Do not grow
                # past the deployment window, or a native model's known output limit.
                ceiling = limit * 2
                if native_limit:
                    ceiling = min(ceiling, native_limit)
                limit = max(limit, min(ceiling, context_window(self) - prompt - 1024))
            log.warning("%s (%s): %s; retrying with JSON feedback and max_tokens=%d",
                        self.provider, self.model, exc, limit)
            retry_messages.append(json_retry_message(exc))

        return retry_malformed(lambda: self._complete_once(system, retry_messages, schema, images, cancel,
                                                           on_progress, limit), on_retry=on_retry)

    def _params(self, system, messages, schema, images, max_tokens):
        if (images or any(m.get("images") for m in messages)) and not self.supports_images:
            raise LLMError(f"the configured model ({self.provider}) does not accept images")
        cache = getattr(self, "prompt_cache", False)
        marker = {"cache_control": {"type": "ephemeral"}}
        msgs: list[dict] = [{"role": "system", "content": [{"type": "text", "text": system, **marker}] if cache else system}]
        pending = list(images or [])  # ``images`` attach to the first user turn
        for m in messages:
            attached = [*pending, *(m.get("images") or [])] if m["role"] == "user" else []
            if attached:
                content = [{"type": "image_url", "image_url": {
                    "url": f"data:{img.media_type};base64,{base64.b64encode(img.data).decode()}"}}
                    for img in attached]
                content.append({"type": "text", "text": m["content"]})
                msgs.append({"role": "user", "content": content})
                pending = []
            else:
                msgs.append({"role": m["role"], "content": m["content"]})
        if cache:
            # Cache everything up to the latest message: the next step reads it back at a fraction
            # of the price, then caches its own additions. (Two of the four breakpoints allowed.)
            last = msgs[-1]
            parts: list[dict] = ([{"type": "text", "text": last["content"]}] if isinstance(last["content"], str)
                                 else list(last["content"]))
            last["content"] = [*parts[:-1], {**parts[-1], **marker}]
        params = {
            "model": self.model, "messages": msgs, "max_tokens": max_tokens,
            "stream": True, "stream_options": {"include_usage": True},
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "response", "strict": self.cfg.strict_schema, "schema": schema}},
            "timeout": self.cfg.timeout_s, "num_retries": 0,
        }
        opts = copy.deepcopy(self.cfg.request_options)
        if self.cfg.kind == "litellm":
            # Native routes accept LiteLLM SDK options (e.g. reasoning_effort, extra_body).
            params.update(opts)
        else:
            params["custom_llm_provider"] = self.cfg.kind
            if self.cfg.kind == "anthropic":
                # reasoning_effort also enables adaptive thinking in LiteLLM. Preserve the
                # old effort-only setting without changing the model's reasoning mode.
                opts.setdefault("output_config", {"effort": opts.pop("effort", "medium")})
                if opts.pop("fallbacks", False):
                    raise LLMError("Anthropic native refusal fallbacks are not supported by the LiteLLM "
                                   "completion API. Remove request_options.fallbacks or set it to false.")
                params.update(opts)
            else:
                params["extra_body"] = opts
        if self.cfg.base_url:
            params["api_base"] = self.cfg.base_url.rstrip("/")
        if self.cfg.api_key:
            params["api_key"] = self.cfg.api_key
        elif self.cfg.kind == "openai":
            params["api_key"] = "unused"  # local compatible endpoints need no credentials
        # Application retries account for every attempt and supply corrective feedback.
        params["num_retries"] = 0
        params["max_retries"] = 0
        params["stream"] = True
        return params

    def _complete_once(self, system, messages, schema, images, cancel, on_progress, max_tokens):
        if cancel:
            cancel.check()
        params = self._params(system, messages, schema, images, max_tokens)
        parts = []
        tool_parts = {}
        tokens_in = tokens_out = cached = generated = blank = 0
        reported = False
        finish = generation_id = upstream = None
        stream = None
        try:
            stream = litellm.completion(**params)
            for chunk in stream:
                if cancel:
                    cancel.check()
                generation_id = chunk.id or generation_id
                upstream = getattr(chunk, "provider", None) or upstream
                usage = getattr(chunk, "usage", None)
                if usage:
                    reported = True
                    tokens_in = int(usage.prompt_tokens or 0)
                    tokens_out = int(usage.completion_tokens or 0)
                    details = getattr(usage, "prompt_tokens_details", None)
                    cached = int(getattr(usage, "cache_read_input_tokens", 0) or getattr(details, "cached_tokens", 0) or 0)
                for choice in chunk.choices or []:
                    if choice.index != 0:
                        continue  # alternatives are separate answers, never concatenate them
                    delta = choice.delta
                    if getattr(delta, "refusal", None):
                        raise LLMError("the model declined this request", tokens_in, tokens_out)
                    piece = delta.content or ""
                    emitted = []
                    if piece:
                        parts.append(piece)
                        emitted.append(piece)
                    for tool in delta.tool_calls or []:
                        if tool.function and tool.function.arguments:
                            args = tool.function.arguments
                            tool_parts.setdefault(tool.index, []).append(args)
                            emitted.append(args)
                    for piece in emitted:
                        generated += len(piece)
                        blank = blank + len(piece) if not piece.strip() else len(piece) - len(piece.rstrip())
                        if blank > RUNAWAY_BLANK:
                            raise MalformedOutput("the model got stuck emitting blank space", tokens_in, tokens_out)
                        if on_progress:
                            on_progress(generated)
                    finish = choice.finish_reason or finish
        except litellm.AuthenticationError:
            raise LLMError(f"{self.provider}: authentication failed (check {self.cfg.api_key_env})", tokens_in, tokens_out) from None
        except litellm.RateLimitError:
            raise LLMError(f"{self.provider}: rate limited; try again shortly", tokens_in, tokens_out) from None
        except litellm.Timeout:
            raise LLMError(f"{self.provider} timed out after {self.cfg.timeout_s:.0f}s", tokens_in, tokens_out) from None
        except litellm.APIConnectionError as exc:
            message = (unreachable_message(self.provider, self.cfg.base_url, exc) if self.cfg.base_url
                       else f"{self.provider}: could not connect: {exc}")
            raise LLMError(message, tokens_in, tokens_out) from None
        except ProviderAPIError as exc:
            raise LLMError(f"{self.provider}: {exc}", tokens_in, tokens_out) from None
        finally:
            if stream is not None:
                # LiteLLM's public close API is async even for synchronous provider streams.
                try:
                    asyncio.run(stream.aclose())
                except Exception:
                    log.warning("%s: could not close the model stream", self.provider, exc_info=True)
        if cancel:
            cancel.check()
        if finish == "length":
            log.warning("%s (%s): truncated reply; generation=%s chars=%d tokens=%d limit=%d",
                        self.provider, self.model, generation_id, generated, tokens_out, max_tokens)
            raise TruncatedOutput(f"the model ran out of output tokens before finishing its answer "
                                  f"({max_tokens:,}-token limit)", tokens_in, tokens_out)
        if finish in ("content_filter", "refusal"):
            raise LLMError("the model declined this request", tokens_in, tokens_out)
        text = "".join(parts)
        if tool_parts:
            if text.strip() or len(tool_parts) != 1:
                raise MalformedOutput("the model returned multiple structured answers", tokens_in, tokens_out)
            text = "".join(next(iter(tool_parts.values())))
        try:
            data = parse_reply(text, tokens_in, tokens_out)
        except MalformedOutput:
            log.warning("%s (%s): invalid JSON; generation=%s upstream=%s finish=%s chars=%d tokens=%d",
                        self.provider, self.model, generation_id, upstream, finish, generated, tokens_out)
            raise
        if not reported:  # some OpenAI-compatible servers omit usage; count something rather than nothing
            tokens_in, tokens_out = estimate_tokens(system, messages, images), max(1, generated // 4)
        return LLMResult(data=data, raw_text=text, input_tokens=tokens_in, output_tokens=tokens_out,
                         extra={"request_id": generation_id}, cached_tokens=cached, estimated=not reported)
