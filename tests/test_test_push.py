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
        self.html_render = None

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

    def plugin(self, notifier, config=None):
        plugin = object.__new__(self.main.MarketWatcherPlugin)
        plugin._notifier = notifier
        plugin._store = object()
        plugin._service = object()
        plugin.config = config or {"enable_image_card": False}
        plugin._runtime_config = None
        plugin._instance_marker = format(id(plugin), "x")
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

        test_config = {"enable_image_card": False}
        notifier = self.main.AstrBotNotifier(
            context, message_chain_loader=lambda: MessageChain
        )
        replies = await collect(self.plugin(notifier, test_config).test_push(Event()))
        self.assertEqual(len(context.calls), 1)
        self.assertEqual(context.calls[0][0], DIAGNOSTIC_ORIGIN)
        self.assertIn("主动推送测试", context.calls[0][1])
        self.assertEqual(replies, ["Market Watcher 主动推送测试已发送（模式：text）。"])

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


class TestPushImageCardContractTests(unittest.TestCase):
    """Contract checks for the image-card path in test_push.

    Requirement coverage:
      - html_render is sourced from plugin self, not self.context (fix 1)
      - test-push constructs payload with at least 1 synthetic event (fix 7)
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.main = load_main_module()

    def test_html_render_from_plugin_not_context(self) -> None:
        """Notifier receives html_render from plugin self, not from self.context."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.initialize)
        # The notifier constructor uses _plugin_renderer, not context attribute
        self.assertIn("_plugin_renderer if _plugin_callable else None", source)
        # Diagnostic observes context via getattr, but the notifier constructor
        # does not reference context for the renderer
        self.assertIn("getattr(self.context,", source)

    def test_instance_marker_in_init_and_initialize(self) -> None:
        """Instance marker is set in __init__ and logged in initialize diagnostic."""
        src_init = inspect.getsource(self.main.MarketWatcherPlugin.__init__)
        self.assertIn("instance_marker", src_init)
        self.assertIn("format(id(self),", src_init)

        src_init_log = inspect.getsource(self.main.MarketWatcherPlugin.initialize)
        self.assertIn("instance_marker", src_init_log)
        self.assertIn("notifier_marker", src_init_log)
        self.assertIn("runtime_marker", src_init_log)

    def test_diagnostic_log_contains_required_fields(self) -> None:
        """Renderer diagnostic log includes all required structured fields."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.initialize)
        self.assertIn("renderer diagnostic", source)
        for field in (
            "instance_marker",
            "notifier_marker",
            "runtime_marker",
            "image_card",
            "plugin_callable",
            "context_callable",
            "api_callable",
            "owner",
            "plugin_type",
            "context_type",
            "notifier_callable",
        ):
            self.assertIn(field, source)

    def test_notifier_only_uses_plugin_callable_no_module_fallback(self) -> None:
        """Notifier gets only plugin html_render; no module fallback assigned."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.initialize)
        self.assertIn("_plugin_renderer if _plugin_callable else None", source)
        self.assertNotIn(
            "render_custom_template", source.rsplit("notifier_callable", 1)[1]
        )

    def test_test_push_diagnostic_contains_required_fields(self) -> None:
        """test-push has a structured diagnostic log before the image/text branch."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        self.assertIn("test-push diagnostic", source)
        for field in (
            "instance_marker",
            "notifier_marker",
            "runtime_marker",
            "image_card",
            "plugin_callable",
            "notifier_callable",
            "condition",
            "notifier_present",
            "service_present",
        ):
            self.assertIn(field, source)

    def test_test_push_diagnostic_before_image_branch(self) -> None:
        """Diagnostic log appears before the image-card if condition."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        diag_pos = source.index("test-push diagnostic")
        if_pos = source.index("if runtime.enable_image_card")
        self.assertLess(diag_pos, if_pos)

    def test_test_push_card_payload_has_at_least_one_event(self) -> None:
        """test-push image payload includes at least one synthetic ChangeEvent."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        self.assertIn("ChangeEvent(", source)
        self.assertIn('event_id="test-push:discovered:1"', source)
        self.assertIn("synthetic_event", source)
        self.assertIn("build_card_payload(", source)
        self.assertNotIn("build_card_payload([]", source)

    def test_test_push_uses_last_delivery_mode(self) -> None:
        """test-push uses notifier.last_delivery_mode, not local hardcoded mode."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        self.assertIn("last_delivery_mode", source)
        image_assign = 'mode = "image"'
        self.assertNotIn(image_assign, source)
        self.assertIn("self._notifier.last_delivery_mode", source)

    def test_last_delivery_mode_reset_before_prepare(self) -> None:
        """test-push resets last_delivery_mode before each prepare/send cycle."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        self.assertIn("last_delivery_mode = None", source)

    def test_instance_marker_shared_across_methods(self) -> None:
        """Both initialize and test-push reference _instance_marker."""
        src_init = inspect.getsource(self.main.MarketWatcherPlugin.initialize)
        src_push = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        self.assertIn("_instance_marker", src_init)
        self.assertIn("_instance_marker", src_push)

    def test_test_push_diagnostic_no_sensitive_content(self) -> None:
        """test-push diagnostic does not contain repr, URL, endpoint, token, UMO."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.test_push)
        diag_line = next(
            line for line in source.splitlines() if "test-push diagnostic" in line
        )
        for forbidden in ("repr", "endpoint", "://", "token", "umo", ".com", "group"):
            self.assertNotIn(forbidden, diag_line.lower())

    def test_init_diagnostic_no_sensitive_content(self) -> None:
        """Init diagnostic log does not contain repr, endpoint, token, UMO, URL."""
        source = inspect.getsource(self.main.MarketWatcherPlugin.initialize)
        diag_line = next(
            line for line in source.splitlines() if "renderer diagnostic" in line
        )
        for forbidden in ("repr", "endpoint", "://", "token", "umo", ".com"):
            self.assertNotIn(forbidden, diag_line.lower())


if __name__ == "__main__":
    unittest.main()
