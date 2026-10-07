import json

import httpx
import pytest

from workbench.config import ProviderConfig
from workbench.llm import Cancelled, CancelToken, LLMError, make_client
from workbench.llm.base import ImageInput, MalformedOutput, parse_json_text

import litellm
from litellm.types.utils import ModelResponseStream

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}},
          "required": ["ok"], "additionalProperties": False}
MESSAGES = [{"role": "user", "content": "add sensors"}]


def chunk(text="", finish=None, usage=None, **delta):
    return ModelResponseStream(choices=[{
        "index": 0, "delta": {"content": text, **delta}, "finish_reason": finish}], usage=usage)


class Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        for item in self.chunks:
            if isinstance(item, Exception):
                raise item
            yield item

    async def aclose(self):
        self.closed = True


def fake_completion(monkeypatch, attempts):
    requests, streams = [], []

    def complete(**params):
        requests.append(params)
        reply = attempts[len(requests) - 1]
        if isinstance(reply, Exception):
            raise reply
        stream = Stream(reply)
        streams.append(stream)
        return stream

    monkeypatch.setattr("workbench.llm.litellm_client.litellm.completion", complete)
    return requests, streams


def client(kind="openai", **kw):
    return make_client(ProviderConfig(name="test", kind=kind,
                                     model="gemini/gemini-2.5-flash" if kind == "litellm" else "model",
                                     base_url="http://localhost:8081/v1" if kind == "openai" else None, **kw))


@pytest.mark.parametrize("kind", ["openai", "anthropic", "litellm"])
def test_extra_json_retries_with_feedback_and_counts_usage(monkeypatch, kind):
    requests, streams = fake_completion(monkeypatch, [
        [chunk('{"ok":true}{"ok":false}', "stop", {"prompt_tokens": 10, "completion_tokens": 5})],
        [chunk('{"ok":true}', "stop", {"prompt_tokens": 20, "completion_tokens": 8})],
    ])
    result = client(kind).complete_json("system", MESSAGES, SCHEMA)
    assert result.data == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (30, 13)
    assert "Extra data" in requests[1]["messages"][-1]["content"]
    assert "exactly one complete JSON object" in requests[1]["messages"][-1]["content"]
    assert len(requests[0]["messages"]) == 2
    assert requests[0]["max_tokens"] == 32768
    assert MESSAGES == [{"role": "user", "content": "add sensors"}]
    assert all(s.closed for s in streams)
    assert all(r["num_retries"] == r["max_retries"] == 0 for r in requests)


def test_exhausted_json_retries_preserve_usage_and_close_streams(monkeypatch):
    requests, streams = fake_completion(monkeypatch, [
        [chunk("bad", "stop", {"prompt_tokens": 10, "completion_tokens": 5})],
        [chunk("bad", "stop", {"prompt_tokens": 20, "completion_tokens": 8})],
    ])
    with pytest.raises(MalformedOutput) as caught:
        client().complete_json("system", MESSAGES, SCHEMA)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (30, 13)
    assert len(requests) == 2 and all(s.closed for s in streams)


@pytest.mark.parametrize("finish,message", [("content_filter", "declined"), ("refusal", "declined")])
def test_terminal_finish_is_not_retried(monkeypatch, finish, message):
    requests, streams = fake_completion(monkeypatch, [[
        chunk('{"ok":true}', finish, {"prompt_tokens": 10, "completion_tokens": 5})]])
    with pytest.raises(LLMError, match=message) as caught:
        client().complete_json("system", MESSAGES, SCHEMA)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (10, 5)
    assert len(requests) == 1 and streams[0].closed


def test_truncated_reply_retries_complete_answer_with_more_room(monkeypatch):
    requests, streams = fake_completion(monkeypatch, [
        [chunk('{"ok":', "length", {"prompt_tokens": 6000, "completion_tokens": 8000})],
        [chunk('{"ok":true}', "stop", {"prompt_tokens": 6100, "completion_tokens": 10})],
    ])
    result = client(context_tokens=32768).complete_json("system", MESSAGES, SCHEMA, max_tokens=8000)
    assert result.data == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (12100, 8010)
    assert [r["max_tokens"] for r in requests] == [8000, 16000]
    feedback = requests[1]["messages"][-1]["content"]
    assert "compact JSON" in feedback and "every required operation" in feedback
    assert '{"ok":' not in feedback
    assert MESSAGES == [{"role": "user", "content": "add sensors"}]
    assert all(s.closed for s in streams)


