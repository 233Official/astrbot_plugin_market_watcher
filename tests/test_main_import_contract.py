from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_release_common():
    path = ROOT / "scripts" / "release_common.py"
    spec = importlib.util.spec_from_file_location("release_common_for_tests", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load release_common.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MainImportContractTests(unittest.TestCase):
    def test_main_loads_in_astrbot_package_context(self) -> None:
        release_common = load_release_common()
        release_common.verify_main_import_contract(ROOT)

    def test_main_still_loads_as_top_level_module(self) -> None:
        release_common = load_release_common()
        saved = {
            name: module
            for name, module in sys.modules.items()
            if name == "astrbot" or name.startswith("astrbot.")
        }
        for name in saved:
            sys.modules.pop(name, None)
        sys.modules.pop("main", None)
        release_common._install_astrbot_stubs()
        try:
            spec = importlib.util.spec_from_file_location("main", ROOT / "main.py")
            if spec is None or spec.loader is None:
                self.fail("cannot create top-level main.py spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules["main"] = module
            spec.loader.exec_module(module)
            self.assertTrue(hasattr(module, "MarketWatcherPlugin"))
            self.assertEqual(module.__package__, "")
            self.assertEqual(module.MarketWatcherPlugin._service_clock()[-1:], "Z")
        finally:
            sys.modules.pop("main", None)
            for name in list(sys.modules):
                if name == "astrbot" or name.startswith("astrbot."):
                    sys.modules.pop(name, None)
            sys.modules.update(saved)


if __name__ == "__main__":
    unittest.main()
