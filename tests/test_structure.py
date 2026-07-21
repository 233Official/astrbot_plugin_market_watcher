from __future__ import annotations

import ast
import importlib
import json
import re
import sys
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
            "CONTRIBUTING.md",
            "metadata.yaml",
            "main.py",
            "_conf_schema.json",
            "requirements.txt",
            "README.md",
            ".github/workflows/ci.yml",
            "docs/PRD.md",
            "docs/COMMANDS.md",
            "docs/CONFIGURATION.md",
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

    def test_release_package_contains_public_docs_and_readme_links(self) -> None:
        scripts_dir = str(ROOT / "scripts")
        sys.path.insert(0, scripts_dir)
        try:
            release_common = importlib.import_module("release_common")
        finally:
            sys.path.remove(scripts_dir)

        packaged = {
            path.relative_to(ROOT).as_posix()
            for path in release_common.package_source_files()
        }
        public_docs = {
            "CONTRIBUTING.md",
            "docs/COMMANDS.md",
            "docs/CONFIGURATION.md",
        }
        self.assertTrue(public_docs.issubset(packaged))

        link_sources = {"README.md", *public_docs}
        missing_targets = []
        for source in link_sources:
            text = (ROOT / source).read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
                clean = target.split("#", 1)[0]
                if not clean or "://" in clean or clean.startswith("mailto:"):
                    continue
                resolved = (Path(source).parent / clean).as_posix()
                if resolved.removeprefix("./") not in packaged:
                    missing_targets.append(f"{source} -> {target}")
        self.assertEqual(sorted(missing_targets), [])

    def test_metadata_contract(self) -> None:
        metadata = read_simple_yaml(ROOT / "metadata.yaml")
        self.assertEqual(metadata["name"], "astrbot_plugin_market_watcher")
        self.assertEqual(metadata["author"], "233Official")
        self.assertEqual(metadata["version"], "1.1.0")
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
        self.assertEqual(project["version"], "1.1.0")
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
            "scripts/package_plugin.py",
        ):
            self.assertIn(command, workflow)
        self.assertNotIn("release-action", workflow.lower())

    def test_launch_json_and_release_workflow_exist(self) -> None:
        self.assertTrue((ROOT / ".vscode/launch.json").is_file())
        self.assertTrue((ROOT / ".github/workflows/release.yml").is_file())


class ReleaseVersionValidationTests(unittest.TestCase):
    """Simulates the release.yml Validate release version step for both
    tag-push (github.ref_name=v1.1.0) and workflow_dispatch (inputs.tag=v1.1.0).
    The validation must accept the v-prefixed tag and match it against
    the unprefixed versions in metadata.yaml, main.py @register, and
    pyproject.toml.
    """

    def _run_validation(self, tag: str) -> list[str]:
        """Replicate the inline Python from release.yml's Validate step."""
        import yaml

        metadata = yaml.safe_load((ROOT / "metadata.yaml").read_text(encoding="utf-8"))
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        main_text = (ROOT / "main.py").read_text(encoding="utf-8")

        metadata_version: object = metadata.get("version")
        project_version: object = project["project"]["version"]
        register_match = re.search(
            r'@register\(.*?"[^"]+",\s*"[^"]+",\s*.*?,\s*"([^"]+)"\s*,?\s*\)',
            main_text,
            re.DOTALL,
        )
        main_version: str | None = register_match.group(1) if register_match else None

        expected_version = tag.removeprefix("v")
        errors: list[str] = []
        if metadata_version != expected_version:
            errors.append(
                f"metadata.yaml version {metadata_version!r} != "
                f"expected {expected_version!r}"
            )
        if main_version != expected_version:
            errors.append(
                f"main.py @register version {main_version!r} != "
                f"expected {expected_version!r}"
            )
        if project_version != expected_version:
            errors.append(
                f"pyproject.toml version {project_version!r} != "
                f"expected {expected_version!r}"
            )
        return errors

    def test_tag_push_v1_1_0(self) -> None:
        """Simulate tag push: RELEASE_TAG = github.ref_name = v1.1.0."""
        errors = self._run_validation("v1.1.0")
        self.assertEqual(errors, [], f"tag-push validation failed: {errors}")

    def test_workflow_dispatch_v1_1_0(self) -> None:
        """Simulate workflow_dispatch: RELEASE_TAG = inputs.tag = v1.1.0."""
        errors = self._run_validation("v1.1.0")
        self.assertEqual(errors, [], f"dispatch validation failed: {errors}")

    def test_invalid_tag_is_rejected(self) -> None:
        """A mismatched tag must produce errors."""
        errors = self._run_validation("v9.9.9")
        self.assertGreater(len(errors), 0)

    def test_unprefixed_tag_is_also_accepted(self) -> None:
        """An unprefixed tag works if versions match."""
        errors = self._run_validation("1.1.0")
        self.assertEqual(errors, [], f"unprefixed tag validation failed: {errors}")


