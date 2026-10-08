"""Provider-neutral LLM interface (SPEC §11.5): messages, tool use, errors.

The shift report uses plain text turns; the copilot (later) uses tools: an assistant turn with
``tool_calls`` is followed by a :class:`ToolResultsTurn`. Assistant turns carry the provider's
native content (``raw``) so a conversation is replayed to the same provider unchanged (thinking
blocks and tool-use ids intact).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

ProviderName = Literal["anthropic", "ollama", "none"]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """A read-only tool the model may call (JSON Schema of its arguments)."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class UserTurn:
    text: str


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None
    """Provider-native content of the turn (replayed as is); ``None`` -> built from text/calls."""


@dataclass(frozen=True, slots=True)
class ToolResultsTurn:
    results: tuple[ToolResult, ...]


Turn = UserTurn | AssistantTurn | ToolResultsTurn


@dataclass(frozen=True, slots=True)
class LlmRequest:
    system: str
    messages: tuple[Turn, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int = 2048


@dataclass(frozen=True, slots=True)
class LlmResponse:
    text: str
    tool_calls: tuple[ToolCall, ...]
    stop_reason: str
    model: str
    """The model that produced the answer (a server-side fallback may differ from the request)."""
    provider: str
    usage: dict[str, int] = field(default_factory=dict)
    raw: Any = None

    def as_turn(self) -> AssistantTurn:
        return AssistantTurn(self.text, self.tool_calls, self.raw)


class LlmError(Exception):
    """The provider gave no usable answer; callers fall back (template report, polite refusal)."""

    kind = "error"


class LlmUnavailableError(LlmError):
    kind = "unavailable"


class LlmOfflineError(LlmUnavailableError):
    """``OFFLINE=true`` forbids this provider; raised before any network call (NFR-04/05)."""

    kind = "offline"


class LlmTimeoutError(LlmError):
    kind = "timeout"


class LlmRefusalError(LlmError):
    kind = "refusal"


@runtime_checkable
class LlmProvider(Protocol):
    name: str
    model: str | None

    @property
    def available(self) -> bool: ...

    @property
    def reason(self) -> str | None:
        """Why the provider is unavailable (``None`` when available)."""
        ...

    async def complete(self, request: LlmRequest) -> LlmResponse: ...

    async def aclose(self) -> None: ...


class NoneProvider:
    """``LLM_PROVIDER=none`` (or a provider that may not run): every call is unavailable."""

    name = "none"
    model: str | None = None

    def __init__(self, reason: str = "LLM_PROVIDER=none") -> None:
        self._reason = reason

    @property
    def available(self) -> bool:
        return False

    @property
    def reason(self) -> str | None:
        return self._reason

    async def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmUnavailableError(self._reason)

    async def aclose(self) -> None:
        return None


__all__ = [
    "AssistantTurn",
    "LlmError",
    "LlmOfflineError",
    "LlmProvider",
    "LlmRefusalError",
    "LlmRequest",
    "LlmResponse",
    "LlmTimeoutError",
    "LlmUnavailableError",
    "NoneProvider",
    "ProviderName",
    "ToolCall",
    "ToolResult",
    "ToolResultsTurn",
    "ToolSpec",
    "Turn",
    "UserTurn",
]
