"""LLM providers for the shift report and the copilot (SPEC §11.5, §4.6).

``create_provider`` picks the provider from ``LLM_PROVIDER`` (``anthropic`` | ``ollama`` |
``none``). ``OFFLINE=true`` allows only ``ollama``/``none``: an ``anthropic`` setting turns into
the ``none`` provider with a warning before anything could reach the network. A missing
``ANTHROPIC_API_KEY`` does the same (the SDK would otherwise look for credentials elsewhere).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import structlog
from pydantic import SecretStr, field_validator

from qost_api.llm.base import (
    AssistantTurn,
    LlmError,
    LlmOfflineError,
    LlmProvider,
    LlmRefusalError,
    LlmRequest,
    LlmResponse,
    LlmTimeoutError,
    LlmUnavailableError,
    NoneProvider,
    ProviderName,
    ToolCall,
    ToolResult,
    ToolResultsTurn,
    ToolSpec,
    Turn,
    UserTurn,
)
from twin_core.settings import TwinSettings

if TYPE_CHECKING:
    import httpx2

log = structlog.get_logger("qost_api.llm")

DEFAULT_MODEL = "claude-sonnet-5-5"


class LlmSettings(TwinSettings):
    """``LLM_*`` environment (SPEC §4.6) plus tuning knobs with safe defaults."""

    llm_provider: ProviderName = "none"
    llm_model: str = DEFAULT_MODEL
    anthropic_api_key: SecretStr | None = None
    anthropic_base_url: str | None = None
    ollama_url: str = "http://localhost:11434"
    llm_timeout_s: float = 20.0
    llm_temperature: float = 0.0
    """Sent to ollama; the anthropic SDK 1.x has no sampling parameters (see the provider)."""
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = "low"
    llm_fallbacks: Literal["default", "off"] = "default"

    @field_validator("llm_effort", mode="before")
    @classmethod
    def _no_effort(cls, value: object) -> object:
        """``LLM_EFFORT=`` / ``none`` / ``off``: do not send ``output_config.effort``."""
        return (
            None
            if isinstance(value, str) and value.strip().lower() in ("", "none", "off")
            else value
        )


def create_provider(
    settings: LlmSettings | None = None, *, transport: httpx2.AsyncBaseTransport | None = None
) -> LlmProvider:
    """The configured provider; ``transport`` replaces the HTTP transport (tests)."""
    s = settings or LlmSettings()
    if s.llm_provider == "none":
        return NoneProvider()
    if s.llm_provider == "anthropic":
        if s.offline:
            log.warning("llm_offline_blocked", provider="anthropic", fallback="none")
            return NoneProvider("OFFLINE=true forbids external LLM calls (anthropic)")
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else ""
        if not key:
            log.warning("llm_no_api_key", provider="anthropic", fallback="none")
            return NoneProvider("ANTHROPIC_API_KEY is empty")
        from qost_api.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            api_key=key,
            model=s.llm_model or DEFAULT_MODEL,
            timeout_s=s.llm_timeout_s,
            effort=s.llm_effort,
            fallbacks=s.llm_fallbacks == "default",
            offline=s.offline,
            base_url=s.anthropic_base_url,
            transport=transport,
        )
    from qost_api.llm.ollama_provider import OllamaProvider

    return OllamaProvider(
        url=s.ollama_url,
        model=s.llm_model,
        timeout_s=s.llm_timeout_s,
        temperature=s.llm_temperature,
        transport=transport,
    )


__all__ = [
    "DEFAULT_MODEL",
    "AssistantTurn",
    "LlmError",
    "LlmOfflineError",
    "LlmProvider",
    "LlmRefusalError",
    "LlmRequest",
    "LlmResponse",
    "LlmSettings",
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
    "create_provider",
]
