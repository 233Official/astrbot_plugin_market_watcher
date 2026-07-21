"""Tests for ``scripts/package_plugin.py`` — dev-version isolation,
determinism, file exclusion, and workspace immutability."""

from __future__ import annotations

import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from package_plugin import (  # noqa: E402
    _patched_file_content,
    _plugin_name,
    _plugin_version,
    _project_version,
    build_archive,
    build_dev_version,
    iter_package_files,
    verify_archive,
)


class PackageFileSelectionTests(unittest.TestCase):
    """Package file list includes code/docs/root files; excludes dev/test."""

    def test_root_files_present(self) -> None:
        files = iter_package_files()
        for required in (
            "main.py",
            "metadata.yaml",
            "_conf_schema.json",
            "requirements.txt",
        ):
            self.assertIn(required, files)

    def test_market_watcher_modules_included(self) -> None:
        files = iter_package_files()
        py_files = [
            f for f in files if f.startswith("market_watcher/") and f.endswith(".py")
        ]
        self.assertGreater(len(py_files), 5)
        self.assertIn("market_watcher/astrbot_adapter.py", files)
        self.assertIn("market_watcher/card_renderer.py", files)
        self.assertIn("market_watcher/__init__.py", files)

    def test_docs_included(self) -> None:
        files = iter_package_files()
        doc_md = [f for f in files if f.startswith("docs/") and f.endswith(".md")]
        self.assertGreaterEqual(len(doc_md), 5)
        self.assertIn("docs/DESIGN.md", files)
        self.assertIn("docs/PRD.md", files)

    def test_excludes_dev_dirs(self) -> None:
        files = iter_package_files()
        for excluded in (
            "tests",
            "scripts",
            ".git",
            ".github",
            ".opencode",
            ".vscode",
            "tmp",
            "dist",
        ):
            for f in files:
                parts = PurePosixPath(f).parts
                self.assertNotIn(excluded, parts, f"{f} should not contain {excluded}")

    def test_excludes_pyproject_toml(self) -> None:
        files = iter_package_files()
        self.assertNotIn("pyproject.toml", files)


class DevVersionTests(unittest.TestCase):
    """--dev-version generates proper SemVer test versions."""

    def test_dev_version_format(self) -> None:
        version = build_dev_version("1.0.0", "t2i")
        self.assertRegex(version, r"^1\.0\.0-test\.\d{8}\.\d{4}\.t2i$")

    def test_dev_version_without_label(self) -> None:
        version = build_dev_version("1.0.0")
        self.assertRegex(version, r"^1\.0\.0-test\.\d{8}\.\d{4}$")

    def test_invalid_label_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_dev_version("1.0.0", "test label!")


class ArchiveDeterminismTests(unittest.TestCase):
    """ZIP determinism, top-level directory, flat mode."""

    def setUp(self) -> None:
        self.plugin_name = _plugin_name()
        self.plugin_version = _plugin_version()

    def _build_and_check(self, path: Path, *, flat: bool) -> Path:
        return build_archive(path, flat=flat)

    def test_default_top_level_is_plugin_name_not_name_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "out.zip"
            self._build_and_check(archive, flat=False)
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
            roots = {PurePosixPath(n).parts[0] for n in names if n.endswith("/")}
            self.assertIn(self.plugin_name, roots)
            # Verify no <name>-<version>/ top-level
            self.assertNotIn(f"{self.plugin_name}-{self.plugin_version}", roots)

    def test_flat_mode_no_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "flat.zip"
            self._build_and_check(archive, flat=True)
            with zipfile.ZipFile(archive) as zf:
                names = zf.namelist()
            self.assertFalse(any(n.endswith("/") for n in names))

    def test_deterministic_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a1 = Path(tmp) / "a.zip"
            a2 = Path(tmp) / "b.zip"
            self._build_and_check(a1, flat=False)
            self._build_and_check(a2, flat=False)
            self.assertEqual(a1.read_bytes(), a2.read_bytes())

    def test_no_symlinks_or_unsafe_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "safe.zip"
            self._build_and_check(archive, flat=False)
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    self.assertFalse(
                        PurePosixPath(info.filename).is_absolute(),
                        f"absolute path: {info.filename}",
                    )
                    self.assertNotIn(
                        "..",
                        PurePosixPath(info.filename).parts,
                        f"path traversal: {info.filename}",
                    )
                    mode = info.external_attr >> 16
                    self.assertFalse(stat.S_ISLNK(mode), f"symlink: {info.filename}")


