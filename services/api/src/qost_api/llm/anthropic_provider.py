"""``LLM_PROVIDER=anthropic``: the Messages API through the official ``anthropic`` SDK (1.x).

* Model ``LLM_MODEL`` (default ``claude-sonnet-5-5``); one hard timeout for the whole call
  (``LLM_TIMEOUT_S``, 20 s), no SDK retries (the report falls back to the template instead).
* Temperature: the SDK 1.x request has no sampling parameters and current models reject
  non-default ones, so «temperature 0» (SPEC §11.5) is approximated by a strict prompt, a low
  ``output_config.effort`` (``LLM_EFFORT``) and the number check; the ollama provider sends
  ``temperature: 0``.
* Refusals: server-side fallback (``fallbacks: "default"``, beta
  ``server-side-fallback-2026-07-01``) unless ``LLM_FALLBACKS=off``; a final ``refusal`` stop
  reason raises :class:`LlmRefusalError`. The model that answered is reported back.
* Tool use: ``tool_choice`` stays ``auto`` (forced tool choice is rejected by current models);
  assistant turns are replayed with their original content blocks.
* ``OFFLINE=true``: :meth:`complete` raises :class:`LlmOfflineError` before touching the network
  (the factory does not even build this provider offline).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Literal, cast

import anthropic

from qost_api.llm.base import (
    AssistantTurn,
    LlmError,
    LlmOfflineError,
    LlmRefusalError,
    LlmRequest,
    LlmResponse,
    LlmTimeoutError,
    ToolCall,
    ToolResultsTurn,
    UserTurn,
)

if TYPE_CHECKING:
    import httpx2
    from anthropic.types.beta import BetaMessage, BetaMessageParam

Effort = Literal["low", "medium", "high", "xhigh", "max"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout_s: float = 20.0,
        effort: Effort | None = "low",
        fallbacks: bool = True,
        offline: bool = False,
        base_url: str | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.model: str | None = model
        self.timeout_s = timeout_s
        self.effort = effort
        self.fallbacks = fallbacks
        self.offline = offline
        http_client = None
        if transport is not None:
            http_client = anthropic.DefaultAsyncHttpxClient(transport=transport)
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_s,
            max_retries=0,
            http_client=http_client,
        )

    @property
    def available(self) -> bool:
        return not self.offline

    @property
    def reason(self) -> str | None:
        return "OFFLINE=true forbids external LLM calls" if self.offline else None

    async def aclose(self) -> None:
        await self._client.close()

    # ------------------------------------------------------------------ request

    @staticmethod
    def _messages(request: LlmRequest) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for turn in request.messages:
            if isinstance(turn, UserTurn):
                out.append({"role": "user", "content": turn.text})
            elif isinstance(turn, AssistantTurn):
                if turn.raw is not None:
                    content: Any = turn.raw
                else:
                    blocks: list[dict[str, Any]] = []
                    if turn.text:
                        blocks.append({"type": "text", "text": turn.text})
                    blocks.extend(
                        {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                        for c in turn.tool_calls
                    )
                    content = blocks
                out.append({"role": "assistant", "content": content})
            elif isinstance(turn, ToolResultsTurn):
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": r.call_id,
                                "content": r.content,
                                "is_error": r.is_error,
                            }
                            for r in turn.results
                        ],
                    }
                )
        return out

    async def complete(self, request: LlmRequest) -> LlmResponse:
        if self.offline:
            raise LlmOfflineError("OFFLINE=true: the anthropic provider may not be called")
        messages = cast("list[BetaMessageParam]", self._messages(request))
        tools: Any = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in request.tools
        ]
        try:
            async with asyncio.timeout(self.timeout_s):
                message: BetaMessage = await self._client.beta.messages.create(
                    model=cast(Any, self.model),
                    max_tokens=request.max_tokens,
                    system=request.system,
                    messages=messages,
                    tools=tools if request.tools else anthropic.omit,
                    output_config={"effort": self.effort} if self.effort else anthropic.omit,
                    fallbacks="default" if self.fallbacks else anthropic.omit,
                    betas=[FALLBACK_BETA] if self.fallbacks else anthropic.omit,
                )
        except TimeoutError as exc:
            raise LlmTimeoutError(f"no answer within {self.timeout_s:g} s") from exc
        except anthropic.APITimeoutError as exc:
            raise LlmTimeoutError(f"no answer within {self.timeout_s:g} s") from exc
        except anthropic.APIStatusError as exc:
            raise LlmError(f"anthropic API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LlmError(f"anthropic API unreachable: {exc}") from exc
        if message.stop_reason == "refusal":
            raise LlmRefusalError("the model declined the request")
        text = "".join(b.text for b in message.content if b.type == "text")
        calls = tuple(
            ToolCall(b.id, b.name, dict(b.input)) for b in message.content if b.type == "tool_use"
        )
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
        return LlmResponse(
            text=text,
            tool_calls=calls,
            stop_reason=str(message.stop_reason or ""),
            model=str(message.model),
            provider=self.name,
            usage=usage,
            raw=list(message.content),
        )


__all__ = ["FALLBACK_BETA", "AnthropicProvider", "Effort"]
