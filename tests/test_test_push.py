from __future__ import annotations

import importlib.util
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIAGNOSTIC_ORIGIN = "qqws:GROUP_MESSAGE:group_openid"


def load_main_module():
    common_path = ROOT / "scripts" / "release_common.py"
    common_spec = importlib.util.spec_from_file_location(
        "release_common_for_test_push", common_path
    )
    if common_spec is None or common_spec.loader is None:
        raise ImportError("cannot load release_common.py")
    common = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(common)

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "astrbot" or name.startswith("astrbot.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    common._install_astrbot_stubs()
    try:
        spec = importlib.util.spec_from_file_location(
            "main_for_test_push", ROOT / "main.py"
        )
        if spec is None or spec.loader is None:
            raise ImportError("cannot load main.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name in list(sys.modules):
            if name == "astrbot" or name.startswith("astrbot."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


class Event:
    def __init__(self, *, private: bool = False) -> None:
        self.unified_msg_origin = DIAGNOSTIC_ORIGIN
        self.private = private

    def is_private_chat(self) -> bool:
        return self.private

    def plain_result(self, text: str) -> str:
        return text


class Notifier:
    def __init__(self, *, result=(True, None), error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    async def send(self, target: str, message: str):
        self.calls.append((target, message))
        if self.error is not None:
            raise self.error
        return self.result


async def collect(generator) -> list[str]:
    return [item async for item in generator]


class TestPushCommandTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_main_module()

    def plugin(self, notifier):
        plugin = object.__new__(self.main.MarketWatcherPlugin)
        plugin._notifier = notifier
        plugin._store = object()
        plugin._service = object()
        return plugin

    def test_non_admin_is_rejected_by_permission_filter(self):
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        self.assertIn("permission_type", source)
        self.assertIn("PermissionType.ADMIN", source)

    async def test_admin_group_success_forwards_original_umo_without_state_changes(
        self,
    ):
        class MessageChain:
            def message(self, text):
                return f"chain:{text}"

        class Context:
            def __init__(self):
                self.calls = []

            async def send_message(self, target, message):
                self.calls.append((target, message))
                return True

        context = Context()
        notifier = self.main.AstrBotNotifier(context, lambda: MessageChain)
        replies = await collect(self.plugin(notifier).test_push(Event()))
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0][0], DIAGNOSTIC_ORIGIN)
        self.assertIn("主动推送测试", context.calls[0][1])
        self.assertEqual(replies, ["Market Watcher 主动推送测试已发送。"])

    async def test_private_chat_is_rejected_without_send(self):
        notifier = Notifier()
        replies = await collect(self.plugin(notifier).test_push(Event(private=True)))
        self.assertEqual(notifier.calls, [])
        self.assertEqual(replies, ["主动推送测试仅可在群聊中使用。"])

    async def test_notifier_exception_is_sanitized(self):
        secret = "token=SECRET origin=private"
        notifier = Notifier(error=RuntimeError(secret))
        replies = await collect(self.plugin(notifier).test_push(Event()))
        self.assertEqual(len(replies), 1)
        self.assertIn("delivery_exception", replies[0])
        self.assertNotIn(secret, replies[0])
        self.assertNotIn(DIAGNOSTIC_ORIGIN, replies[0])


if __name__ == "__main__":
    unittest.main()
