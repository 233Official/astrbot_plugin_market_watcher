from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_simple_yaml(path: Path) -> dict[str, str]:
    """Read the flat scalar subset used by metadata.yaml."""
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


class StructureTests(unittest.TestCase):
    def test_required_files_exist(self) -> None:
        required = {
            "metadata.yaml",
            "main.py",
            "_conf_schema.json",
            "requirements.txt",
            "README.md",
            "docs/PRD.md",
            "LICENSE",
            ".gitignore",
            "CHANGELOG.md",
            "pyproject.toml",
            "market_watcher/__init__.py",
        }
        missing = sorted(path for path in required if not (ROOT / path).is_file())
        self.assertEqual(missing, [])

    def test_metadata_contract(self) -> None:
        metadata = read_simple_yaml(ROOT / "metadata.yaml")
        self.assertEqual(metadata["name"], "astrbot_plugin_market_watcher")
        self.assertEqual(metadata["author"], "233Official")
        self.assertEqual(metadata["version"], "0.1.0")
        self.assertEqual(
            metadata["repo"],
            "https://github.com/233Official/astrbot_plugin_market_watcher",
        )
        self.assertIn("<5", metadata["astrbot_version"])

    def test_config_schema_has_safe_defaults(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        expected = {
            "enabled",
            "poll_interval_minutes",
            "push_targets",
            "github_token",
            "llm_provider_id",
            "source_market_api",
            "source_collection_issues",
            "source_plugin_publish_issues",
            "source_github_discovery",
        }
        self.assertTrue(expected.issubset(schema))
        self.assertFalse(schema["enabled"]["default"])
        self.assertEqual(schema["github_token"]["default"], "")
        self.assertEqual(schema["push_targets"]["default"], [])
        self.assertFalse(schema["source_plugin_publish_issues"]["default"])
        self.assertFalse(schema["source_github_discovery"]["default"])


if __name__ == "__main__":
    unittest.main()
