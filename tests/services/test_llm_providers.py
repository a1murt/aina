"""T-LLM: provider abstraction (SPEC §11.5) — ``none``, the OFFLINE guard (no network, checked
with a transport that fails the test when called), the anthropic Messages API request/response
and tool use through a mock transport, ollama, timeouts."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from qost_api.llm import (
    AssistantTurn,
    LlmError,
    LlmOfflineError,
    LlmRefusalError,
    LlmRequest,
    LlmSettings,
    LlmTimeoutError,
    LlmUnavailableError,
    NoneProvider,
    ToolCall,
    ToolResult,
    ToolResultsTurn,
    ToolSpec,
    UserTurn,
    create_provider,
)
from qost_api.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider
from qost_api.llm.ollama_provider import OllamaProvider

TOOL = ToolSpec(
    "get_kpi",
    "KPI of a line",
    {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
)
REQUEST = LlmRequest(system="sys", messages=(UserTurn("hello"),), tools=(TOOL,), max_tokens=500)


def failing_transport() -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        pytest.fail(f"network call under OFFLINE: {request.method} {request.url}")

    return httpx2.MockTransport(handler)


def settings(**values: Any) -> LlmSettings:
    return LlmSettings(**values)


def anthropic_reply(content: list[dict[str, Any]], stop: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    }


def recording(
    reply: dict[str, Any] | Callable[[httpx2.Request], httpx2.Response], seen: list[Any]
) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        if callable(reply):
            return reply(request)
        return httpx2.Response(200, json=reply)

    return httpx2.MockTransport(handler)


async def test_none_provider_is_unavailable() -> None:
    provider = create_provider(settings(llm_provider="none"))
    assert isinstance(provider, NoneProvider)
    assert not provider.available
    assert provider.reason == "LLM_PROVIDER=none"
    with pytest.raises(LlmUnavailableError):
        await provider.complete(REQUEST)


async def test_offline_turns_anthropic_into_none_before_any_network() -> None:
    provider = create_provider(
        settings(llm_provider="anthropic", offline=True, anthropic_api_key="sk-test"),
        transport=failing_transport(),
    )
    assert isinstance(provider, NoneProvider)
    assert provider.reason is not None
    assert "OFFLINE" in provider.reason
    with pytest.raises(LlmUnavailableError):
        await provider.complete(REQUEST)


async def test_anthropic_provider_refuses_offline_without_network() -> None:
    provider = AnthropicProvider(
        api_key="sk-test", model="claude-sonnet-5-5", offline=True, transport=failing_transport()
    )
    try:
        assert not provider.available
        with pytest.raises(LlmOfflineError):
            await provider.complete(REQUEST)
    finally:
        await provider.aclose()


def test_effort_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_EFFORT", "")
    assert LlmSettings().llm_effort is None
    monkeypatch.setenv("LLM_EFFORT", "medium")
    assert LlmSettings().llm_effort == "medium"


async def test_anthropic_without_key_is_unavailable() -> None:
    provider = create_provider(
        settings(llm_provider="anthropic", offline=False, anthropic_api_key=None)
    )
    assert isinstance(provider, NoneProvider)
    assert provider.reason == "ANTHROPIC_API_KEY is empty"


async def test_anthropic_messages_request_and_tool_use() -> None:
    seen: list[httpx2.Request] = []
    reply = anthropic_reply(
        [
            {"type": "text", "text": "Смотрю KPI."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_kpi", "input": {"code": "PAINT-1"}},
        ],
        stop="tool_use",
    )
    provider = create_provider(
        settings(llm_provider="anthropic", offline=False, anthropic_api_key="sk-test"),
        transport=recording(reply, seen),
    )
    try:
        assert isinstance(provider, AnthropicProvider)
        assert provider.available
        response = await provider.complete(REQUEST)
    finally:
        await provider.aclose()
    assert response.text == "Смотрю KPI."
    assert response.tool_calls == (ToolCall("toolu_1", "get_kpi", {"code": "PAINT-1"}),)
    assert response.stop_reason == "tool_use"
    assert response.model == "claude-sonnet-5-5"
    assert response.usage == {"input_tokens": 12, "output_tokens": 7}
    request = seen[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "sk-test"
    assert FALLBACK_BETA in request.headers["anthropic-beta"]
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["system"] == "sys"
    assert body["max_tokens"] == 500
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    assert body["tools"][0]["name"] == "get_kpi"
    assert "input_schema" in body["tools"][0]
    assert body["output_config"] == {"effort": "low"}
    assert body["fallbacks"] == "default"
    assert "temperature" not in body
    assert "tool_choice" not in body  # auto; no sampling params


async def test_anthropic_replays_tool_turns_unchanged() -> None:
    seen: list[httpx2.Request] = []
    provider = AnthropicProvider(
        api_key="sk-test",
        model="claude-sonnet-5-5",
        fallbacks=False,
        transport=recording(anthropic_reply([{"type": "text", "text": "OEE 79,0%"}]), seen),
    )
    first = AssistantTurn("", (ToolCall("toolu_1", "get_kpi", {"code": "PAINT-1"}),), raw=None)
    request = LlmRequest(
        system="sys",
        messages=(
            UserTurn("OEE окраски?"),
            first,
            ToolResultsTurn((ToolResult("toolu_1", "get_kpi", '{"oee": 0.79}'),)),
        ),
        tools=(TOOL,),
    )
    try:
        response = await provider.complete(request)
    finally:
        await provider.aclose()
    assert response.text == "OEE 79,0%"
    body = json.loads(seen[0].content)
    assert "anthropic-beta" not in seen[0].headers
    assert "fallbacks" not in body
    assert body["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "get_kpi", "input": {"code": "PAINT-1"}}
        ],
    }
    assert body["messages"][2]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": '{"oee": 0.79}',
        "is_error": False,
    }


async def test_anthropic_refusal_and_errors() -> None:
    refusal = anthropic_reply([], stop="refusal")
    provider = AnthropicProvider(api_key="k", model="m", transport=recording(refusal, []))
    with pytest.raises(LlmRefusalError):
        await provider.complete(REQUEST)
    await provider.aclose()

    def overloaded(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "busy"}}
        )

    provider = AnthropicProvider(api_key="k", model="m", transport=recording(overloaded, []))
    with pytest.raises(LlmError, match="529"):
        await provider.complete(REQUEST)
    await provider.aclose()


async def test_anthropic_hard_timeout() -> None:
    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(1.0)
        return httpx2.Response(200, json=anthropic_reply([]))

    provider = AnthropicProvider(
        api_key="k", model="m", timeout_s=0.05, transport=httpx2.MockTransport(slow)
    )
    try:
        with pytest.raises(LlmTimeoutError):
            await provider.complete(REQUEST)
    finally:
        await provider.aclose()


async def test_ollama_chat_request_temperature_zero_and_tools() -> None:
    seen: list[httpx2.Request] = []
    reply = {
        "model": "qwen2.5:7b",
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"function": {"name": "get_kpi", "arguments": {"code": "WELD-1"}}}],
        },
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 30,
        "eval_count": 5,
    }
    provider = create_provider(
        settings(
            llm_provider="ollama",
            llm_model="qwen2.5:7b",
            offline=True,
            ollama_url="http://ollama.local:11434/",
        ),
        transport=recording(reply, seen),
    )
    try:
        assert isinstance(provider, OllamaProvider)
        assert provider.available  # allowed offline
        response = await provider.complete(REQUEST)
        follow = await provider.complete(
            LlmRequest(
                system="sys",
                messages=(
                    UserTurn("hello"),
                    response.as_turn(),
                    ToolResultsTurn((ToolResult("call_0", "get_kpi", "{}"),)),
                ),
            )
        )
    finally:
        await provider.aclose()
    assert response.tool_calls == (ToolCall("call_0", "get_kpi", {"code": "WELD-1"}),)
    assert follow.model == "qwen2.5:7b"
    body = json.loads(seen[0].content)
    assert str(seen[0].url) == "http://ollama.local:11434/api/chat"
    assert body["stream"] is False
    assert body["options"]["temperature"] == 0.0
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["tools"][0]["function"]["name"] == "get_kpi"
    replay = json.loads(seen[1].content)["messages"]
    assert replay[2]["tool_calls"][0]["function"]["name"] == "get_kpi"
    assert replay[3] == {"role": "tool", "tool_name": "get_kpi", "content": "{}"}


async def test_ollama_errors_and_timeout() -> None:
    def down(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    provider = OllamaProvider(url="http://o", model="m", transport=httpx2.MockTransport(down))
    with pytest.raises(LlmError, match="unreachable"):
        await provider.complete(REQUEST)
    await provider.aclose()

    def missing(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"error": "model 'm' not found"})

    provider = OllamaProvider(url="http://o", model="m", transport=httpx2.MockTransport(missing))
    with pytest.raises(LlmError, match="404"):
        await provider.complete(REQUEST)
    await provider.aclose()

    def timeout(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    provider = OllamaProvider(url="http://o", model="m", transport=httpx2.MockTransport(timeout))
    with pytest.raises(LlmTimeoutError):
        await provider.complete(REQUEST)
    await provider.aclose()
