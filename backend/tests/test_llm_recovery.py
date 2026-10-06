import json

import httpx
import pytest

from workbench.config import ProviderConfig
from workbench.llm.base import LLMResult, MalformedOutput, parse_json_text
from workbench.llm.openai_compat import OpenAICompatClient


@pytest.mark.parametrize("base_url", ["https://openrouter.ai/api/v1", "http://localhost:8081/v1"])
@pytest.mark.parametrize("options", [{}, {"provider": {"order": ["example"], "require_parameters": True}}])
def test_extra_json_retries_with_feedback_and_preserves_options(monkeypatch, base_url, options):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        text = '{"action":"propose"}{"action":"search_terms"}' if len(requests) == 1 else '{"action":"propose"}'
        chunk = {"id": "gen-test", "provider": "example", "choices": [
            {"delta": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return httpx.Response(200, text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    real_client = httpx.Client
    monkeypatch.setattr("workbench.llm.openai_compat.httpx.Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    client = OpenAICompatClient(ProviderConfig(
        name="test", kind="openai", model="model", base_url=base_url,
        request_options=options))
    messages = [{"role": "user", "content": "add sensors"}]
    result = client.complete_json("system", messages, {"type": "object"})
    assert result.data == {"action": "propose"}
    assert (result.input_tokens, result.output_tokens) == (20, 10)
    for request in requests:
        if "provider" in options:
            assert request["provider"] == options["provider"]
        else:
            assert "provider" not in request
    assert client.cfg.request_options == options
    assert len(requests[0]["messages"]) == 2
    feedback = requests[1]["messages"][-1]
    assert feedback["role"] == "user"
    assert "Extra data" in feedback["content"]
    assert "exactly one complete JSON object" in feedback["content"]
    assert messages == [{"role": "user", "content": "add sensors"}]


def test_multiple_objects_are_rejected_instead_of_dropping_operations():
    with pytest.raises(MalformedOutput, match="Extra data"):
        parse_json_text('{"operations":[1]}\n{"operations":[2]}')


def test_anthropic_retry_also_receives_feedback(monkeypatch):
    from workbench.llm.anthropic_client import AnthropicClient

    client = AnthropicClient.__new__(AnthropicClient)
    seen = []

    def complete(system, messages, *args):
        seen.append(list(messages))
        if len(seen) == 1:
            raise MalformedOutput("Extra data", 10, 5)
        return LLMResult({"action": "propose"}, "", 20, 8)

    monkeypatch.setattr(client, "_complete_once", complete)
    messages = [{"role": "user", "content": "add sensors"}]
    result = client.complete_json("system", messages, {})
    assert (result.input_tokens, result.output_tokens) == (30, 13)
    assert "Extra data" in seen[1][-1]["content"]
    assert len(messages) == 1
