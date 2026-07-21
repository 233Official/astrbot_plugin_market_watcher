from __future__ import annotations

import importlib
import importlib.util
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from market_watcher.astrbot_adapter import AstrBotAiClient, AstrBotNotifier


def load_astrbot_contract():
    if importlib.util.find_spec("astrbot") is None:
        raise unittest.SkipTest(
            "AstrBot is not installed; set PYTHONPATH or install AstrBot"
        )
    main_module = importlib.import_module("main")
    event_module = importlib.import_module("astrbot.api.event")
    star_module = importlib.import_module("astrbot.api.star")
    return main_module, event_module.MessageChain, star_module.StarTools


class AstrBotContractIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main, cls.MessageChain, cls.StarTools = load_astrbot_contract()

    def test_event_module_exports_message_chain(self) -> None:
        self.assertTrue(callable(self.MessageChain))
        self.assertTrue(callable(self.StarTools.get_data_dir))
        context_type = importlib.import_module("astrbot.api.star").Context
        self.assertTrue(callable(context_type.get_current_chat_provider_id))

    def test_main_import_and_admin_decorator_contract(self) -> None:
        self.assertTrue(hasattr(self.main, "MarketWatcherPlugin"))
        source = inspect.getsource(self.main.MarketWatcherPlugin)
        self.assertIn("permission_type", source)
        self.assertIn("PermissionType.ADMIN", source)

    def test_plugin_construct_initialize_and_terminate(self) -> None:
        class Context:
            async def send_message(self, target, chain):
                return True

            async def llm_generate(self, **kwargs):
                return SimpleNamespace(role="assistant", completion_text="ok")

            async def get_current_chat_provider_id(self, origin):
                return "provider"

        class Http:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            async def get(self, url, *, headers=None):
                raise AssertionError("initialize must not access the network")

            async def close(self):
                return None

        async def exercise():
            with tempfile.TemporaryDirectory() as directory:
                with (
                    patch.object(
                        self.main.StarTools,
                        "get_data_dir",
                        return_value=Path(directory),
                    ) as data_dir,
                    patch.object(self.main, "AioHttpClient", Http),
                ):
                    plugin = self.main.MarketWatcherPlugin(Context(), {})
                    await plugin.initialize()
                    self.assertIsNotNone(plugin._scheduler)
                    self.assertIs(plugin._service.notifier, plugin._notifier)
                    self.assertIs(plugin._service.ai_intro, plugin._ai_intro)
                    self.assertIs(plugin._ai_intro.client, plugin._ai_client)
                    self.assertEqual(plugin._ai_client.timeout_seconds, 60)
                    data_dir.assert_called_once_with("astrbot_plugin_market_watcher")
                    await plugin.terminate()
                    self.assertIsNone(plugin._scheduler)

        import asyncio

        asyncio.run(exercise())

    def test_llm_generate_and_send_message_boundaries(self) -> None:
        class Context:
            def __init__(self):
                self.llm_calls = []
                self.send_calls = []

            async def llm_generate(self, **kwargs):
                self.llm_calls.append(kwargs)
                return SimpleNamespace(role="assistant", completion_text="导语")

            async def get_current_chat_provider_id(self, origin):
                return "provider"

            async def send_message(self, target, chain):
                self.send_calls.append((target, chain))
                return True

        async def exercise():
            context = Context()
            text = await AstrBotAiClient(context).generate(
                provider_id="provider", prompt="facts", system_prompt="rules"
            )
            self.assertEqual(text, "导语")
            self.assertEqual(context.llm_calls[0]["chat_provider_id"], "provider")
            success, error = await AstrBotNotifier(context).send("umo:test", "message")
            self.assertTrue(success, error)
            self.assertEqual(context.send_calls[0][0], "umo:test")

        import asyncio

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