@pytest.mark.parametrize("prompt,expected", [(4000, 3168), (5500, 2048)])
def test_truncation_retry_respects_small_context_headroom(monkeypatch, prompt, expected):
    requests, _ = fake_completion(monkeypatch, [
        [chunk("partial", "length", {"prompt_tokens": prompt, "completion_tokens": 2048})],
        [chunk('{"ok":true}', "stop")],
    ])
    client(context_tokens=8192).complete_json("system", MESSAGES, SCHEMA, max_tokens=2048)
    assert requests[1]["max_tokens"] == expected


def test_truncation_retry_respects_native_output_limit(monkeypatch):
    monkeypatch.setattr(litellm, "get_model_info", lambda _: {"max_output_tokens": 9000})
    requests, _ = fake_completion(monkeypatch, [
        [chunk("partial", "length", {"prompt_tokens": 2000, "completion_tokens": 8000})],
        [chunk('{"ok":true}', "stop")],
    ])
    client("litellm", context_tokens=100000).complete_json("system", MESSAGES, SCHEMA, max_tokens=8000)
    assert requests[1]["max_tokens"] == 9000


def test_default_budget_respects_native_model_output_maximum(monkeypatch):
    monkeypatch.setattr(litellm, "get_model_info", lambda _: {"max_output_tokens": 8192})
    requests, _ = fake_completion(monkeypatch, [[chunk('{"ok":true}', "stop")]])
    client("litellm").complete_json("system", MESSAGES, SCHEMA)
    assert requests[0]["max_tokens"] == 8192


def test_exhausted_truncation_retry_counts_usage_and_never_accepts_partial_answer(monkeypatch):
    requests, streams = fake_completion(monkeypatch, [
        [chunk('{"ok":true}', "length", {"prompt_tokens": 6000, "completion_tokens": 8000})],
        [chunk('{"ok":true}', "length", {"prompt_tokens": 6100, "completion_tokens": 16000})],
    ])
    with pytest.raises(LLMError, match="16,000-token limit") as caught:
        client(context_tokens=32768).complete_json("system", MESSAGES, SCHEMA, max_tokens=8000)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (12100, 24000)
    assert len(requests) == 2 and all(s.closed for s in streams)


@pytest.mark.parametrize("error,message", [
    (litellm.AuthenticationError("bad key", "test", "model"), "authentication failed"),
    (litellm.RateLimitError("slow down", "test", "model"), "rate limited"),
    (litellm.Timeout("slow", "model", "test"), "timed out"),
    (litellm.APIConnectionError("offline", "test", "model"), "Cannot reach model endpoint"),
    (litellm.BadRequestError("bad schema", "model", "test"), "bad schema"),
])
def test_provider_errors_are_normalized_without_retry(monkeypatch, error, message):
    requests, _ = fake_completion(monkeypatch, [error])
    with pytest.raises(LLMError, match=message):
        client().complete_json("system", MESSAGES, SCHEMA)
    assert len(requests) == 1


def test_midstream_failure_preserves_reported_usage(monkeypatch):
    _, streams = fake_completion(monkeypatch, [[
        chunk('{"ok":', usage={"prompt_tokens": 10, "completion_tokens": 2}),
        litellm.APIConnectionError("offline", "test", "model"),
    ]])
    with pytest.raises(LLMError) as caught:
        client().complete_json("system", MESSAGES, SCHEMA)
    assert (caught.value.input_tokens, caught.value.output_tokens) == (10, 2)
    assert streams[0].closed


def test_cancellation_closes_stream_and_is_not_retried(monkeypatch):
    token = CancelToken()
    requests, streams = fake_completion(monkeypatch, [[chunk('{"ok":'), chunk("true}")]])
    with pytest.raises(Cancelled):
        client().complete_json("system", MESSAGES, SCHEMA, cancel=token, on_progress=lambda _: token.cancel())
    assert len(requests) == 1 and streams[0].closed
    requests.clear()
    with pytest.raises(Cancelled):
        client().complete_json("system", MESSAGES, SCHEMA, cancel=token)
    assert not requests


