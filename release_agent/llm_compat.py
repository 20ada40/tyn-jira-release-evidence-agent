"""Adapter for OpenAI-compatible chat-completions endpoints (Gemini, Groq, OpenRouter, Ollama).

The agent loop speaks the Anthropic Messages shape (content blocks, tool_use / tool_result).
This adapter exposes the same `messages.create(...)` surface and translates each call to
`POST {base_url}/chat/completions`, so the loop, tracing and validation are unchanged whichever
model is used.
"""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace
from typing import Any

import requests

# provider -> (base URL, environment variable holding the key; None = no key needed)
PROVIDERS: dict[str, tuple[str, str | None]] = {
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
}
RETRY_STATUS = {429, 500, 502, 503, 504}
STOP_REASONS = {"length": "max_tokens", "content_filter": "refusal", "tool_calls": "tool_use"}


class LLMError(RuntimeError):
    """A model call failed after retries."""


def _get(obj: Any, name: str, default: Any = None) -> Any:
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _clean_schema(schema: Any) -> Any:
    """Drop `additionalProperties: false`, which some compatible endpoints reject."""
    if isinstance(schema, dict):
        return {k: _clean_schema(v) for k, v in schema.items()
                if not (k == "additionalProperties" and v is False)}
    if isinstance(schema, list):
        return [_clean_schema(v) for v in schema]
    return schema


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function",
             "function": {"name": t["name"], "description": t.get("description", ""),
                          "parameters": _clean_schema(t["input_schema"])}} for t in tools]


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for message in messages:
        role, content = message["role"], message["content"]
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if role == "assistant":
            texts, calls = [], []
            for block in content:
                kind = _get(block, "type")
                if kind == "text":
                    texts.append(_get(block, "text", ""))
                elif kind == "tool_use":
                    # Send back the provider's own tool-call object when we have it, so
                    # provider-specific fields (e.g. reasoning signatures) survive the round trip.
                    calls.append(_get(block, "raw") or {
                        "id": _get(block, "id"), "type": "function",
                        "function": {"name": _get(block, "name"),
                                     "arguments": json.dumps(_get(block, "input") or {})}})
            entry: dict[str, Any] = {"role": "assistant", "content": "\n".join(texts) or None}
            if calls:
                entry["tool_calls"] = calls
            out.append(entry)
            continue
        texts = []
        for block in content:
            kind = _get(block, "type")
            if kind == "tool_result":
                out.append({"role": "tool", "tool_call_id": _get(block, "tool_use_id"),
                            "content": _get(block, "content", "")})
            elif kind == "text":
                texts.append(_get(block, "text", ""))
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})
    return out


def from_openai_response(data: dict, requested_model: str) -> SimpleNamespace:
    choices = data.get("choices") or []
    if not choices:
        raise LLMError(f"model returned no choices: {json.dumps(data)[:300]}")
    choice = choices[0]
    message = choice.get("message") or {}
    blocks: list[SimpleNamespace] = []
    if message.get("content"):
        blocks.append(SimpleNamespace(type="text", text=message["content"]))
    for n, call in enumerate(message.get("tool_calls") or []):
        function = call.get("function") or {}
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = None  # the loop reports this back to the model as an invalid input
        call.setdefault("id", f"call_{n}")
        call.setdefault("type", "function")
        blocks.append(SimpleNamespace(type="tool_use", id=call["id"], name=function.get("name"),
                                      input=arguments, raw=call))
    finish = choice.get("finish_reason")
    stop_reason = "tool_use" if any(b.type == "tool_use" for b in blocks) \
        else STOP_REASONS.get(finish, "end_turn")
    usage = data.get("usage") or {}
    return SimpleNamespace(
        content=blocks, stop_reason=stop_reason, model=data.get("model") or requested_model,
        usage=SimpleNamespace(input_tokens=usage.get("prompt_tokens", 0),
                              output_tokens=usage.get("completion_tokens", 0),
                              cache_creation_input_tokens=0, cache_read_input_tokens=0))


class OpenAICompatLLM:
    def __init__(self, base_url: str, api_key: str | None, *, session: Any = None,
                 max_retries: int = 6, timeout: float = 600.0, sleep=time.sleep):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.session = session or requests.Session()
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"
        self.max_retries = max_retries
        self.timeout = timeout
        self._sleep = sleep
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, *, model: str, system: str, messages: list[dict], tools: list[dict],
                max_tokens: int = 16000, **_ignored: Any) -> SimpleNamespace:
        body = {"model": model, "messages": to_openai_messages(system, messages),
                "tools": to_openai_tools(tools), "tool_choice": "auto", "max_tokens": max_tokens}
        last = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(self.url, headers=self.headers, json=body,
                                             timeout=self.timeout)
            except requests.RequestException as exc:
                last = f"connection error: {exc}"
            else:
                if response.status_code == 200:
                    return from_openai_response(response.json(), model)
                last = f"HTTP {response.status_code}: {response.text[:500]}"
                if response.status_code not in RETRY_STATUS:
                    raise LLMError(last)
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.replace(".", "", 1).isdigit():
                    self._sleep(min(float(retry_after) + 1, 90))
                    continue
            if attempt < self.max_retries:
                self._sleep(min(5 * 2 ** attempt, 65))
        raise LLMError(f"model call failed after {self.max_retries + 1} attempts: {last}")


def build_compat_llm(provider: str) -> OpenAICompatLLM:
    """provider is a key of PROVIDERS, or 'custom' (LLM_BASE_URL + optional LLM_API_KEY)."""
    if provider == "custom":
        base_url, key = os.environ.get("LLM_BASE_URL", "").strip(), os.environ.get("LLM_API_KEY")
        if not base_url:
            raise LLMError("provider 'custom' needs LLM_BASE_URL in the environment")
        return OpenAICompatLLM(base_url, key)
    base_url, key_name = PROVIDERS[provider]
    key = os.environ.get(key_name, "").strip() if key_name else None
    if key_name and not key:
        raise LLMError(f"provider '{provider}' needs {key_name} in the environment / .env")
    return OpenAICompatLLM(base_url, key)
