"""The OpenAI-compatible adapter: message/tool translation, response parsing, retries."""
import json
from types import SimpleNamespace

import pytest

from release_agent.llm_compat import (LLMError, OpenAICompatLLM, from_openai_response,
                                      to_openai_messages, to_openai_tools)

TOOL = {"name": "get_issue", "description": "read an issue",
        "input_schema": {"type": "object", "properties": {"key": {"type": "string"}},
                         "required": ["key"], "additionalProperties": False}}


class FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def completion(tool_calls=None, content=None, finish="stop"):
    return {"model": "m-1", "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            "choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": content,
                                     "tool_calls": tool_calls}}]}


def test_tools_are_translated_and_strict_flag_dropped():
    [tool] = to_openai_tools([TOOL])
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "get_issue"
    assert "additionalProperties" not in tool["function"]["parameters"]
    assert tool["function"]["parameters"]["required"] == ["key"]


def test_response_becomes_tool_use_blocks_and_round_trips():
    raw_call = {"id": "c1", "type": "function", "extra_content": {"sig": "abc"},
                "function": {"name": "get_issue", "arguments": '{"key": "KAN-5"}'}}
    response = from_openai_response(completion(tool_calls=[raw_call], content="looking"), "m-1")
    assert response.stop_reason == "tool_use"
    text, call = response.content
    assert (text.type, text.text) == ("text", "looking")
    assert (call.name, call.input, call.id) == ("get_issue", {"key": "KAN-5"}, "c1")
    assert response.usage.input_tokens == 11 and response.usage.output_tokens == 7

    messages = to_openai_messages("SYS", [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": response.content},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "{}", "is_error": False},
            {"type": "text", "text": "Turns remaining: 3."}]},
    ])
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    assert messages[2]["tool_calls"] == [raw_call]  # provider fields preserved verbatim
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "{}"}
    assert messages[4]["content"] == "Turns remaining: 3."


def test_stop_reasons_and_bad_arguments():
    assert from_openai_response(completion(content="x", finish="length"), "m").stop_reason == "max_tokens"
    assert from_openai_response(completion(content="x", finish="content_filter"), "m").stop_reason == "refusal"
    bad = {"id": "c1", "function": {"name": "get_issue", "arguments": "{not json"}}
    assert from_openai_response(completion(tool_calls=[bad]), "m").content[0].input is None
    with pytest.raises(LLMError):
        from_openai_response({"choices": []}, "m")


def test_retries_rate_limit_then_succeeds():
    session = FakeSession([FakeResponse(429, {"error": "slow down"}, {"Retry-After": "2"}),
                           FakeResponse(200, completion(content="ok"))])
    slept = []
    llm = OpenAICompatLLM("https://example.test/v1/", "k", session=session, sleep=slept.append)
    response = llm.messages.create(model="m-1", system="SYS", tools=[TOOL], max_tokens=100,
                                   messages=[{"role": "user", "content": "hi"}],
                                   thinking={"type": "adaptive"})  # extra kwargs are ignored
    assert response.content[0].text == "ok"
    assert slept == [3.0]
    sent = session.requests[0]
    assert sent["url"] == "https://example.test/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer k"
    assert set(sent["json"]) == {"model", "messages", "tools", "tool_choice", "max_tokens"}


def test_non_retryable_error_raises_immediately():
    session = FakeSession([FakeResponse(400, {"error": "bad model"})])
    llm = OpenAICompatLLM("https://example.test/v1", None, session=session, sleep=lambda s: None)
    with pytest.raises(LLMError, match="HTTP 400"):
        llm.messages.create(model="m", system="s", tools=[], messages=[])
    assert len(session.requests) == 1
