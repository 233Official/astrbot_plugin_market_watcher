"""Build and self-verify a deterministic AstrBot WebUI release archive."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from release_common import (
    MAX_ZIP_BYTES,
    PACKAGE_DOC_FILES,
    PACKAGE_ROOT_FILES,
    ROOT,
    package_source_files,
    release_identity,
    simple_yaml_bytes,
    verify_main_import_contract,
)

FIXED_TIMESTAMP = (2020, 1, 1, 0, 0, 0)
FILE_MODE = 0o100644


def main() -> int:
    name, version = release_identity()
    top_level = f"{name}-{version}"
    dist = ROOT / "dist"
    archive = dist / f"{top_level}.zip"
    checksum = archive.with_suffix(".zip.sha256")
    temporary = archive.with_suffix(".zip.tmp")
    checksum_temporary = Path(f"{checksum}.tmp")
    dist.mkdir(parents=True, exist_ok=True)
    for path in (temporary, checksum_temporary):
        path.unlink(missing_ok=True)
    try:
        _write_archive(temporary, top_level)
        _verify_archive(temporary, name, version, top_level)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        checksum_temporary.write_text(
            f"{digest}  {archive.name}\n", encoding="ascii", newline="\n"
        )
        os.replace(temporary, archive)
        os.replace(checksum_temporary, checksum)
    except Exception as exc:
        for path in (temporary, checksum_temporary, archive, checksum):
            path.unlink(missing_ok=True)
        print(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    print(
        f"PASS: {archive} size={archive.stat().st_size} sha256={digest} "
        f"top={top_level}/"
    )
    return 0


def _write_archive(path: Path, top_level: str) -> None:
    with zipfile.ZipFile(
        path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as bundle:
        for source in package_source_files():
            relative = source.relative_to(ROOT).as_posix()
            member = f"{top_level}/{relative}"
            info = zipfile.ZipInfo(member, date_time=FIXED_TIMESTAMP)
            info.create_system = 3
            info.external_attr = FILE_MODE << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(info, source.read_bytes(), compresslevel=9)


def _verify_archive(path: Path, name: str, version: str, top_level: str) -> None:
    if path.stat().st_size >= MAX_ZIP_BYTES:
        raise ValueError("archive is 16 MiB or larger")
    with zipfile.ZipFile(path) as bundle:
        infos = bundle.infolist()
        if not infos:
            raise ValueError("archive is empty")
        names = [info.filename for info in infos]
        roots = {PurePosixPath(item).parts[0] for item in names}
        if roots != {top_level}:
            raise ValueError("archive must contain exactly one top-level directory")
        for info in infos:
            member = PurePosixPath(info.filename)
            if member.is_absolute() or ".." in member.parts:
                raise ValueError(f"unsafe archive member: {info.filename}")
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ValueError(f"symlink archive member: {info.filename}")
        relative_names = {
            PurePosixPath(item).relative_to(top_level).as_posix() for item in names
        }
        required = PACKAGE_ROOT_FILES | PACKAGE_DOC_FILES
        if not required.issubset(relative_names):
            raise ValueError("archive is missing required files")
        for item in relative_names:
            parts = PurePosixPath(item).parts
            if (
                not parts
                or parts[0] in {"tests", "scripts", "dist", ".git", ".slim"}
                or item in {"pyproject.toml", ".gitignore", ".ignore"}
                or any(part in {"__pycache__", "deepwork", ".venv"} for part in parts)
                or PurePosixPath(item).name.startswith(".env")
                or PurePosixPath(item).suffix in {".pyc", ".pyo"}
            ):
                raise ValueError(f"forbidden archive member: {item}")
        metadata_member = f"{top_level}/metadata.yaml"
        metadata = simple_yaml_bytes(bundle.read(metadata_member))
        if metadata.get("name") != name or metadata.get("version") != version:
            raise ValueError("archive metadata identity mismatch")
        if not any(item == f"{top_level}/main.py" for item in names):
            raise ValueError("archive main.py is missing")
        with tempfile.TemporaryDirectory() as directory:
            bundle.extractall(directory)
            verify_main_import_contract(Path(directory) / top_level)


if __name__ == "__main__":
    raise SystemExit(main())
