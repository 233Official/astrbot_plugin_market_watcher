"""Shared, read-only release metadata and safe file-selection helpers."""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_ZIP_BYTES = 16 * 1024 * 1024
FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644
REQUIRED_FILES = {
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "CHANGELOG.md",
    "_conf_schema.json",
    "docs/FSD.md",
    "docs/DESIGN.md",
    "docs/COMMANDS.md",
    "docs/CONFIGURATION.md",
    "docs/ONLINE_ACCEPTANCE.md",
    "docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md",
    "docs/PRD.md",
    "main.py",
    "metadata.yaml",
    "pyproject.toml",
    "requirements.txt",
}
PACKAGE_ROOT_FILES = {
    "CONTRIBUTING.md",
    "metadata.yaml",
    "main.py",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
}
PACKAGE_DOC_FILES = {
    "docs/COMMANDS.md",
    "docs/CONFIGURATION.md",
    "docs/DESIGN.md",
    "docs/FSD.md",
    "docs/ONLINE_ACCEPTANCE.md",
    "docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md",
    "docs/PRD.md",
}
EXCLUDED_PARTS = {
    ".git",
    ".slim",
    ".venv",
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
EXCLUDED_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".gitignore",
    ".ignore",
    "pyproject.toml",
}
ALLOWED_DIRECTORIES = {"docs", "market_watcher", "scripts", "tests"}
ALLOWED_ROOT_FILES = REQUIRED_FILES | {".gitignore", ".ignore"}
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


def verify_main_import_contract(plugin_root: Path) -> None:
    """Verify and execute main.py under AstrBot's package loading convention."""
    main_path = plugin_root / "main.py"
    _verify_sibling_import_guards(main_path)
    module_name = "data.plugins.astrbot_plugin_market_watcher.main"
    with _isolated_import_environment(plugin_root, module_name):
        spec = importlib.util.spec_from_file_location(module_name, main_path)
        if spec is None or spec.loader is None:
            raise ImportError("cannot create package-context main.py spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        sibling_name = f"{module_name.rpartition('.')[0]}.market_watcher"
        if sibling_name not in sys.modules:
            raise ImportError("package-context sibling market_watcher was not imported")


def _verify_sibling_import_guards(main_path: Path) -> None:
    tree = ast.parse(main_path.read_text(encoding="utf-8"), filename=str(main_path))
    errors: list[str] = []
    relative: set[tuple[str, tuple[str, ...]]] = set()
    absolute: set[tuple[str, tuple[str, ...]]] = set()

    def visit(nodes: list[ast.stmt], mode: str | None = None) -> None:
        for node in nodes:
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name):
                if node.test.id == "__package__":
                    visit(node.body, "package")
                    visit(node.orelse, "top-level")
                    continue
            if isinstance(node, ast.Try):
                sibling_imports = [
                    item
                    for item in ast.walk(node)
                    if isinstance(item, ast.ImportFrom)
                    and item.module
                    and item.module.startswith("market_watcher")
                ]
                if sibling_imports:
                    errors.append("sibling imports must not use try/except fallback")
            if isinstance(node, ast.ImportFrom) and node.module:
                module = node.module
                if module.startswith("market_watcher"):
                    key = (module, tuple(alias.name for alias in node.names))
                    if node.level == 1 and mode == "package":
                        relative.add(key)
                    elif node.level == 0 and mode == "top-level":
                        absolute.add(key)
                    else:
                        errors.append(
                            f"unguarded sibling import at line {node.lineno}: {module}"
                        )
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    visit(child.body, mode)

    visit(tree.body)
    if relative != absolute:
        errors.append("package-relative and top-level sibling import sets differ")
    if not relative:
        errors.append("no guarded market_watcher sibling imports found")
    if errors:
        raise ValueError("; ".join(errors))


