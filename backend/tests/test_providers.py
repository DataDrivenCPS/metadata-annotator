"""Model providers whose server lists its own models (llama-server): no model is hard-coded,
the assistant panel offers what the server lists, and runs record the model that answered."""

from types import SimpleNamespace

import httpx
import pytest

from workbench.config import ProviderConfig, load_settings
from workbench.events import EventBus
from workbench.llm.endpoint import EndpointInfo
from workbench.runs import ProviderUnavailable, RunManager


def settings_with(tmp_path, text=""):
    path = tmp_path / "workbench.toml"
    path.write_text(text)
    return load_settings(path)


def test_a_provider_model_can_be_chosen_without_configuring_it(tmp_path):
    settings = settings_with(tmp_path)
    assert settings.provider("local").model == ""  # the default local server lists its own
    chosen = settings.provider("local/unsloth/gemma-3-4b-it-GGUF:Q4_K_M")  # ids may hold slashes
    assert (chosen.name, chosen.model) == ("local", "unsloth/gemma-3-4b-it-GGUF:Q4_K_M")
    with pytest.raises(KeyError):
        settings.provider("nowhere/gemma")


@pytest.mark.parametrize("text,problem", [
    ('[llm.providers."a/b"]\nkind = "openai"\nbase_url = "http://x/v1"\n', "cannot contain '/'"),
    ('[llm.providers.claude]\nkind = "anthropic"\n', "needs a model"),
])
def test_provider_names_and_models_are_checked_at_load(tmp_path, text, problem):
    with pytest.raises(ValueError, match=problem):
        settings_with(tmp_path, text)


def serving(monkeypatch, models, status=200):
    import workbench.llm.endpoint as endpoint

    def get(url, **kw):
        if url.endswith("/models"):
            return httpx.Response(status, json={"data": [{"id": m, "object": "model"} for m in models]})
        return httpx.Response(404)  # no /props etc.: context size is not the point here

    monkeypatch.setattr(endpoint.httpx, "get", get)


def local(model="", url="http://server/v1"):
    return ProviderConfig(name="local", kind="openai", model=model, base_url=url)


def test_health_lists_the_servers_models_and_names_the_one_that_runs(monkeypatch):
    serving(monkeypatch, ["gemma-3-4b", "qwen3-vl-8b"])
    health = EndpointInfo(local(url="http://router-a/v1")).health()
    assert health["ok"] and health["models"] == ["gemma-3-4b", "qwen3-vl-8b"]
    assert health["model"] == "gemma-3-4b"  # nothing chosen: the first it lists
    assert EndpointInfo(local("qwen3-vl-8b", "http://router-b/v1")).health()["model"] == "qwen3-vl-8b"
    missing = EndpointInfo(local("llama-9", "http://router-c/v1")).health()
    assert not missing["ok"] and "'llama-9' is not offered" in missing["detail"]


def test_a_single_model_server_reports_what_it_serves_whatever_name_was_asked_for(monkeypatch):
    serving(monkeypatch, ["gemma-3-4b-it-Q4_K_M.gguf"])
    health = EndpointInfo(local("local", "http://single/v1")).health()
    assert health["ok"] and health["model"] == "gemma-3-4b-it-Q4_K_M.gguf"


@pytest.mark.parametrize("models,status,detail", [
    ([], 200, "lists no models"),
    (["x"], 503, "HTTP 503"),
])
def test_a_server_without_a_usable_model_is_unavailable(monkeypatch, models, status, detail):
    serving(monkeypatch, models, status)
    health = EndpointInfo(local(url="http://empty/v1")).health()
    assert not health["ok"] and detail in health["detail"]


def test_runs_record_the_model_that_answers(tmp_path, monkeypatch):
    settings = settings_with(tmp_path)
    checks = []

    def client(cfg):
        def health():
            checks.append(cfg.model)
            return {"ok": True, "models": ["gemma-3-4b", "qwen3-vl-8b"]}
        return SimpleNamespace(health=health)

    monkeypatch.setattr("workbench.runs.make_client", client)
    manager = RunManager(settings, None, EventBus())
    try:
        assert manager.choose(settings.provider("local")).model == "gemma-3-4b"
        assert manager.choose(settings.provider("local/qwen3-vl-8b")).model == "qwen3-vl-8b"
        manager.choose(settings.provider("local/qwen3-vl-8b"))
        assert checks == ["", "qwen3-vl-8b"]  # each model's check is cached on its own
        manager._forget_health("local")
        manager.choose(settings.provider("local/qwen3-vl-8b"))
        assert checks[-1] == "qwen3-vl-8b" and len(checks) == 3  # a failed run forgets all of them

        monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(
            health=lambda: {"ok": True, "models": ["what-is-loaded"]}))
        manager._forget_health("local")
        assert manager.choose(settings.provider("local/what-i-asked-for")).model == "what-is-loaded"

        monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(
            health=lambda: {"ok": False, "detail": "Cannot reach model endpoint 'local'"}))
        manager._forget_health("local")
        with pytest.raises(ProviderUnavailable, match="Cannot reach"):
            manager.choose(settings.provider("local"))
    finally:
        manager.pool.shutdown()


