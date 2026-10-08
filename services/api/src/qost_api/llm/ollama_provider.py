"""``LLM_PROVIDER=ollama``: a local model through the Ollama HTTP API (``POST /api/chat``).

Allowed with ``OFFLINE=true`` (the model runs on the plant's own host, ``OLLAMA_URL``).
``LLM_MODEL`` must name a model pulled into that Ollama (e.g. ``qwen2.5:7b-instruct``).
Requests are non-streaming, ``temperature: 0``, one hard timeout (``LLM_TIMEOUT_S``).
Tool calls use Ollama's function format; tool results go back as ``role: tool`` messages.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2

from qost_api.llm.base import (
    AssistantTurn,
    LlmError,
    LlmRequest,
    LlmResponse,
    LlmTimeoutError,
    ToolCall,
    ToolResultsTurn,
    UserTurn,
)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        url: str,
        model: str,
        timeout_s: float = 20.0,
        temperature: float = 0.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.model: str | None = model
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.temperature = temperature
        self._client = httpx2.AsyncClient(timeout=timeout_s, transport=transport)

    @property
    def available(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return None

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _messages(request: LlmRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for turn in request.messages:
            if isinstance(turn, UserTurn):
                out.append({"role": "user", "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                if isinstance(turn.raw, dict):
                    out.append(turn.raw)
                    continue
                msg: dict[str, Any] = {"role": "assistant", "content": turn.text}
                if turn.tool_calls:
                    msg["tool_calls"] = [
                        {"function": {"name": c.name, "arguments": c.arguments}}
                        for c in turn.tool_calls
                    ]
                out.append(msg)
            elif isinstance(turn, ToolResultsTurn):
                out.extend(
                    {"role": "tool", "tool_name": r.name, "content": r.content}
                    for r in turn.results
                )
        return out

    async def complete(self, request: LlmRequest) -> LlmResponse:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(request),
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": request.max_tokens},
        }
        if request.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in request.tools
            ]
        try:
            async with asyncio.timeout(self.timeout_s):
                response = await self._client.post(f"{self.url}/api/chat", json=body)
        except (TimeoutError, httpx2.TimeoutException) as exc:
            raise LlmTimeoutError(f"no answer within {self.timeout_s:g} s") from exc
        except httpx2.HTTPError as exc:
            raise LlmError(f"ollama unreachable: {exc}") from exc
        if response.status_code != 200:
            raise LlmError(f"ollama error {response.status_code}: {response.text[:200]}")
        try:
            data = response.json()
            message = data["message"]
        except (ValueError, KeyError, TypeError) as exc:
            raise LlmError("ollama returned an unexpected body") from exc
        calls: list[ToolCall] = []
        for n, call in enumerate(message.get("tool_calls") or []):
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(str(call.get("id") or f"call_{n}"), str(fn.get("name")), args))
        usage = {
            "input_tokens": int(data.get("prompt_eval_count") or 0),
            "output_tokens": int(data.get("eval_count") or 0),
        }
        return LlmResponse(
            text=str(message.get("content") or ""),
            tool_calls=tuple(calls),
            stop_reason=str(data.get("done_reason") or "stop"),
            model=str(data.get("model") or self.model),
            provider=self.name,
            usage=usage,
            raw=message,
        )


__all__ = ["OllamaProvider"]