@contextmanager
def _isolated_import_environment(plugin_root: Path, module_name: str) -> Iterator[None]:
    prefixes = ("data", "astrbot")
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == prefixes[0]
        or name.startswith(f"{prefixes[0]}.")
        or name == prefixes[1]
        or name.startswith(f"{prefixes[1]}.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        _install_package_hierarchy(plugin_root, module_name)
        _install_astrbot_stubs()
        yield
    finally:
        for name in list(sys.modules):
            if (
                name == prefixes[0]
                or name.startswith(f"{prefixes[0]}.")
                or name == prefixes[1]
                or name.startswith(f"{prefixes[1]}.")
            ):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


def _install_package_hierarchy(plugin_root: Path, module_name: str) -> None:
    package_name = module_name.rpartition(".")[0]
    package_parts = package_name.split(".")
    for index in range(1, len(package_parts) + 1):
        name = ".".join(package_parts[:index])
        module = ModuleType(name)
        module.__package__ = name
        module.__path__ = [str(plugin_root)] if index == len(package_parts) else []
        sys.modules[name] = module


def _install_astrbot_stubs() -> None:
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    event = ModuleType("astrbot.api.event")
    star = ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context) -> None:
            self.context = context

    def identity_decorator(*args, **kwargs):
        del args, kwargs

        def decorate(value):
            return value

        return decorate

    def command_group(*args, **kwargs):
        del args, kwargs

        def decorate(function):
            function.command = identity_decorator
            return function

        return decorate

    setattr(api, "AstrBotConfig", dict)
    setattr(
        api,
        "logger",
        SimpleNamespace(info=lambda *args: None, error=lambda *args: None),
    )
    setattr(event, "AstrMessageEvent", object)
    setattr(
        event,
        "filter",
        SimpleNamespace(
            command_group=command_group,
            permission_type=identity_decorator,
            PermissionType=SimpleNamespace(ADMIN="ADMIN"),
        ),
    )
    setattr(star, "Context", object)
    setattr(star, "Star", Star)
    setattr(star, "StarTools", SimpleNamespace(get_data_dir=lambda name: Path(name)))
    setattr(star, "register", identity_decorator)
    setattr(astrbot, "api", api)
    setattr(api, "event", event)
    setattr(api, "star", star)
    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )


def simple_yaml_bytes(value: bytes) -> dict[str, str]:
    return simple_yaml_text(value.decode("utf-8"))


def simple_yaml(path: Path) -> dict[str, str]:
    return simple_yaml_text(path.read_text(encoding="utf-8"))


def simple_yaml_text(value: str) -> dict[str, str]:
    result = {}
    for line in value.splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, item = line.split(":", 1)
        result[key.strip()] = item.strip().strip("\"'")
    return result


def release_identity() -> tuple[str, str]:
    metadata = simple_yaml(ROOT / "metadata.yaml")
    name = metadata.get("name", "")
    version = metadata.get("version", "")
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("metadata name is invalid")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("metadata version is invalid")
    main_source = (ROOT / "main.py").read_text(encoding="utf-8")
    registered = re.search(
        r'@register\(.*?"(?P<version>\d+\.\d+\.\d+)"\s*,?\s*\)',
        main_source,
        re.S,
    )
    if not registered or registered.group("version") != version:
        raise ValueError("metadata.yaml and main.py versions differ")
    return name, version


def package_source_files() -> list[Path]:
    candidates = {ROOT / path for path in PACKAGE_ROOT_FILES | PACKAGE_DOC_FILES}
    package_root = ROOT / "market_watcher"
    candidates.update(package_root.rglob("*.py"))
    files = _safe_existing_files(candidates)
    expected = PACKAGE_ROOT_FILES | PACKAGE_DOC_FILES
    missing = sorted(path for path in expected if ROOT / path not in files)
    if missing:
        raise FileNotFoundError(f"missing package files: {', '.join(missing)}")
    return files


def verification_files() -> list[Path]:
    tracked = (
        subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        .stdout.decode("utf-8")
        .split("\0")
    )
    candidates = {ROOT / path for path in tracked if path}
    candidates.update(ROOT / path for path in ALLOWED_ROOT_FILES)
    for directory in ALLOWED_DIRECTORIES:
        root = ROOT / directory
        if root.is_dir():
            candidates.update(root.rglob("*"))
    return _safe_existing_files(candidates, allow_development=True)


def _safe_existing_files(
    candidates: set[Path], *, allow_development: bool = False
) -> list[Path]:
    files = []
    for path in candidates:
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(ROOT)
        excluded_parts = EXCLUDED_PARTS - (
            {"scripts", "tests"} if allow_development else set()
        )
        excluded_names = EXCLUDED_NAMES - (
            {".gitignore", ".ignore", "pyproject.toml"} if allow_development else set()
        )
        if any(part in excluded_parts for part in relative.parts):
            continue
        if path.name in excluded_names or path.name.endswith((".pyc", ".pyo")):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(ROOT).as_posix())