def test_the_panel_is_offered_the_list_only_for_providers_without_a_model(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from workbench.api import create_app

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "data"))
    settings = settings_with(tmp_path, '[llm.providers.router]\nkind = "openai"\nmodel = "a/b"\n'
                                       'base_url = "https://router.example/v1"\n')
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(
        health=lambda: {"ok": True, "model": cfg.model or "gemma", "models": ["gemma", "a/b"]}))
    app = create_app(settings)
    try:
        api = TestClient(app)
        providers = {p["name"]: p for p in api.get("/api/status").json()["providers"]}
        assert providers["local"]["lists_models"] and not providers["router"]["lists_models"]
        assert api.get("/api/providers/local/health").json()["models"] == ["gemma", "a/b"]
        assert api.get("/api/providers/local/gemma/health").json()["model"] == "gemma"
        assert api.get("/api/providers/local%2Fgemma/health").json()["model"] == "gemma"  # as the browser sends it
        assert "models" not in api.get("/api/providers/router/health").json()  # not hundreds of ids
    finally:
        app.state.runs.pool.shutdown()


def router(monkeypatch, entries, props=None):
    """A llama-server: /models entries as given, /props as given (else none)."""
    import workbench.llm.endpoint as endpoint

    def get(url, **kw):
        if url.endswith("/models"):
            return httpx.Response(200, json={"data": entries})
        if url.endswith("/props") and props is not None:
            return httpx.Response(200, json=props)
        return httpx.Response(404)

    monkeypatch.setattr(endpoint.httpx, "get", get)


def entry(model, *args, images=True, trained=131072):
    return {"id": model, "object": "model", "context_length": trained,
            "architecture": {"input_modalities": ["text", "image"] if images else ["text"]},
            "status": {"value": "unloaded", "args": ["--model", f"models/{model}.gguf", *args]}}


def test_a_router_says_per_model_whether_it_reads_images_and_how_much_context_it_runs_with(monkeypatch):
    router(monkeypatch, [entry("gemma-3-4b", "--ctx-size", "32768", "--mmproj", "m.gguf"),
                         entry("tiny-text", "-c", "16384", "-np", "2", images=False),
                         entry("whole", "--ctx-size", "0", trained=65536)])
    gemma = EndpointInfo(local("gemma-3-4b", "http://vision-a/v1")).health()
    text = EndpointInfo(local("tiny-text", "http://vision-b/v1")).health()
    whole = EndpointInfo(local("whole", "http://vision-c/v1")).health()
    assert (gemma["supports_images"], gemma["context_tokens"]) == (True, 32768)  # not the trained 131072
    assert (text["supports_images"], text["context_tokens"]) == (False, 8192)  # per parallel slot
    assert whole["context_tokens"] == 65536  # --ctx-size 0: the model's own maximum


def test_a_single_model_server_reports_its_projector_and_configuration_overrides(monkeypatch):
    router(monkeypatch, [{"id": "m.gguf", "object": "model"}], props={"modalities": {"vision": True}})
    assert EndpointInfo(local(url="http://single-a/v1")).health()["supports_images"] is True
    pinned = ProviderConfig(name="local", kind="openai", base_url="http://single-b/v1", supports_images=False)
    assert EndpointInfo(pinned).health()["supports_images"] is False


def test_runs_take_vision_from_the_model_that_answers(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, '[llm.providers.claude]\nkind = "anthropic"\nmodel = "claude-opus-5-5"\n')
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(health=lambda: {
        "ok": True, "models": ["gemma", "text"], "supports_images": cfg.model in ("", "gemma")}))
    manager = RunManager(settings, None, EventBus())
    try:
        assert manager.choose(settings.provider("local/gemma")).supports_images is True
        assert manager.choose(settings.provider("local/text")).supports_images is False
        assert manager.choose(settings.provider("claude")).supports_images is True
    finally:
        manager.pool.shutdown()


def test_the_client_asks_once_and_can_still_be_told(monkeypatch):
    from workbench.llm.litellm_client import LiteLLMClient

    router(monkeypatch, [entry("gemma", "--ctx-size", "8192")])
    client = LiteLLMClient(local(url="http://client/v1"))
    assert client.supports_images is True
    client.supports_images = False
    assert client.supports_images is False