class DevVersionIsolationTests(unittest.IsolatedAsyncioTestCase):
    """Dev-version patching does not modify workspace files."""

    def setUp(self) -> None:
        self.workspace_version = _plugin_version()
        self.workspace_project_version = _project_version()
        self.plugin_name = _plugin_name()

    def _workspace_files_unchanged(self) -> None:
        self.assertEqual(_plugin_version(), self.workspace_version)
        self.assertEqual(_project_version(), self.workspace_project_version)

    def test_dev_version_does_not_modify_workspace(self) -> None:
        dev_ver = build_dev_version(self.workspace_version, "t2i")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "dev.zip"
            build_archive(archive, flat=False, package_version=dev_ver)
        self._workspace_files_unchanged()

    def test_dev_archive_has_patched_version(self) -> None:
        dev_ver = build_dev_version(self.workspace_version, "t2i")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "ver.zip"
            build_archive(archive, flat=False, package_version=dev_ver)

            with zipfile.ZipFile(archive) as zf:
                meta = yaml_safe_load_bytes(
                    zf.read(f"{self.plugin_name}/metadata.yaml")
                )
                self.assertEqual(meta.get("version"), dev_ver)

                # Check main.py registration version is patched
                main_text = zf.read(f"{self.plugin_name}/main.py").decode("utf-8")
                self.assertIn(f'"{dev_ver}"', main_text)

    def test_patched_file_content_returns_string(self) -> None:
        dev_ver = build_dev_version(self.workspace_version, "t2i")
        for rel in ("metadata.yaml", "main.py"):
            content = _patched_file_content(rel, dev_ver)
            self.assertIsNotNone(content, f"{rel} should be patched")
            self.assertIsInstance(content, str)
        self.assertIsNone(_patched_file_content("requirements.txt", dev_ver))

    def test_build_without_dev_keeps_original_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "rel.zip"
            build_archive(archive, flat=False)
            with zipfile.ZipFile(archive) as zf:
                meta = yaml_safe_load_bytes(
                    zf.read(f"{self.plugin_name}/metadata.yaml")
                )
                self.assertEqual(meta.get("version"), self.workspace_version)


class ArchiveVerificationTests(unittest.TestCase):
    """Built archive passes verify_archive checks."""

    def setUp(self) -> None:
        self.plugin_name = _plugin_name()
        self.plugin_version = _plugin_version()

    def test_standard_archive_passes_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "std.zip"
            build_archive(archive, flat=False)
            verify_archive(archive, self.plugin_name, self.plugin_version, flat=False)

    def test_flat_archive_passes_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "flat.zip"
            build_archive(archive, flat=True)
            verify_archive(archive, self.plugin_name, self.plugin_version, flat=True)

    def test_dev_version_archive_passes_verification(self) -> None:
        dev_ver = build_dev_version(self.plugin_version, "t2i")
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "dev.zip"
            build_archive(archive, flat=False, package_version=dev_ver)
            verify_archive(archive, self.plugin_name, dev_ver, flat=False)

    def test_empty_archive_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "empty.zip"
            with zipfile.ZipFile(archive, "w"):
                pass
            with self.assertRaises(ValueError):
                verify_archive(
                    archive, self.plugin_name, self.plugin_version, flat=False
                )

    def test_missing_main_py_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "bad.zip"
            build_archive(archive, flat=False)
            # Remove main.py from archive
            modified = Path(tmp) / "bad2.zip"
            with zipfile.ZipFile(archive) as src, zipfile.ZipFile(modified, "w") as dst:
                for info in src.infolist():
                    if info.filename.endswith("main.py"):
                        continue
                    dst.writestr(info, src.read(info.filename))
            with self.assertRaises(ValueError):
                verify_archive(
                    modified, self.plugin_name, self.plugin_version, flat=False
                )


class DefaultOutputTests(unittest.TestCase):
    """Default output path uses <name>-<version>.zip in dist/."""

    def test_default_output_format(self) -> None:
        name = _plugin_name()
        version = _plugin_version()
        expected = f"dist/{name}-{version}.zip"
        self.assertTrue(
            hasattr(ROOT / expected, "parent"),
            f"expected default output like {expected}",
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def yaml_safe_load_bytes(data: bytes) -> dict:
    import yaml

    result = yaml.safe_load(data)
    if not isinstance(result, dict):
        raise ValueError("not a YAML object")
    return result


if __name__ == "__main__":
    unittest.main()
