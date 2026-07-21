"""Strict schema-version 1 JSON state storage with recoverable atomic writes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import ModelValidationError, WatcherState

SCHEMA_VERSION = 1


class StateError(RuntimeError):
    pass


class StateCorruptError(StateError):
    pass


class StateVersionError(StateError):
    pass


class StateWriteError(StateError):
    pass


class JsonStateStore:
    """Load and save one state file while always retaining a valid recovery copy."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.backup_path = self.path.with_name(f"{self.path.name}.bak")
        self.temp_path = self.path.with_name(f".{self.path.name}.tmp")

    def load(self) -> WatcherState:
        if not self.path.exists() and not self.backup_path.exists():
            return WatcherState()
        primary_error: Exception | None = None
        if self.path.exists():
            try:
                return self._read(self.path)
            except StateVersionError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                primary_error = exc
        if self.backup_path.exists():
            try:
                return self._read(self.backup_path)
            except StateVersionError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as backup_error:
                raise StateCorruptError(
                    "state and backup failed strict validation"
                ) from backup_error
        raise StateCorruptError(
            "state failed strict validation and no backup exists"
        ) from primary_error

    def save(self, state: WatcherState) -> None:
        if (
            type(state.schema_version) is not int
            or state.schema_version != SCHEMA_VERSION
        ):
            raise StateVersionError(
                f"unsupported state schema version: {state.schema_version!r}"
            )
        self._reject_newer_disk_schema()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        serialized = self.dumps(state)
        try:
            self._write_fsynced(self.temp_path, serialized)
            self._read(self.temp_path)

            primary_valid = self._is_valid(self.path)
            backup_valid = self._is_valid(self.backup_path)
            if primary_valid:
                self._copy_verified(self.path, self.backup_path)
            elif not backup_valid:
                self._copy_verified(self.temp_path, self.backup_path)

            try:
                os.replace(self.temp_path, self.path)
                self._fsync_directory()
                self._read(self.path)
            except Exception as exc:
                if not self._is_valid(self.backup_path):
                    raise StateWriteError(
                        "state write failed without a valid recovery copy"
                    ) from exc
                raise StateWriteError(
                    "state write failed; valid backup was preserved"
                ) from exc
        except StateWriteError:
            raise
        except Exception as exc:
            if self._is_valid(self.path) or self._is_valid(self.backup_path):
                raise StateWriteError(
                    "state write preparation failed; valid recovery data remains"
                ) from exc
            raise StateWriteError(
                "state write preparation failed without valid recovery data"
            ) from exc
        finally:
            try:
                self.temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def dumps(state: WatcherState) -> str:
        data = state.to_dict()
        WatcherState.from_dict(data)
        return (
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )

    def _read(self, path: Path) -> WatcherState:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        if type(data) is not dict or "schema_version" not in data:
            raise ModelValidationError("state root or schema_version is invalid")
        version = data["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise StateVersionError(f"unsupported state schema version: {version!r}")
        return WatcherState.from_dict(data)

    def _is_valid(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            self._read(path)
        except (OSError, ValueError, TypeError, KeyError, StateError):
            return False
        return True

    def _reject_newer_disk_schema(self) -> None:
        for path in (self.path, self.backup_path):
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if type(data) is not dict:
                continue
            version = data.get("schema_version")
            if type(version) is int and version > SCHEMA_VERSION:
                raise StateVersionError(
                    "disk state schema "
                    f"{version} is newer than supported {SCHEMA_VERSION}"
                )

    def _copy_verified(self, source: Path, destination: Path) -> None:
        self._read(source)
        destination_temp = destination.with_name(f".{destination.name}.tmp")
        try:
            with source.open("rb") as input_file, destination_temp.open("wb") as output:
                while chunk := input_file.read(65536):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            self._read(destination_temp)
            os.replace(destination_temp, destination)
            self._fsync_directory()
        finally:
            try:
                destination_temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _write_fsynced(path: Path, value: str) -> None:
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
