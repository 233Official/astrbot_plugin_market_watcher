from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from market_watcher.models import (
    EndpointSnapshot,
    ModelValidationError,
    PluginRecord,
    SourceEvidence,
    SourceKind,
    SourceObservation,
    SourceState,
    WatcherState,
)
from market_watcher.state import (
    JsonStateStore,
    StateCorruptError,
    StateVersionError,
    StateWriteError,
)

ENDPOINT = "https://api.soulter.top/astrbot/plugins"


def sample_state(version: str = "1.0.0") -> WatcherState:
    evidence = SourceEvidence(
        source_kind=SourceKind.MARKET,
        source_record_id="demo",
        source_url="https://github.com/example/astrbot_plugin_demo",
        observed_at="2026-07-20T00:00:00Z",
    )
    observation = SourceObservation(
        source_kind=SourceKind.MARKET,
        source_record_id="demo",
        source_url=evidence.source_url,
        observed_at=evidence.observed_at,
        fetched_from=ENDPOINT,
        canonical_id="github:example/astrbot_plugin_demo",
        repo_url=evidence.source_url,
        repo_owner="example",
        repo_name="astrbot_plugin_demo",
        name="astrbot_plugin_demo",
        version=version,
        platforms=("aiocqhttp",),
        observation_hash=f"sha256:{version}",
    )
    return WatcherState(
        updated_at="2026-07-20T00:00:00Z",
        sources={
            "market": SourceState(
                baseline_established=True,
                complete=True,
                observations={"demo": observation},
                snapshots={
                    ENDPOINT: EndpointSnapshot(
                        endpoint=ENDPOINT,
                        etag='"etag"',
                        observations={"demo": observation},
                    )
                },
            )
        },
        plugins={
            observation.canonical_id: PluginRecord(
                canonical_id=observation.canonical_id,
                name="astrbot_plugin_demo",
                repo_url=evidence.source_url,
                repo_owner="example",
                repo_name="astrbot_plugin_demo",
                version=version,
                platforms=("aiocqhttp",),
                observed_at=evidence.observed_at,
                evidence=(evidence,),
                field_sources={"version": evidence},
            )
        },
    )


class StateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.path = Path(self.tempdir.name) / "nested" / "state.json"
        self.store = JsonStateStore(self.path)

    def test_first_load_returns_default_without_creating_files(self) -> None:
        self.assertEqual(self.store.load(), WatcherState())
        self.assertFalse(self.path.exists())

    def test_round_trip_and_stable_serialization(self) -> None:
        state = sample_state()
        self.store.save(state)
        first = self.path.read_text(encoding="utf-8")
        loaded = self.store.load()
        self.assertEqual(loaded, state)
        self.store.save(loaded)
        self.assertEqual(first, self.path.read_text(encoding="utf-8"))

    def test_rejects_missing_top_level_fields_and_loose_types(self) -> None:
        self.path.parent.mkdir(parents=True)
        bad_states = [
            {"schema_version": 1},
            {**WatcherState().to_dict(), "schema_version": True},
            {
                **WatcherState().to_dict(),
                "sources": {
                    "market": {
                        "baseline_established": 1,
                        "last_success_at": None,
                        "complete": False,
                        "error_code": None,
                        "observations": {},
                        "snapshots": {},
                    }
                },
            },
        ]
        for value in bad_states:
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises((StateCorruptError, StateVersionError)):
                    self.store.load()

    def test_oversized_raw_excerpt_is_rejected_before_save(self) -> None:
        state = sample_state()
        state.sources["market"].observations["demo"].raw_excerpt = {"body": "x" * 9000}
        with self.assertRaises(ModelValidationError):
            self.store.save(state)

    def test_corrupt_primary_recovers_from_backup(self) -> None:
        self.store.save(sample_state("1.0.0"))
        self.store.save(sample_state("2.0.0"))
        self.path.write_text("{broken", encoding="utf-8")
        recovered = self.store.load()
        self.assertEqual(
            recovered.plugins["github:example/astrbot_plugin_demo"].version,
            "1.0.0",
        )

    def test_both_corrupt_raise_without_rebuilding(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{broken", encoding="utf-8")
        self.store.backup_path.write_text("[]", encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            self.store.load()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{broken")

    def test_rejects_lower_and_higher_schema_versions(self) -> None:
        self.path.parent.mkdir(parents=True)
        for version in (0, 2):
            with self.subTest(version=version):
                value = WatcherState().to_dict()
                value["schema_version"] = version
                self.path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(StateVersionError):
                    self.store.load()

    def test_save_refuses_newer_schema_without_changing_disk_bytes(self) -> None:
        self.path.parent.mkdir(parents=True)
        for newer_path in (self.path, self.store.backup_path):
            with self.subTest(newer_path=newer_path):
                self.path.unlink(missing_ok=True)
                self.store.backup_path.unlink(missing_ok=True)
                newer_path.write_bytes(b'{"schema_version":2,"future":true}\n')
                other_path = (
                    self.store.backup_path if newer_path == self.path else self.path
                )
                other_path.write_bytes(b"{broken")
                before = {
                    self.path: self.path.read_bytes(),
                    self.store.backup_path: self.store.backup_path.read_bytes(),
                }
                with self.assertRaises(StateVersionError):
                    self.store.save(sample_state())
                self.assertEqual(self.path.read_bytes(), before[self.path])
                self.assertEqual(
                    self.store.backup_path.read_bytes(),
                    before[self.store.backup_path],
                )

    def test_snapshot_source_kind_must_match_outer_source(self) -> None:
        value = sample_state().to_dict()
        value["sources"]["market"]["snapshots"][ENDPOINT]["observations"]["demo"][
            "source_kind"
        ] = SourceKind.COLLECTION_ISSUE.value
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(StateCorruptError):
            self.store.load()

    def test_primary_replace_failure_preserves_previous_state(self) -> None:
        self.store.save(sample_state("1.0.0"))
        real_replace = os.replace

        def fail_primary(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return real_replace(source, destination)

        with mock.patch("market_watcher.state.os.replace", side_effect=fail_primary):
            with self.assertRaises(StateWriteError):
                self.store.save(sample_state("2.0.0"))
        self.assertEqual(
            self.store.load().plugins["github:example/astrbot_plugin_demo"].version,
            "1.0.0",
        )

    def test_cleanup_error_does_not_override_state_write_error(self) -> None:
        self.store.save(sample_state("1.0.0"))
        real_replace = os.replace

        def fail_primary(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return real_replace(source, destination)

        with (
            mock.patch("market_watcher.state.os.replace", side_effect=fail_primary),
            mock.patch("pathlib.Path.unlink", side_effect=OSError("cleanup failed")),
        ):
            with self.assertRaises(StateWriteError):
                self.store.save(sample_state("2.0.0"))

    def test_backup_recovery_then_failed_save_does_not_overwrite_backup(self) -> None:
        self.store.save(sample_state("1.0.0"))
        self.store.save(sample_state("2.0.0"))
        self.path.write_text("{broken", encoding="utf-8")
        backup_before = self.store.backup_path.read_bytes()
        real_replace = os.replace

        def fail_primary(source, destination):
            if Path(destination) == self.path:
                raise OSError("replace failed")
            return real_replace(source, destination)

        with mock.patch("market_watcher.state.os.replace", side_effect=fail_primary):
            with self.assertRaises(StateWriteError):
                self.store.save(sample_state("3.0.0"))
        self.assertEqual(backup_before, self.store.backup_path.read_bytes())
        self.assertEqual(
            self.store.load().plugins["github:example/astrbot_plugin_demo"].version,
            "1.0.0",
        )

    def test_post_replace_validation_failure_retains_valid_backup(self) -> None:
        self.store.save(sample_state("1.0.0"))
        original_read = self.store._read
        real_replace = os.replace
        replaced_primary = False

        def tracking_replace(source, destination):
            nonlocal replaced_primary
            result = real_replace(source, destination)
            if Path(destination) == self.path:
                replaced_primary = True
            return result

        def fail_post_validation(path):
            if replaced_primary and Path(path) == self.path:
                raise ModelValidationError("simulated post-write validation failure")
            return original_read(path)

        with (
            mock.patch("market_watcher.state.os.replace", side_effect=tracking_replace),
            mock.patch.object(self.store, "_read", side_effect=fail_post_validation),
        ):
            with self.assertRaises(StateWriteError):
                self.store.save(sample_state("2.0.0"))
        self.assertTrue(JsonStateStore(self.path)._is_valid(self.store.backup_path))


if __name__ == "__main__":
    unittest.main()
