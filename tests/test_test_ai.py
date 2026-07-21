from __future__ import annotations

import inspect
import unittest

from market_watcher.ai import AiIntroService, AiTimeout
from tests.test_test_push import DIAGNOSTIC_ORIGIN, Event, collect, load_main_module


class AiClient:
    def __init__(
        self,
        *,
        provider: str | None | BaseException = "default-provider",
        outcome: str | BaseException = "生态出现一个演示插件",
    ):
        self.provider = provider
        self.outcome = outcome
        self.resolve_calls = []
        self.generate_calls = []

    async def resolve_provider_id(self, origin: str) -> str | None:
        self.resolve_calls.append(origin)
        if isinstance(self.provider, BaseException):
            raise self.provider
        return self.provider

    async def generate(
        self, *, provider_id: str, prompt: str, system_prompt: str
    ) -> str:
        self.generate_calls.append(
            {
                "provider_id": provider_id,
                "prompt": prompt,
                "system_prompt": system_prompt,
            }
        )
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class TestAiCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_main_module()

    def plugin(self, client: AiClient, *, provider_id: str = ""):
        plugin = object.__new__(self.main.MarketWatcherPlugin)
        plugin.config = {
            "llm_provider_id": provider_id,
            "enable_ai_summary": False,
        }
        plugin._runtime_config = None
        plugin._ai_intro = AiIntroService(client)
        plugin._store = object()
        plugin._service = object()
        return plugin

    def test_non_admin_is_rejected_by_permission_filter(self):
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_ai)
        self.assertIn("permission_type", source)
        self.assertIn("PermissionType.ADMIN", source)

    async def test_default_provider_uses_event_origin_and_returns_safe_intro(self):
        client = AiClient(outcome="发现一个用于诊断的演示插件")
        replies = await collect(self.plugin(client).test_ai(Event()))
        self.assertEqual(client.resolve_calls, [DIAGNOSTIC_ORIGIN])
        self.assertEqual(client.generate_calls[0]["provider_id"], "default-provider")
        self.assertIn("astrbot_plugin_demo", client.generate_calls[0]["prompt"])
        self.assertNotIn(DIAGNOSTIC_ORIGIN, client.generate_calls[0]["prompt"])
        self.assertEqual(
            replies,
            ["真实 Provider 调用成功。安全导语：发现一个用于诊断的演示插件"],
        )
        self.assertNotIn(DIAGNOSTIC_ORIGIN, replies[0])
        self.assertNotIn("请生成一段", replies[0])

    async def test_explicit_provider_wins_even_when_ai_summary_is_disabled(self):
        client = AiClient()
        replies = await collect(
            self.plugin(client, provider_id="explicit-provider").test_ai(Event())
        )
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual(client.generate_calls[0]["provider_id"], "explicit-provider")
        self.assertIn("真实 Provider 调用成功", replies[0])

    async def test_provider_failures_and_invalid_output_use_sanitized_fallback(self):
        cases = (
            (AiClient(provider=None), "ai_provider_not_found"),
            (AiClient(outcome=AiTimeout("secret timeout")), "ai_timeout"),
            (AiClient(outcome=RuntimeError("token=SECRET")), "ai_exception"),
            (AiClient(outcome="x" * 121), "ai_output_too_long"),
        )
        for client, error_code in cases:
            with self.subTest(error_code=error_code):
                replies = await collect(self.plugin(client).test_ai(Event()))
                self.assertEqual(len(replies), 1)
                self.assertIn(error_code, replies[0])
                self.assertIn("已回退纯事实模板", replies[0])
                self.assertNotIn("SECRET", replies[0])
                self.assertNotIn("secret timeout", replies[0])
                self.assertNotIn(DIAGNOSTIC_ORIGIN, replies[0])

    async def test_private_chat_is_rejected_without_provider_call(self):
        client = AiClient()
        replies = await collect(self.plugin(client).test_ai(Event(private=True)))
        self.assertEqual(client.resolve_calls, [])
        self.assertEqual(client.generate_calls, [])
        self.assertEqual(replies, ["AI Provider 测试仅可在群聊中使用。"])


if __name__ == "__main__":
    unittest.main()