def test_images_attach_once_and_progress_counts_characters(monkeypatch):
    requests, _ = fake_completion(monkeypatch, [[chunk('{"ok":'), chunk("true}", "stop")]])
    progress = []
    client(supports_images=True).complete_json(
        "system", [*MESSAGES, {"role": "user", "content": "and now?"}], SCHEMA,
        images=[ImageInput(b"image")], on_progress=progress.append)
    assert requests[0]["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert requests[0]["messages"][2]["content"] == "and now?"
    assert progress == [6, 11]


def test_text_only_model_rejects_images_before_call(monkeypatch):
    requests, _ = fake_completion(monkeypatch, [])
    with pytest.raises(LLMError, match="does not accept images"):
        client().complete_json("system", MESSAGES, SCHEMA, images=[ImageInput(b"image")])
    assert not requests


def test_schema_tool_fallback_arguments_are_parsed(monkeypatch):
    fake_completion(monkeypatch, [[
        chunk(tool_calls=[{"index": 0, "function": {"name": "json_tool_call", "arguments": '{"ok":'}}]),
        chunk(finish="tool_calls", tool_calls=[{"index": 0, "function": {"arguments": "true}"}}]),
    ]])
    progress = []
    assert client("anthropic").complete_json("system", MESSAGES, SCHEMA, on_progress=progress.append).data == {"ok": True}
    assert progress == [6, 11]


def test_alternative_choices_are_not_concatenated(monkeypatch):
    response = ModelResponseStream(choices=[
        {"index": 0, "delta": {"content": '{"ok":true}'}, "finish_reason": "stop"},
        {"index": 1, "delta": {"content": '{"ok":false}'}, "finish_reason": "stop"}])
    fake_completion(monkeypatch, [[response]])
    assert client().complete_json("system", MESSAGES, SCHEMA).data == {"ok": True}


def test_existing_configs_and_native_routes_keep_their_options():
    options = {"provider": {"require_parameters": True}, "reasoning": {"enabled": False}}
    c = client(request_options=options)
    params = c._params("system", MESSAGES, SCHEMA, None, 100)
    assert params["custom_llm_provider"] == "openai" and params["extra_body"] == options
    assert params["api_key"] == "unused"
    params["extra_body"]["provider"]["require_parameters"] = False
    assert options["provider"]["require_parameters"] is True
    native = client("litellm", request_options={"temperature": .2})
    params = native._params("system", MESSAGES, SCHEMA, None, 100)
    assert "custom_llm_provider" not in params and params["temperature"] == .2
    assert params["model"] == "gemini/gemini-2.5-flash"
    anthropic = client("anthropic", request_options={"fallbacks": False, "effort": "low"})
    params = anthropic._params("system", MESSAGES, SCHEMA, None, 100)
    assert params["output_config"]["effort"] == "low" and "fallbacks" not in params
    assert "reasoning_effort" not in params
    assert "extra_headers" not in params
    with pytest.raises(LLMError, match="native refusal fallbacks"):
        client("anthropic", request_options={"fallbacks": True})._params("system", MESSAGES, SCHEMA, None, 100)


def test_multiple_objects_are_rejected_instead_of_dropping_operations():
    with pytest.raises(MalformedOutput, match="Extra data"):
        parse_json_text('{"operations":[1]}\n{"operations":[2]}')


def test_real_litellm_openai_transport_preserves_wire_request(monkeypatch):
    from openai import OpenAI

    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        payload = {"id": "chat-test", "object": "chat.completion.chunk", "created": 0, "model": "local",
                   "choices": [{"index": 0, "delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        return httpx.Response(200, text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    real_complete = litellm.completion
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = OpenAI(api_key="unused", base_url="http://local/v1", http_client=http_client)
        monkeypatch.setattr(litellm, "completion", lambda **params: real_complete(**params, client=sdk))
        result = client(request_options={"provider": {"require_parameters": True}}).complete_json("system", MESSAGES, SCHEMA)
    assert result.data == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    assert requests[0]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert requests[0]["max_tokens"] == 32768
    assert requests[0]["provider"] == {"require_parameters": True}


def test_non_strict_openai_transport_preserves_optional_update_fields(monkeypatch):
    from openai import OpenAI
    from workbench.agent.correction import step_schema
    from workbench.operations import OperationList

    schema = step_schema()
    reply = {"thought": "Correct only the unit", "action": "propose", "operations": [
        {"op": "update_point", "id": "pt-test", "unit": "unit:PSI"},
        {"op": "update_point", "id": "pt-clear", "unit": None},
    ]}
    requests = []

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        response_schema = body["response_format"]["json_schema"]
        if response_schema["strict"]:
            return httpx.Response(400, json={"error": {"message": "Missing required query",
                                                     "type": "invalid_request_error"}})
        payload = {"id": "chat-test", "object": "chat.completion.chunk", "created": 0, "model": "model",
                   "choices": [{"index": 0, "delta": {"content": json.dumps(reply)}, "finish_reason": "stop"}]}
        return httpx.Response(200, text=f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    real_complete = litellm.completion
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = OpenAI(api_key="unused", base_url="https://api.openai.com/v1", http_client=http_client)
        monkeypatch.setattr(litellm, "completion", lambda **params: real_complete(**params, client=sdk))
        result = client(strict_schema=False).complete_json("system", MESSAGES, schema)
    assert requests[0]["response_format"]["json_schema"]["schema"] == schema
    assert result.data == reply
    operations = OperationList.validate_python(result.data["operations"])
    assert operations[0].provided() == {"unit"}
    assert operations[0].unit == "unit:PSI"
    assert operations[1].provided() == {"unit"}
    assert operations[1].unit is None


@pytest.mark.parametrize("status", [400, 429])
def test_real_litellm_transport_errors_are_not_retried(monkeypatch, status):
    from openai import OpenAI

    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "Rejected", "type": "invalid_request_error"}})

    real_complete = litellm.completion
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        sdk = OpenAI(api_key="unused", base_url="http://local/v1", http_client=http_client)
        monkeypatch.setattr(litellm, "completion", lambda **params: real_complete(**params, client=sdk))
        with pytest.raises(LLMError):
            client().complete_json("system", MESSAGES, SCHEMA)
    assert len(requests) == 1


def test_real_litellm_anthropic_transport_preserves_native_options(monkeypatch):
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    requests = []
    model = "claude-opus-5-5"
    events = [
        {"type": "message_start", "message": {"id": "msg-test", "type": "message", "role": "assistant",
            "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": '{"ok":true}'}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 5}},
        {"type": "message_stop"},
    ]

    def handler(request):
        requests.append(request)
        sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    real_complete = litellm.completion
    monkeypatch.setenv("WORKBENCH_TEST_ANTHROPIC_KEY", "test-key")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        transport = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "completion", lambda **params: real_complete(**params, client=transport))
        c = make_client(ProviderConfig(name="claude", kind="anthropic", model=model,
                                     api_key_env="WORKBENCH_TEST_ANTHROPIC_KEY"))
        result = c.complete_json("system", MESSAGES, SCHEMA)
    assert result.data == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    body = json.loads(requests[0].content)
    assert "fallbacks" not in body and "extra_body" not in body
    assert "thinking" not in body
    assert body["output_config"]["effort"] == "medium"
    assert body["output_format"]["schema"] == SCHEMA


def test_real_litellm_native_gemini_route_translates_schema(monkeypatch):
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    requests = []

    def handler(request):
        requests.append(request)
        payload = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": '{"ok":true}'}]},
                                   "finishReason": "STOP"}],
                   "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15}}
        return httpx.Response(200, text=f"data: {json.dumps(payload)}\n\n", headers={"content-type": "text/event-stream"})

    real_complete = litellm.completion
    monkeypatch.setenv("WORKBENCH_TEST_GEMINI_KEY", "test-key")
    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        transport = HTTPHandler(client=http_client)
        monkeypatch.setattr(litellm, "completion", lambda **params: real_complete(**params, client=transport))
        result = client("litellm", api_key_env="WORKBENCH_TEST_GEMINI_KEY").complete_json("system", MESSAGES, SCHEMA)
    assert result.data == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (10, 5)
    body = json.loads(requests[0].content)
    assert body["generationConfig"]["response_mime_type"] == "application/json"
    assert body["generationConfig"]["response_json_schema"] == SCHEMA