class ReleaseNotesExtractionTests(unittest.TestCase):
    """Tests the release.yml Extract release notes inline Python logic.

    Replicates the regex and extraction, then verifies correctness for
    various CHANGELOG states.
    """

    CHANGELOG_1_1_0 = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    def _extract(self, tag: str, changelog: str | None = None) -> str:
        """Replicate the inline Python from release.yml's Extract step."""
        expected = tag.removeprefix("v")
        text = changelog if changelog is not None else self.CHANGELOG_1_1_0
        pattern = re.compile(
            rf"^##\s+{re.escape(expected)}(?:\s+-\s+[^\n]+)?\n(?P<body>.*?)(?=\n---\n\n##\s+|\n##\s+|\Z)",
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(text)
        if not match:
            raise LookupError(
                f"Cannot find {expected} section in CHANGELOG.md "
                f"(searched for tag {tag!r})"
            )
        return match.group("body").strip()

    def test_v1_1_0_dispatch_tag_extracts_non_empty_notes(self) -> None:
        """workflow_dispatch(tag=v1.1.0) extracts real notes."""
        notes = self._extract("v1.1.0")
        self.assertGreater(len(notes), 100)
        self.assertIn("图片卡片", notes)
        self.assertIn("image_card", notes)

    def test_unprefixed_1_1_0_also_extracts(self) -> None:
        """Unprefixed 1.1.0 works (some callers may strip beforehand)."""
        notes = self._extract("1.1.0")
        self.assertGreater(len(notes), 100)

    def test_nonexistent_version_raises(self) -> None:
        """A missing version section raises LookupError, not silent empty."""
        with self.assertRaises(LookupError):
            self._extract("v9.9.9")

    def test_minimal_changelog_section_extracts_content(self) -> None:
        """A version with minimal content still extracts correctly, not empty."""
        fake = "# Fake\n\n---\n\n## 9.9.9 - 2099-01-01\n\n-\n\n---\n\n## 0.0.1\n"
        notes = self._extract("v9.9.9", fake)
        self.assertEqual(notes, "-")

    def test_notes_preserve_markdown_subheadings_lists_code_and_separators(
        self,
    ) -> None:
        """Extracted notes keep subheadings, bullet lists, backtick code,
        and horizontal rules without truncation or escaping."""
        notes = self._extract("v1.1.0")
        self.assertIn("### Added", notes)
        self.assertIn("### Changed", notes)
        self.assertIn("### Fixed", notes)
        self.assertIn("- 增加图片卡片", notes)
        self.assertIn("`card_renderer`", notes)
        self.assertIn("`enable_image_card`", notes)
        self.assertIn("`image_render_timeout_seconds`", notes)
        self.assertIn("`deliver_pending()`", notes)

    def test_v_prefix_heading_is_not_required(self) -> None:
        """CHANGELOG uses '## 1.1.0 - date' not '## v1.1.0 - date'."""
        self.assertIn("## 1.1.0 - 2026-07-22", self.CHANGELOG_1_1_0)
        self.assertNotIn("## v1.1.0", self.CHANGELOG_1_1_0)

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