def test_drawing_descriptions_follow_what_the_server_says(tmp_path, monkeypatch):
    from workbench.retrieval.describe import Describer

    monkeypatch.delenv("WORKBENCH_OFFLINE")  # tests switch descriptions off; this one is about them
    settings = settings_with(tmp_path, '[retrieval]\ndescribe = "auto"\n'
                                       '[llm.providers.local]\nkind = "openai"\nbase_url = "http://describe/v1"\n')
    router(monkeypatch, [entry("qwen3-vl-8b", "--ctx-size", "32768"), entry("other", "--ctx-size", "32768")])
    describer = Describer(settings)
    assert describer.available and describer.model_id == "local:qwen3-vl-8b"  # recorded under the real model

    import workbench.llm.endpoint as endpoint
    monkeypatch.setattr(endpoint.httpx, "get", lambda url, **kw: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    down = Describer(settings)
    assert not down.available and "could not be reached" in down.reason
    router(monkeypatch, [entry("text-only", "--ctx-size", "8192", images=False)])
    down._asked -= 61  # a minute later it asks again
    assert "does not accept images" in down.reason


MODEL_SETTINGS = '''
[llm.providers.local]
kind = "openai"
base_url = "http://settings/v1"
request_options = { provider = { require_parameters = true }, temperature = 0.2 }
timeout_s = 600

[llm.providers.local.models."gemma-3-4b"]
request_options = { temperature = 1.0, top_k = 64, top_p = 0.95, min_p = 0.0 }

[llm.providers.local.models."text-only"]
supports_images = false
timeout_s = 900
'''


def test_each_model_is_asked_with_its_own_settings(tmp_path):
    settings = settings_with(tmp_path, MODEL_SETTINGS)
    gemma = settings.provider("local/gemma-3-4b")
    assert gemma.request_options == {"provider": {"require_parameters": True}, "temperature": 1.0,
                                     "top_k": 64, "top_p": 0.95, "min_p": 0.0}  # merged, the model's win
    assert gemma.timeout_s == 600  # the provider's, where the model sets nothing
    text = gemma.for_model("text-only")  # another model drops gemma's settings
    assert (text.request_options, text.supports_images, text.timeout_s) == (
        {"provider": {"require_parameters": True}, "temperature": 0.2}, False, 900)


def test_per_model_settings_cannot_change_the_endpoint(tmp_path):
    with pytest.raises(ValueError, match="base_url cannot be set per model"):
        settings_with(tmp_path, MODEL_SETTINGS + '\n[llm.providers.local.models."x"]\nbase_url = "http://elsewhere/v1"\n')


def test_the_model_a_server_picks_brings_its_settings(tmp_path, monkeypatch):
    settings = settings_with(tmp_path, MODEL_SETTINGS)
    router(monkeypatch, [entry("text-only", "--ctx-size", "8192"), entry("gemma-3-4b", "--ctx-size", "32768")])
    health = EndpointInfo(settings.provider("local")).health()  # nothing chosen: the first listed
    assert (health["model"], health["supports_images"]) == ("text-only", False)  # its setting beats the server
    router(monkeypatch, [entry("gemma-3-4b", "--ctx-size", "32768")])
    served = EndpointInfo(settings.provider("local/text-only")).resolved()  # one model loaded: that one answers
    assert served.model == "gemma-3-4b" and served.request_options["top_k"] == 64


def test_runs_record_how_the_model_was_asked(sample_project, tmp_path, monkeypatch):
    from workbench.schemas import SelectionScope

    settings = settings_with(tmp_path, MODEL_SETTINGS)
    monkeypatch.setattr("workbench.runs.make_client", lambda cfg: SimpleNamespace(health=lambda: {
        "ok": True, "models": ["gemma-3-4b", "text-only"], "supports_images": True, "context_tokens": 32768}))
    manager = RunManager(settings, SimpleNamespace(version="skill"), EventBus())
    monkeypatch.setattr(manager.pool, "submit", lambda *a, **kw: None)  # record the run; do not run it
    manager.save = lambda project, run: None
    try:
        run = manager.start_correction(sample_project, sample_project.head(), SelectionScope(), "Check",
                                       provider="local/gemma-3-4b")
        assert (run.provider, run.model) == ("local", "gemma-3-4b")
        assert run.model_settings["request_options"]["top_k"] == 64
        assert (run.model_settings["context_tokens"], run.model_settings["supports_images"]) == (32768, True)
    finally:
        manager.pool.shutdown()


def test_a_client_that_finds_its_model_late_keeps_the_request_images(monkeypatch):
    from workbench.llm import ImageInput
    from workbench.llm.litellm_client import LiteLLMClient

    router(monkeypatch, [entry("gemma", "--ctx-size", "8192")])
    client = LiteLLMClient(local(url="http://late/v1"))
    sent = {}
    monkeypatch.setattr(client, "_params", lambda system, messages, schema, images, limit: sent.setdefault("images", images) and {})
    picture = [ImageInput(data=b"png")]
    try:
        client.complete_json("system", [{"role": "user", "content": "look"}], {"type": "object"}, images=picture)
    except Exception:
        pass  # only what was prepared matters here
    assert client.model == "gemma" and sent["images"] == picture
