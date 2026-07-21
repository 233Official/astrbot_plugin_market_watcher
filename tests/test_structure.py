from __future__ import annotations

import ast
import importlib
import json
import unittest
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI path
    import tomli as tomllib

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
    def test_runtime_avoids_python_311_datetime_utc(self) -> None:
        runtime_files = [ROOT / "main.py", *(ROOT / "market_watcher").rglob("*.py")]
        offenders = []
        for path in runtime_files:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "datetime":
                    if any(alias.name == "UTC" for alias in node.names):
                        offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_datetime_modules_import_on_supported_python(self) -> None:
        for module_name in (
            "market_watcher.normalize",
            "market_watcher.outbox",
            "market_watcher.http",
            "market_watcher.github",
            "market_watcher.github_diagnostic",
        ):
            with self.subTest(module=module_name):
                importlib.import_module(module_name)

    def test_scheduler_uses_python_310_asyncio_timeout_class(self) -> None:
        source = (ROOT / "market_watcher/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("except asyncio.TimeoutError:", source)
        self.assertNotIn("except TimeoutError:", source)

    def test_required_files_exist(self) -> None:
        required = {
            "metadata.yaml",
            "main.py",
            "_conf_schema.json",
            "requirements.txt",
            "README.md",
            ".github/workflows/ci.yml",
            "docs/PRD.md",
            "docs/FSD.md",
            "docs/DESIGN.md",
            "docs/ONLINE_ACCEPTANCE.md",
            "docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md",
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
        self.assertEqual(metadata["version"], "1.0.0")
        self.assertEqual(
            metadata["repo"],
            "https://github.com/233Official/astrbot_plugin_market_watcher",
        )
        self.assertIn("<5", metadata["astrbot_version"])
        platforms = {
            item.strip()
            for item in metadata["support_platforms"].strip("[]").split(",")
        }
        self.assertTrue({"aiocqhttp", "qq_official"}.issubset(platforms))
        self.assertNotIn("qq_official_webhook", platforms)

    def test_config_schema_has_safe_defaults(self) -> None:
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        expected = {
            "enabled",
            "poll_interval_minutes",
            "push_targets",
            "github_token",
            "llm_provider_id",
            "enable_ai_summary",
            "ai_timeout_seconds",
            "source_market_api",
            "source_collection_issues",
            "source_plugin_publish_issues",
            "source_github_discovery",
        }
        self.assertTrue(expected.issubset(schema))
        self.assertFalse(schema["enabled"]["default"])
        self.assertEqual(schema["github_token"]["default"], "")
        self.assertIs(schema["github_token"]["obvious_hint"], True)
        self.assertEqual(schema["push_targets"]["default"], [])
        self.assertIn("适配器实例 ID", schema["push_targets"]["hint"])
        self.assertIn(
            "qqws:GROUP_MESSAGE:<group_openid>", schema["push_targets"]["hint"]
        )
        self.assertEqual(schema["llm_provider_id"]["_special"], "select_provider")
        self.assertEqual(schema["ai_timeout_seconds"]["default"], 60)
        self.assertEqual(
            schema["ai_timeout_seconds"]["slider"],
            {"min": 10, "max": 120, "step": 5},
        )
        self.assertFalse(schema["enable_ai_summary"]["default"])
        self.assertFalse(schema["source_plugin_publish_issues"]["default"])
        self.assertFalse(schema["source_github_discovery"]["default"])

    def test_pyproject_metadata_matches_runtime_contract(self) -> None:
        with (ROOT / "pyproject.toml").open("rb") as handle:
            pyproject = tomllib.load(handle)
        project = pyproject["project"]
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        runtime_dependencies = [
            line.strip()
            for line in requirements.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(project["name"], "astrbot-plugin-market-watcher")
        self.assertEqual(project["version"], "1.0.0")
        self.assertEqual(project["requires-python"], ">=3.10")
        self.assertEqual(project["dependencies"], runtime_dependencies)
        self.assertNotIn("tomli", project["dependencies"])
        self.assertNotIn("astrbot", " ".join(project["dependencies"]).lower())
        self.assertEqual(pyproject["tool"]["ruff"]["target-version"], "py310")
        self.assertEqual(
            pyproject["tool"]["pytest"]["ini_options"]["testpaths"], ["tests"]
        )

    def test_ci_and_playbook_cover_release_contract(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        for command in (
            "pip install --no-deps -e .",
            "ruff check .",
            "compileall",
            "scripts/verify_release.py",
            "scripts/package_release.py",
        ):
            self.assertIn(command, workflow)
        self.assertNotIn("release-action", workflow.lower())

        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/DESIGN.md", readme)
        self.assertIn("docs/ONLINE_ACCEPTANCE.md", readme)

        playbook = (ROOT / "docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md").read_text(
            encoding="utf-8"
        )
        for contract in (
            "if __package__",
            "StarTools.get_data_dir",
            "ModuleNotFoundError",
            "SHA-256",
            "真实 AstrBot",
        ):
            self.assertIn(contract, playbook)

    def test_status_command_reports_pending_and_exhausted_separately(self) -> None:
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        status_source = (ROOT / "market_watcher/status.py").read_text(encoding="utf-8")
        self.assertIn("count_pending(state)", main_source)
        self.assertIn("count_exhausted(state)", main_source)
        self.assertIn("待投递目标", status_source)
        self.assertIn("永久失败目标", status_source)

    def test_command_group_uses_canonical_name_without_wake_prefix(self) -> None:
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@filter.command_group("marketwatch")', main_source)
        self.assertNotIn('@filter.command_group("/marketwatch")', main_source)
        self.assertIn('@marketwatch.command("test-push")', main_source)
        self.assertIn('@marketwatch.command("test-ai")', main_source)
        self.assertIn('@marketwatch.command("test-github")', main_source)
        self.assertIn('@marketwatch.command("test-outbox-prepare")', main_source)
        self.assertIn('@marketwatch.command("test-outbox-status")', main_source)
        self.assertIn('@marketwatch.command("test-outbox-deliver")', main_source)
        self.assertIn('@marketwatch.command("test-outbox-cleanup")', main_source)
        self.assertNotIn('@marketwatch.command("!test-push")', main_source)
        self.assertNotIn('@marketwatch.command("/test-push")', main_source)
        self.assertNotIn('@marketwatch.command("!test-ai")', main_source)
        self.assertNotIn('@marketwatch.command("/test-ai")', main_source)
        self.assertNotIn('@marketwatch.command("!test-github")', main_source)
        self.assertNotIn('@marketwatch.command("/test-github")', main_source)

    def test_ai_timeout_wires_one_client_to_production_and_diagnostics(self) -> None:
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("timeout_seconds=runtime.ai_timeout_seconds", main_source)
        self.assertIn("self._ai_intro = AiIntroService(self._ai_client)", main_source)
        self.assertIn("ai_intro=self._ai_intro", main_source)
        self.assertIn("await self._ai_intro.diagnose(", main_source)

    def test_user_guidance_supports_current_wake_method(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("命令注册名不含唤醒词", readme)
        self.assertIn("!marketwatch status", readme)
        self.assertIn("@机器人", readme)

        playbook = (ROOT / "docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("<当前唤醒方式>marketwatch subscribe", playbook)
        self.assertIn("不要在插件内重复解析", playbook)

        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        hint = schema["push_targets"]["hint"]
        self.assertIn("当前唤醒方式", hint)
        self.assertIn("/marketwatch subscribe", hint)

    def test_admin_commands_are_permission_guarded(self) -> None:
        main_source = (ROOT / "main.py").read_text(encoding="utf-8")
        for command in (
            "test-push",
            "test-ai",
            "test-github",
            "test-outbox-prepare",
            "test-outbox-status",
            "test-outbox-deliver",
            "test-outbox-cleanup",
            "subscribe",
            "unsubscribe",
            "subscriptions",
        ):
            marker = f'@marketwatch.command("{command}")'
            start = main_source.index(marker)
            block = main_source[start : start + 220]
            self.assertIn("PermissionType.ADMIN", block)


if __name__ == "__main__":
    unittest.main()
