#!/usr/bin/env python3
"""Package this AstrBot plugin into an installable deterministic zip archive.

Default output: ``dist/<metadata.name>-<metadata.version>.zip``
The archive has a top-level directory ``<metadata.name>/`` (not ``<name>-<version>/``)
so that AstrBot WebUI (v4.24.2+) can install it correctly.

Version-bearing files inside the zip are patched when ``--dev-version``,
``--package-version``, or ``--test-label`` is used; workspace files are never
modified.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import sys
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

try:
    import yaml
except ImportError as exc:  # pragma: no cover – PyYAML exists in AstrBot envs.
    raise SystemExit("PyYAML is required to read metadata.yaml") from exc

from release_common import (
    FIXED_TIMESTAMP,
    FILE_MODE,
    MAX_ZIP_BYTES,
    ROOT,
    verify_main_import_contract,
)

DIST_DIR = ROOT / "dist"

PACKAGE_ROOT_FILES = [
    "CONTRIBUTING.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "_conf_schema.json",
    "main.py",
    "metadata.yaml",
    "requirements.txt",
]

PACKAGE_MODULE_DIRS = ["market_watcher"]

PACKAGE_DOC_DIRS = ["docs"]

EXCLUDED_FILE_PARTS = {
    ".git",
    ".github",
    ".opencode",
    ".slim",
    ".venv",
    ".vscode",
    "__pycache__",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "cache",
    "deepwork",
    "dist",
    "scripts",
    "tests",
    "tmp",
}

EXCLUDED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".gitignore",
    ".ignore",
    "pyproject.toml",
    ".markdownlint-cli2.yaml",
}

EXCLUDED_FILE_SUFFIXES = {".pyc", ".pyo"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_metadata() -> dict:
    path = ROOT / "metadata.yaml"
    with path.open("r", encoding="utf-8") as fh:
        meta = yaml.safe_load(fh)
    if not isinstance(meta, dict):
        raise ValueError("metadata.yaml must contain a YAML object")
    return meta


def _plugin_name() -> str:
    name = _read_metadata().get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("metadata.yaml must define a non-empty name")
    return name.strip()


def _plugin_version() -> str:
    version = _read_metadata().get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("metadata.yaml must define a non-empty version")
    return version.strip()


def _project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"\s*$', text, re.MULTILINE)
    if not match:
        raise ValueError("pyproject.toml must define project.version")
    return match.group(1)


# ---------------------------------------------------------------------------
# file selection
# ---------------------------------------------------------------------------


def iter_package_files() -> list[str]:
    """Return the sorted list of relative paths to include in the archive."""
    files: list[str] = list(PACKAGE_ROOT_FILES)

    for mod_dir in PACKAGE_MODULE_DIRS:
        pkg = ROOT / mod_dir
        if not pkg.is_dir():
            raise FileNotFoundError(f"Missing package module directory: {mod_dir}")
        files.extend(
            path.relative_to(ROOT).as_posix()
            for path in sorted(pkg.rglob("*.py"))
            if path.is_file()
        )

    for doc_dir in PACKAGE_DOC_DIRS:
        doc = ROOT / doc_dir
        if not doc.is_dir():
            continue
        files.extend(
            path.relative_to(ROOT).as_posix()
            for path in sorted(doc.rglob("*.md"))
            if path.is_file()
        )

    # Filter out excluded entries
    result: list[str] = []
    excluded_parts = EXCLUDED_FILE_PARTS
    excluded_names = EXCLUDED_FILE_NAMES
    excluded_suffixes = EXCLUDED_FILE_SUFFIXES

    for relative in files:
        parts = PurePosixPath(relative).parts
        if any(part in excluded_parts for part in parts):
            continue
        if PurePosixPath(relative).name in excluded_names:
            continue
        if PurePosixPath(relative).suffix in excluded_suffixes:
            continue
        result.append(relative)

    return sorted(result)


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build_archive(
    output: Path,
    *,
    flat: bool,
    package_version: str | None = None,
) -> Path:
    """Build the deterministic ZIP archive.

    Parameters
    ----------
    output : Path
        Destination path for the zip file.
    flat : bool
        If True, omit the top-level plugin directory (legacy layout).
    package_version : str, optional
        Override version string patched into version-bearing files inside the
        zip.  When None (default) the workspace version is used as-is.
    """
    plugin_name = _plugin_name()
    source_version = _plugin_version()
    package_version = package_version or source_version
    patch_versions = package_version != source_version

    # Collect files early to fail fast on missing entries
    package_files = iter_package_files()
    missing = [path for path in package_files if not (ROOT / path).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing package file(s): {', '.join(missing)}")

    output.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        # AstrBot v4.24.2+ WebUI uses the first entry's top-level directory as
        # the extraction root.  Write an explicit directory entry.
        if not flat:
            archive.writestr(f"{plugin_name}/", "")

        for relative in package_files:
            source = ROOT / relative
            archive_name = relative if flat else f"{plugin_name}/{relative}"

            content: str | None = (
                _patched_file_content(relative, package_version)
                if patch_versions
                else None
            )

            info = zipfile.ZipInfo(archive_name, date_time=FIXED_TIMESTAMP)
            info.create_system = 3
            info.external_attr = FILE_MODE << 16
            info.compress_type = zipfile.ZIP_DEFLATED

            if content is None:
                archive.writestr(info, source.read_bytes(), compresslevel=9)
            else:
                archive.writestr(info, content.encode("utf-8"), compresslevel=9)

    return output


def _patched_file_content(relative: str, package_version: str) -> str | None:
    """Return patched file content for version-bearing files.

    Workspace files are never modified – only the in-memory zip content is
    patched.
    """
    if relative == "metadata.yaml":
        meta = _read_metadata()
        meta["version"] = package_version
        return yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)

    source = ROOT / relative

    if relative == "main.py":
        text = source.read_text(encoding="utf-8")
        source_version = _plugin_version()
        old = f'"{source_version}"'
        new = f'"{package_version}"'
        if old not in text:
            raise ValueError(
                f"Cannot patch plugin version in {relative}: {old!r} not found"
            )
        return text.replace(old, new, 1)

    if relative == "pyproject.toml":
        text = source.read_text(encoding="utf-8")
        source_version = _project_version()
        target_version = _project_version_for_package(package_version)
        old = f'version = "{source_version}"'
        new = f'version = "{target_version}"'
        if old not in text:
            raise ValueError(
                f"Cannot patch project version in {relative}: {old!r} not found"
            )
        return text.replace(old, new, 1)

    return None


def _project_version_for_package(package_version: str) -> str:
    """Return a valid PEP 440 version for the pyproject.toml inside the zip."""
    try:
        from packaging.version import Version as PepVersion

        return str(PepVersion(package_version.removeprefix("v")))
    except ImportError:
        pass
    # Simple fallback: keep as-is if packaging is unavailable
    return package_version.removeprefix("v")


# ---------------------------------------------------------------------------
# dev-version helpers
# ---------------------------------------------------------------------------


def build_dev_version(base_version: str, test_label: str | None = None) -> str:
    """Return a SemVer-compatible temporary version based on local time.

    Uses ``-test.`` instead of ``-dev.`` because AstrBot's current version
    comparator strips all ``v`` characters.
    """
    stamp = datetime.now().strftime("%Y%m%d.%H%M")
    version = f"{base_version}-test.{stamp}"
    if test_label:
        if not re.fullmatch(r"[0-9A-Za-z-]+", test_label):
            raise ValueError(
                "--test-label must contain only ASCII letters, digits, and hyphens"
            )
        version = f"{version}.{test_label}"
    return version


# ---------------------------------------------------------------------------
# verification (run inside the just-built zip)
# ---------------------------------------------------------------------------


def verify_archive(
    path: Path,
    plugin_name: str,
    package_version: str,
    flat: bool,
) -> None:
    """Run deterministic and security checks on the built archive."""
    if path.stat().st_size >= MAX_ZIP_BYTES:
        raise ValueError(f"archive exceeds {MAX_ZIP_BYTES} bytes")

    top = plugin_name if not flat else ""

    with zipfile.ZipFile(path) as bundle:
        infos = bundle.infolist()
        if not infos:
            raise ValueError("archive is empty")

        names = [info.filename for info in infos]
        roots = {PurePosixPath(item).parts[0] for item in names}

        if flat:
            # Flat archive: no top-level directory expected.
            # The first entry is not a directory entry.
            pass
        else:
            if roots != {top}:
                raise ValueError(
                    f"archive must contain exactly one top-level directory {top!r}; "
                    f"got {roots}"
                )

        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"unsafe archive member: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink archive member: {info.filename}")

        if not flat:
            relative_names = {
                PurePosixPath(item).relative_to(top).as_posix() for item in names
            }
        else:
            relative_names = set(names)

        # Check required files
        required = {
            "metadata.yaml",
            "main.py",
            "_conf_schema.json",
            "requirements.txt",
        }
        if not required.issubset(relative_names):
            raise ValueError(
                f"archive missing required files: {required - relative_names}"
            )

        # Check no forbidden members
        forbidden_parts = {
            "tests",
            "scripts",
            "dist",
            ".git",
            ".github",
            ".opencode",
            ".vscode",
            "tmp",
            ".slim",
            "__pycache__",
        }
        forbidden_names = {"pyproject.toml", ".gitignore", ".ignore"}
        for item in relative_names:
            parts = PurePosixPath(item).parts
            if any(part in forbidden_parts for part in parts):
                raise ValueError(f"forbidden archive member: {item}")
            if PurePosixPath(item).name in forbidden_names:
                raise ValueError(f"forbidden archive member: {item}")
            if PurePosixPath(item).suffix in {".pyc", ".pyo"}:
                raise ValueError(f"forbidden archive member: {item}")

        # Check metadata identity
        metadata_member = f"{top}/metadata.yaml" if not flat else "metadata.yaml"
        meta_raw = bundle.read(metadata_member)
        meta_parsed = yaml.safe_load(meta_raw)
        if not isinstance(meta_parsed, dict):
            raise ValueError("metadata.yaml is not a YAML object")
        if meta_parsed.get("name") != plugin_name:
            raise ValueError(
                f"archive metadata name mismatch: "
                f"{meta_parsed.get('name')!r} != {plugin_name!r}"
            )
        if meta_parsed.get("version") != package_version:
            raise ValueError(
                f"archive metadata version mismatch: "
                f"{meta_parsed.get('version')!r} != {package_version!r}"
            )

        # Package-context import contract
        with tempfile.TemporaryDirectory() as directory:
            bundle.extractall(directory)
            extract_root = Path(directory) / (top if not flat else "")
            verify_main_import_contract(extract_root)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    plugin_name = _plugin_name()
    plugin_version = _plugin_version()
    default_output = DIST_DIR / f"{plugin_name}-{plugin_version}.zip"
    parser = argparse.ArgumentParser(
        description="Package the AstrBot Market Watcher plugin into a zip archive.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=default_output,
        help=f"Output zip path. Defaults to {default_output}",
    )
    parser.add_argument(
        "--flat",
        action="store_true",
        help=(
            "Build a legacy flat archive without the top-level plugin directory. "
            "Do not use this for AstrBot WebUI upload installation on v4.24.2+."
        ),
    )
    parser.add_argument(
        "--dev-version",
        action="store_true",
        help=(
            "Build a temporary test package with a SemVer prerelease timestamp. "
            "Zip-internal version-bearing files are patched, but workspace files "
            "are not modified."
        ),
    )
    parser.add_argument(
        "--test-label",
        type=str,
        default=None,
        help=(
            "Append a recognizable label to --dev-version, e.g. t2i or scheduler. "
            "Requires --dev-version."
        ),
    )
    parser.add_argument(
        "--package-version",
        type=str,
        default=None,
        help=(
            "Override the version written into the zip package. "
            "Workspace files are not modified."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    plugin_name = _plugin_name()
    source_version = _plugin_version()
    package_version: str | None = args.package_version

    if args.test_label and not args.dev_version:
        print("FAIL: --test-label requires --dev-version", file=sys.stderr)
        return 1
    if args.dev_version:
        if package_version is not None:
            print(
                "FAIL: --dev-version and --package-version cannot be used together",
                file=sys.stderr,
            )
            return 1
        package_version = build_dev_version(source_version, args.test_label)

    # Adjust default output when version differs from source
    if (
        package_version
        and package_version != source_version
        and args.output == DIST_DIR / f"{plugin_name}-{source_version}.zip"
    ):
        args.output = DIST_DIR / f"{plugin_name}-{package_version}.zip"

    # Build
    temporary = args.output.with_suffix(".zip.tmp")
    temporary.unlink(missing_ok=True)
    try:
        build_archive(temporary, flat=args.flat, package_version=package_version)
        verify_archive(
            temporary,
            plugin_name,
            package_version or source_version,
            flat=args.flat,
        )

        # SHA-256 sidecar
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        checksum = args.output.with_suffix(".zip.sha256")
        checksum_tmp = Path(f"{checksum}.tmp")
        checksum_tmp.write_text(
            f"{digest}  {args.output.name}\n", encoding="ascii", newline="\n"
        )

        os.replace(temporary, args.output)
        os.replace(checksum_tmp, checksum)

        size = args.output.stat().st_size
        top_note = f"top={plugin_name}/" if not args.flat else "flat"
        print(f"PASS: {args.output} size={size} sha256={digest} {top_note}")
    except Exception as exc:
        for p in (temporary, args.output, args.output.with_suffix(".zip.sha256")):
            p.unlink(missing_ok=True)
        print(f"FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
