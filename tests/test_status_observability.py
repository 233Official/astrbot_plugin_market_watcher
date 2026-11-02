from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from market_watcher.config import parse_runtime_config
from market_watcher.models import (
    DeliveryBatch,
    DeliveryTargetState,
    EndpointSnapshot,
    ImageDeliveryWindow,
    PluginRecord,
    SourceKind,
    SourceObservation,
    SourceState,
    WatcherState,
)
from market_watcher.status import (
    StateDiagnostics,
    collect_state_diagnostics,
    format_source_errors,
    format_source_states,
    format_status,
    unavailable_state_diagnostics,
)

NOW = "2026-07-20T12:00:00Z"


def observation() -> SourceObservation:
    return SourceObservation(
        source_kind=SourceKind.MARKET,
        source_record_id="demo",
        source_url="https://example.invalid/plugin/demo",
        observed_at=NOW,
        fetched_from="https://example.invalid/api",
        canonical_id="github:owner/demo",
        repo_url="https://github.com/owner/demo",
        repo_owner="owner",
        repo_name="demo",
        name="demo",
        version="1.0.0",
        observation_hash="sha256:demo",
    )


def observable_state() -> WatcherState:
    item = observation()
    source = SourceState(
        baseline_established=True,
        last_success_at=NOW,
        complete=True,
        observations={item.source_record_id: item},
        snapshots={
            "https://example.invalid/api": EndpointSnapshot(
                endpoint="https://example.invalid/api",
                observations={item.source_record_id: item},
            )
        },
    )
    batch = DeliveryBatch(
        batch_id="batch:demo",
        event_ids=(),
        message="safe message",
        created_at=NOW,
        targets={"umo:one": DeliveryTargetState(target="umo:one")},
    )
    return WatcherState(
        sources={SourceKind.MARKET.value: source},
        plugins={
            "github:owner/demo": PluginRecord(
                canonical_id="github:owner/demo",
                name="demo",
            )
        },
        github=WatcherState().github,
        outbox={batch.batch_id: batch},
        id_aliases={"source:demo": "github:owner/demo"},
        image_delivery_windows={"umo:one": ImageDeliveryWindow()},
    )


def status_text(*, diagnostics: StateDiagnostics, source_states: str) -> str:
    runtime = parse_runtime_config({"github_token": "github_pat_CANARY_TOKEN"})
    return format_status(
        runtime=runtime,
        enabled_sources={SourceKind.MARKET},
        scheduler_state="disabled",
        scheduler_last_attempt=NOW,
        scheduler_last_success=NOW,
        scheduler_error="无",
        drain_scheduler_state="stopped",
        drain_last_attempt=None,
        drain_last_success=None,
        drain_error="无",
        service_busy=False,
        health="正常",
        schema_version="3",
        plugin_count=1,
        source_states=source_states,
        github_remaining="未知",
        github_reset="未知",
        github_cache_count=0,
        configured_target_count=0,
        subscription_count=0,
        effective_target_count=0,
        pending=1,
        exhausted=0,
        throttled=0,
        next_drain="无",
        last_text="暂无运行报告",
        plugin_version="1.1.1",
        state_diagnostics=diagnostics,
    )


class StatusObservabilityTests(unittest.TestCase):
    def test_version_and_source_summary_are_safe(self) -> None:
        source_states = format_source_states(
            {
                SourceKind.MARKET.value: SourceState(
                    complete=True,
                    last_success_at=NOW,
                    observations={"a": observation()},
                    snapshots={"endpoint": EndpointSnapshot(endpoint="endpoint")},
                ),
                SourceKind.GITHUB_DISCOVERY.value: SourceState(
                    complete=False,
                    error_code="http_error",
                ),
            }
        )
        self.assertIn(
            "插件版本：1.1.1",
            status_text(
                diagnostics=StateDiagnostics(state_file_bytes=0),
                source_states=source_states,
            ),
        )
        self.assertIn("market:complete=yes", source_states)
        self.assertIn("failed=no", source_states)
        self.assertIn("error_code=none", source_states)
        self.assertIn("last_success_at=" + NOW, source_states)
        self.assertIn("observations=1", source_states)
        self.assertIn("snapshots=1", source_states)
        self.assertIn("github_discovery:complete=no", source_states)
        self.assertIn("failed=yes", source_states)
        self.assertIn("error_code=http_error", source_states)
        self.assertIn("last_success_at=none", source_states)

    def test_compact_field_bytes_and_counts_are_exact(self) -> None:
        state = observable_state()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_bytes(b"disk-state")
            diagnostics = collect_state_diagnostics(state, path)

        serialized = state.to_dict()
        self.assertEqual(diagnostics.state_file_bytes, len(b"disk-state"))
        for key, value in serialized.items():
            expected = len(
                json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            self.assertEqual(diagnostics.field_bytes[key], expected)
        self.assertEqual(
            diagnostics.counts,
            {
                "source_observations": 1,
                "snapshots": 1,
                "snapshot_observations": 1,
                "plugins": 1,
                "github_repos": 0,
                "outbox_batches": 1,
                "pending_targets": 1,
                "aliases": 1,
                "image_windows": 1,
            },
        )

    def test_missing_file_and_serialization_fallback_are_non_throwing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.json"
            diagnostics = collect_state_diagnostics(WatcherState(), missing)
        self.assertTrue(diagnostics.available)
        self.assertEqual(diagnostics.state_file_bytes, 0)

        class BrokenState:
            def to_dict(self):
                raise RuntimeError("raw endpoint and secret message")

        diagnostics = collect_state_diagnostics(BrokenState())  # type: ignore[arg-type]
        self.assertFalse(diagnostics.available)
        text = status_text(
            diagnostics=diagnostics,
            source_states="暂无",
        )
        self.assertIn("状态字段 JSON bytes：unavailable", text)
        self.assertNotIn("raw endpoint", text)
        self.assertNotIn("secret message", text)
        self.assertFalse(unavailable_state_diagnostics().available)

    def test_canary_content_never_enters_status_or_source_errors(self) -> None:
        item = observation()
        item.fetched_from = "https://secret.example/endpoint?token=CANARY"
        item.raw_excerpt = {
            "body": "raw excerpt CANARY_RAW_EXCERPT",
            "message": "secret response message",
        }
        state = observable_state()
        state.sources[SourceKind.MARKET.value].observations[item.source_record_id] = (
            item
        )
        state.sources[SourceKind.MARKET.value].error_code = "safe_error"
        state.outbox["batch:demo"].message = "secret outbox message"
        state.outbox["batch:demo"].targets = {
            "umo:CANARY_TARGET": DeliveryTargetState(target="umo:CANARY_TARGET")
        }
        state.plugins["github:owner/demo"].description = "github_pat_CANARY_TOKEN"

        source_states = format_source_states(state.sources)
        source_errors = format_source_errors(state.sources)
        text = status_text(
            diagnostics=collect_state_diagnostics(state),
            source_states=source_states,
        )
        rendered = "\n".join((source_states, source_errors, text))
        for canary in (
            "CANARY",
            "secret response message",
            "secret outbox message",
            "github_pat_CANARY_TOKEN",
        ):
            self.assertNotIn(canary, rendered)
        self.assertIn("market:safe_error", source_errors)


if __name__ == "__main__":
    unittest.main()
