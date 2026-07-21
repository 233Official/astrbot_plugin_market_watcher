"""Offline, read-only release readiness checks for the plugin repository."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 CI path
    import tomli as tomllib

from release_common import (
    MAX_FILE_BYTES,
    MAX_ZIP_BYTES,
    REQUIRED_FILES,
    ROOT,
    release_identity,
    simple_yaml,
    verification_files,
    verify_main_import_contract,
)

REQUIRED_CONFIG = {
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
    "include_star_count",
    "enable_image_card",
    "image_render_timeout_seconds",
}
CREDENTIAL_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{50,}"),
    re.compile(r"gh[opusr]_[A-Za-z0-9]{36,}"),
    re.compile(r"sk-[A-Za-z0-9]{32,}"),
)


def main() -> int:
    errors: list[str] = []
    files = verification_files()
    missing = sorted(path for path in REQUIRED_FILES if not (ROOT / path).is_file())
    if missing:
        errors.append(f"缺少必需文件：{', '.join(missing)}")

    metadata = simple_yaml(ROOT / "metadata.yaml")
    try:
        verified_name, verified_version = release_identity()
    except ValueError as exc:
        errors.append(str(exc))
        verified_version = ""

    if not verified_version:
        errors.append("无法确定有效的发布版本")
    else:
        # Verify pyproject.toml matches metadata version
        with (ROOT / "pyproject.toml").open("rb") as handle:
            project_version = tomllib.load(handle)["project"]["version"]
        # pyproject version may have PEP 440 normalization differences, but should
        # be equivalent to the metadata version (minus any 'v' prefix)
        if project_version != verified_version:
            # Also try without 'v' prefix if present
            if project_version != verified_version.removeprefix("v"):
                errors.append(
                    f"pyproject.toml 版本 ({project_version}) 与 "
                    f"metadata/main 版本 ({verified_version}) 不一致"
                )

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    missing_config = sorted(REQUIRED_CONFIG - schema.keys())
    if missing_config:
        errors.append(f"配置 schema 缺少：{', '.join(missing_config)}")
    if schema.get("github_token", {}).get("default") != "":
        errors.append("github_token 默认值必须为空")
    if schema.get("push_targets", {}).get("default") != []:
        errors.append("push_targets 默认值必须为空数组")
    ai_timeout = schema.get("ai_timeout_seconds", {})
    if ai_timeout.get("default") != 60 or ai_timeout.get("slider") != {
        "min": 10,
        "max": 120,
        "step": 5,
    }:
        errors.append("ai_timeout_seconds 必须为默认 60、范围 10 至 120、步长 5")

    for markdown in (
        ROOT / "CONTRIBUTING.md",
        ROOT / "README.md",
        ROOT / "docs/COMMANDS.md",
        ROOT / "docs/CONFIGURATION.md",
        ROOT / "docs/DESIGN.md",
        ROOT / "docs/FSD.md",
        ROOT / "docs/ONLINE_ACCEPTANCE.md",
        ROOT / "docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md",
        ROOT / "docs/PRD.md",
    ):
        errors.extend(_broken_links(markdown))

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_targets = {
        target.split("#", 1)[0].removeprefix("./")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", readme)
    }
    for required_link in (
        "CONTRIBUTING.md",
        "docs/COMMANDS.md",
        "docs/CONFIGURATION.md",
        "docs/DESIGN.md",
        "docs/ONLINE_ACCEPTANCE.md",
    ):
        if required_link not in readme_targets:
            errors.append(f"README 缺少文档入口：{required_link}")

    for path in files:
        size = path.stat().st_size
        relative = path.relative_to(ROOT)
        if size > MAX_FILE_BYTES:
            errors.append(f"文件过大：{relative} ({size} bytes)")
        if size <= 1024 * 1024:
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if any(pattern.search(text) for pattern in CREDENTIAL_PATTERNS):
                errors.append(f"疑似凭据：{relative}")

    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "plugin.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in files:
                bundle.write(path, path.relative_to(ROOT))
        if archive.stat().st_size >= MAX_ZIP_BYTES:
            errors.append(f"zip 超过 16 MiB：{archive.stat().st_size} bytes")
        zip_size = archive.stat().st_size
        extracted = Path(directory) / "extracted"
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(extracted)
        try:
            verify_main_import_contract(extracted)
        except (ImportError, OSError, SyntaxError, ValueError) as exc:
            errors.append(f"包上下文导入契约失败：{type(exc).__name__}: {exc}")

    tests = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if tests.returncode:
        errors.append("默认 unittest 命令失败")
        print("\n".join((tests.stdout + tests.stderr).splitlines()[-20:]))

    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(
        "PASS: release readiness offline checks; "
        f"version={metadata.get('version')}; files={len(files)}; zip={zip_size} bytes"
    )
    return 0


def _broken_links(path: Path) -> list[str]:
    errors = []
    text = path.read_text(encoding="utf-8")
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        clean = target.split("#", 1)[0]
        if not clean or "://" in clean or clean.startswith("mailto:"):
            continue
        if not (path.parent / clean).resolve().exists():
            errors.append(f"失效文档链接：{path.relative_to(ROOT)} -> {target}")
    return errors


if __name__ == "__main__":
    raise SystemExit(main())
