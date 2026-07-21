from __future__ import annotations

import ast
import inspect
import sys
import textwrap
import unittest
from contextlib import contextmanager
from functools import partial
from types import ModuleType, SimpleNamespace
from unittest import mock

from market_watcher import astrbot_handler_compat as compat
from tests.test_test_push import Event, Notifier, collect, load_main_module


class Registry:
    def __init__(self, handlers):
        self.handlers = handlers
        self.module_names = []

    def get_handlers_by_module_name(self, module_name):
        self.module_names.append(module_name)
        return self.handlers


@contextmanager
def installed_registry(registry):
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "astrbot" or name.startswith("astrbot.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    modules = {
        "astrbot": ModuleType("astrbot"),
        "astrbot.core": ModuleType("astrbot.core"),
        "astrbot.core.star": ModuleType("astrbot.core.star"),
        "astrbot.core.star.star_handler": ModuleType("astrbot.core.star.star_handler"),
    }
    setattr(
        modules["astrbot.core.star.star_handler"],
        "star_handlers_registry",
        registry,
    )
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == "astrbot" or name.startswith("astrbot."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def plugin_type(module_name="tests.dynamic_market_watcher"):
    def command(self, event):
        return self.label, event

    return type(
        "Plugin",
        (),
        {
            "__module__": module_name,
            "command": command,
            "not_function": object(),
        },
    )


class HandlerCompatTests(unittest.IsolatedAsyncioTestCase):
    def test_correct_single_binding_is_unchanged(self):
        plugin = plugin_type()
        current = plugin()
        original = plugin.__dict__["command"]
        handler = partial(original, current)
        metadata = SimpleNamespace(handler_name="command", handler=handler)
        registry = Registry([metadata])
        with installed_registry(registry):
            self.assertEqual(compat.normalize_plugin_handler_bindings(current), 0)
        self.assertIs(metadata.handler, handler)
        self.assertEqual(registry.module_names, [plugin.__module__])

    def test_double_and_triple_bindings_converge_to_current_instance(self):
        for depth in (2, 3):
            with self.subTest(depth=depth):
                plugin = plugin_type(f"tests.dynamic_depth_{depth}")
                instances = [plugin() for _ in range(depth)]
                current = instances[-1]
                setattr(current, "label", "current")
                original = plugin.__dict__["command"]
                handler = partial(original, instances[0])
                for item in instances[1:]:
                    handler = partial(handler, item)
                metadata = SimpleNamespace(handler_name="command", handler=handler)
                with installed_registry(Registry([metadata])):
                    self.assertEqual(
                        compat.normalize_plugin_handler_bindings(current), 1
                    )
                self.assertIs(metadata.handler.func, original)
                self.assertEqual(metadata.handler.args, (current,))
                self.assertEqual(metadata.handler("event"), ("current", "event"))

    def test_unsafe_shapes_are_skipped(self):
        plugin = plugin_type()
        current = plugin()
        old = plugin()
        original = plugin.__dict__["command"]

        def other(instance, event):
            return instance, event

        handlers = [
            SimpleNamespace(handler_name="command", handler=partial(other, old)),
            SimpleNamespace(
                handler_name="command", handler=partial(original, object())
            ),
            SimpleNamespace(
                handler_name="command",
                handler=partial(original, old, diagnostic=True),
            ),
            SimpleNamespace(handler_name="not_function", handler=object()),
        ]
        before = [item.handler for item in handlers]
        with installed_registry(Registry(handlers)):
            self.assertEqual(compat.normalize_plugin_handler_bindings(current), 0)
        self.assertEqual([item.handler for item in handlers], before)

    def test_registry_or_api_unavailable_returns_zero(self):
        plugin = plugin_type()
        with mock.patch.object(
            compat.importlib, "import_module", side_effect=ImportError("missing")
        ):
            self.assertEqual(compat.normalize_plugin_handler_bindings(plugin()), 0)
        with mock.patch.object(
            compat.importlib, "import_module", return_value=ModuleType("star_handler")
        ):
            self.assertEqual(compat.normalize_plugin_handler_bindings(plugin()), 0)

    def test_registry_logic_errors_are_not_silenced(self):
        plugin = plugin_type()
        registry = mock.Mock()
        registry.get_handlers_by_module_name.side_effect = RuntimeError("registry bug")
        with installed_registry(registry):
            with self.assertRaisesRegex(RuntimeError, "registry bug"):
                compat.normalize_plugin_handler_bindings(plugin())

    def test_exact_dynamic_and_astrbot_package_module_paths(self):
        for module_name in (
            "tests.dynamic_market_watcher",
            "data.plugins.astrbot_plugin_market_watcher.main",
        ):
            with self.subTest(module_name=module_name):
                plugin = plugin_type(module_name)
                current = plugin()
                metadata = SimpleNamespace(
                    handler_name="command",
                    handler=partial(plugin.__dict__["command"], plugin(), current),
                )
                registry = Registry([metadata])
                with installed_registry(registry):
                    self.assertEqual(
                        compat.normalize_plugin_handler_bindings(current), 1
                    )
                self.assertEqual(registry.module_names, [module_name])

    async def test_reenable_normalization_uses_new_test_push_instance(self):
        main = load_main_module()
        plugin_type_ = main.MarketWatcherPlugin
        original = plugin_type_.__dict__["test_push"]
        old = object.__new__(plugin_type_)
        new = object.__new__(plugin_type_)
        old._notifier = None
        new_notifier = Notifier()
        new._notifier = new_notifier
        metadata = SimpleNamespace(
            handler_name="test_push",
            handler=partial(partial(original, old), new),
        )
        registry = Registry([metadata])
        with installed_registry(registry):
            self.assertEqual(compat.normalize_plugin_handler_bindings(new), 1)
        replies = await collect(metadata.handler(Event()))
        self.assertEqual(len(new_notifier.calls), 1)
        self.assertIn("已发送", replies[0])

    def test_command_filter_handler_params_remain_empty(self):
        main = load_main_module()
        original = main.MarketWatcherPlugin.__dict__["test_push"]
        parameters = tuple(inspect.signature(original).parameters.values())
        self.assertEqual([item.name for item in parameters], ["self", "event"])
        self.assertFalse(
            any(item.kind is inspect.Parameter.VAR_POSITIONAL for item in parameters)
        )
        self.assertEqual(parameters[2:], ())

    def test_initialize_normalizes_before_resources_or_await(self):
        main = load_main_module()
        source = textwrap.dedent(inspect.getsource(main.MarketWatcherPlugin.initialize))
        function = ast.parse(source).body[0]
        if not isinstance(function, ast.AsyncFunctionDef):
            self.fail("initialize must remain an async function")
        first = function.body[0]
        if not isinstance(first, ast.Assign):
            self.fail("initialize must normalize handlers first")
        target = first.targets[0]
        call = first.value
        if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
            self.fail("initialize handler normalization shape changed")
        if not isinstance(call.func, ast.Name):
            self.fail("initialize must call the compatibility boundary directly")
        self.assertEqual(target.id, "normalized_handlers")
        self.assertEqual(call.func.id, "normalize_plugin_handler_bindings")
        self.assertFalse(any(isinstance(node, ast.Await) for node in ast.walk(first)))


if __name__ == "__main__":
    unittest.main()
