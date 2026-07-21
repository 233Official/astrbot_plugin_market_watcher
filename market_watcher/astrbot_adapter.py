"""AstrBot notification boundary kept import-safe for offline tests."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Callable

from .ai import (
    AI_TIMEOUT_SECONDS,
    AiCallFailed,
    AiProviderMissing,
    AiRoleError,
    AiTimeout,
)


def load_message_chain():
    from astrbot.api.event import MessageChain

    return MessageChain


class AstrBotNotifier:
    def __init__(self, context, message_chain_loader: Callable | None = None) -> None:
        self.context = context
        self.message_chain_loader = message_chain_loader or load_message_chain

    async def send(self, target: str, message: str) -> tuple[bool, str | None]:
        try:
            MessageChain = self.message_chain_loader()
            result = await self.context.send_message(
                target,
                MessageChain().message(message),
            )
        except Exception:
            return False, "astrbot_send_exception"
        if result is False:
            return False, "astrbot_send_false"
        return True, None


class AstrBotAiClient:
    def __init__(self, context, timeout_seconds: float = AI_TIMEOUT_SECONDS) -> None:
        self.context = context
        self.timeout_seconds = timeout_seconds

    async def resolve_provider_id(self, origin: str) -> str | None:
        try:
            provider_id = await self.context.get_current_chat_provider_id(origin)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            provider_error = _provider_not_found_error()
            if (provider_error is not None and isinstance(exc, provider_error)) or type(
                exc
            ).__name__ == "ProviderNotFoundError":
                raise AiProviderMissing from exc
            raise AiCallFailed from exc
        return provider_id.strip() if isinstance(provider_id, str) else None

    async def generate(
        self, *, provider_id: str, prompt: str, system_prompt: str
    ) -> str:
        try:
            response = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=system_prompt,
                ),
                timeout=self.timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise AiTimeout from exc
        except Exception as exc:
            provider_error = _provider_not_found_error()
            if (provider_error is not None and isinstance(exc, provider_error)) or type(
                exc
            ).__name__ == "ProviderNotFoundError":
                raise AiProviderMissing from exc
            raise AiCallFailed from exc
        if getattr(response, "role", None) == "err":
            raise AiRoleError
        for name in ("completion_text", "content", "text"):
            value = getattr(response, name, None)
            if isinstance(value, str):
                return value
        if isinstance(response, str):
            return response
        return ""


def _provider_not_found_error():
    for module_name in (
        "astrbot.core.provider",
        "astrbot.core.provider.provider",
        "astrbot.api.provider",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        error_type = getattr(module, "ProviderNotFoundError", None)
        if isinstance(error_type, type) and issubclass(error_type, Exception):
            return error_type
    return None


class FakeNotifier:
    def __init__(
        self,
        outcomes: dict[str, list[bool | Exception]] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, str]] = []

    async def send(self, target: str, message: str) -> tuple[bool, str | None]:
        self.calls.append((target, message))
        queue = self.outcomes.get(target, [])
        outcome = queue.pop(0) if queue else True
        if isinstance(outcome, Exception):
            raise outcome
        return (True, None) if outcome else (False, "fake_send_false")
