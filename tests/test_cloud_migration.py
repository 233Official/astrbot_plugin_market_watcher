from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from market_watcher.http import HttpResponse
from market_watcher.models import (
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    SourceKind,
    SourceState,
)
from market_watcher.outbox import compact_terminal_batches, next_outbox_due_at
from market_watcher.service import MarketWatcherService
from market_watcher.sources.github_search import GitHubSearchFetcher
from market_watcher.sources.market import MARKET_API_URL, MarketFetcher
from market_watcher.state import JsonStateStore
from tests.test_state import sample_state

NOW = "2026-11-02T00:00:00Z"
TARGET = "local:test"


def catalog(version="1.0.0"):
    return {
        "$meta": {"updated_at": NOW},
        "demo": {
            "name": "astrbot_plugin_demo",
            "repo": "https://github.com/example/astrbot_plugin_demo",
            "desc": "Cloud description",
            "version": version,
        },
        "second": {
            "name": "second",
            "repo": "https://github.com/example/second",
            "version": "1.0.0",
        },
    }


class CatalogHttp:
    def __init__(self, payload=None):
        self.payload = catalog() if payload is None else payload
        self.calls = []

    async def get(self, url, *, headers=None):
        self.calls.append(url)
        if isinstance(self.payload, Exception):
            raise self.payload
        return HttpResponse(200, json.dumps(self.payload).encode(), url=url)


class LocalNotifier:
    def __init__(self):
        self.messages = []

    async def prepare(self, batch):
        return None

    async def send(self, target, message):
        self.messages.append(message)
        return True, None

    def clear_prepared(self):
        pass


def legacy_fixture(version=2):
    state = sample_state()
    state.subscriptions = [TARGET]
    retired = deepcopy(state.sources["market"])
    for item in retired.observations.values():
        item.source_kind = SourceKind.COLLECTION_ISSUE
    for snapshot in retired.snapshots.values():
        for item in snapshot.observations.values():
            item.source_kind = SourceKind.COLLECTION_ISSUE
    state.sources[SourceKind.COLLECTION_ISSUE.value] = retired
    meta = deepcopy(state.sources["market"].observations["demo"])
    meta.source_record_id = "$meta"
    meta.canonical_id = "source:market:old-meta"
    meta.name = "$meta"
    state.sources["market"].observations["$meta"] = meta
    for snapshot in state.sources["market"].snapshots.values():
        snapshot.observations["$meta"] = meta
    meta_record = deepcopy(next(iter(state.plugins.values())))
    meta_record.canonical_id = meta.canonical_id
    meta_record.name = "$meta"
    meta_record.evidence[0].source_record_id = "$meta"
    state.plugins[meta.canonical_id] = meta_record
    state.outbox = {
        "old": DeliveryBatch(
            batch_id="old",
            event_ids=("unverifiable-old-event",),
            message="Legacy unverified page",
            created_at=NOW,
            targets={
                TARGET: DeliveryTargetState(
                    target=TARGET,
                    status=DeliveryStatus.FAILED,
                    attempts=1,
                    next_retry_at=NOW,
                ),
                "held:pending": DeliveryTargetState(
                    target="held:pending",
                    next_retry_at="2099-01-01T00:00:00Z",
                ),
                "already:sent": DeliveryTargetState(
                    target="already:sent", status=DeliveryStatus.SENT, attempts=1
                ),
            },
            card_payload={"batch_index": 3, "batch_total": 4},
        ),
        "exhausted": DeliveryBatch(
            batch_id="exhausted",
            event_ids=("exhausted-event",),
            message="Exhausted legacy page",
            created_at=NOW,
            targets={
                TARGET: DeliveryTargetState(
                    target=TARGET, status=DeliveryStatus.EXHAUSTED, attempts=5
                )
            },
        ),
    }
    value = state.to_dict()
    value["schema_version"] = version
    del value["legacy_outbox_ids"]
    del value["market_baseline_pending"]
    if version == 1:
        del value["image_delivery_windows"]
    return value


class CloudMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JsonStateStore(Path(self.directory.name) / "state.json")
        self.http = CatalogHttp()
        self.notifier = LocalNotifier()

    def service(self):
        return MarketWatcherService(
            store=self.store,
            fetchers={SourceKind.MARKET: MarketFetcher(self.http, clock=lambda: NOW)},
            notifier=self.notifier,
            clock=lambda: NOW,
        )

    def seed_legacy(self, version=2):
        self.store.path.write_text(
            json.dumps(legacy_fixture(version)), encoding="utf-8"
        )

    async def check(self, service):
        return await service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=[],
            max_items_per_push=10,
            image_send_interval_seconds=0,
        )

    async def test_persisted_upgrade_holds_payloads_and_rebases_once(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.seed_legacy(version)
                self.notifier.messages.clear()
                service = self.service()
                status = await service.prepare_migration()
                self.assertEqual(
                    status, {"quarantined_batches": 2, "baseline_pending": True}
                )
                before = self.store.load()
                self.assertNotIn("collection_issue", before.sources)
                self.assertEqual(before.subscriptions, [TARGET])
                self.assertNotIn("source:market:old-meta", before.plugins)
                self.assertNotIn("$meta", before.sources["market"].observations)
                self.assertEqual(
                    before.outbox["old"].card_payload,
                    {"batch_index": 3, "batch_total": 4},
                )
                self.assertEqual(compact_terminal_batches(before).removed_batches, 0)
                self.assertIsNone(next_outbox_due_at(before, now=NOW))
                await service.drain_pending()
                self.assertEqual(self.notifier.messages, [])
                report = await self.check(service)
                self.assertEqual((report.discovered, report.updated), (0, 0))
                self.assertEqual(self.notifier.messages, [])
                after = self.store.load()
                self.assertFalse(after.market_baseline_pending)
                self.assertEqual(after.outbox, before.outbox)
                self.assertEqual(after.subscriptions, before.subscriptions)
                restarted = self.service()
                self.assertEqual(
                    await restarted.prepare_migration(),
                    {"quarantined_batches": 2, "baseline_pending": False},
                )
                report = await self.check(restarted)
                self.assertEqual((report.discovered, report.updated), (0, 0))
                self.http.payload = catalog("2.0.0")
                report = await self.check(restarted)
                self.assertEqual((report.discovered, report.updated), (0, 1))
                self.assertEqual(len(self.notifier.messages), 1)
                self.assertEqual(
                    self.store.load().legacy_outbox_ids, ["exhausted", "old"]
                )
                self.http.payload = catalog()

    async def test_failed_rebase_is_not_consumed_and_restart_stays_silent(self):
        self.seed_legacy()
        original_plugins = self.store.load().plugins
        for bad_payload in (TimeoutError(), [], {"$meta": {"updated_at": NOW}}):
            self.http.payload = bad_payload
            service = self.service()
            await service.prepare_migration()
            report = await self.check(service)
            self.assertEqual(report.status, "failed")
            state = self.store.load()
            self.assertTrue(state.market_baseline_pending)
            self.assertEqual(state.plugins, original_plugins)
            self.assertEqual(self.notifier.messages, [])
        self.http.payload = catalog()
        report = await self.check(self.service())
        self.assertEqual((report.discovered, report.updated), (0, 0))
        self.assertFalse(self.store.load().market_baseline_pending)
        self.assertTrue(all(url == MARKET_API_URL for url in self.http.calls))

    async def test_admin_discard_only_removes_quarantined_unsent_work(self):
        self.seed_legacy()
        service = self.service()
        await service.prepare_migration()
        state = self.store.load()
        state.outbox["new"] = DeliveryBatch(
            "new",
            ("real-event",),
            "New verified page",
            NOW,
            {TARGET: DeliveryTargetState(target=TARGET)},
        )
        self.store.save(state)
        result = await service.resolve_legacy_outbox("discard")
        self.assertEqual(result["affected_batches"], 2)
        state = self.store.load()
        self.assertEqual(state.legacy_outbox_ids, [])
        self.assertEqual(set(state.outbox["old"].targets), {"already:sent"})
        self.assertNotIn("exhausted", state.outbox)
        await self.service().drain_pending()
        self.assertEqual(self.notifier.messages, ["New verified page"])
        self.assertEqual(
            (await self.service().migration_status())["quarantined_batches"], 0
        )

    async def test_admin_release_preserves_sent_pages_retries_and_exhaustion(self):
        self.seed_legacy()
        service = self.service()
        await service.prepare_migration()
        before = self.store.load()
        result = await service.resolve_legacy_outbox("release")
        self.assertEqual(result["affected_batches"], 2)
        state = self.store.load()
        self.assertEqual(state.outbox, before.outbox)
        self.assertEqual(state.legacy_outbox_ids, [])
        restarted = self.service()
        await restarted.prepare_migration()
        await restarted.drain_pending(image_send_interval_seconds=0)
        self.assertEqual(self.notifier.messages, ["Legacy unverified page"])
        await self.service().drain_pending(image_send_interval_seconds=0)
        self.assertEqual(self.notifier.messages, ["Legacy unverified page"])

    async def test_new_install_has_no_legacy_migration_or_quarantine(self):
        service = self.service()
        self.assertEqual(
            await service.prepare_migration(),
            {"quarantined_batches": 0, "baseline_pending": False},
        )
        report = await self.check(service)
        self.assertEqual((report.discovered, report.updated), (0, 0))
        self.http.payload["third"] = {
            "name": "third",
            "repo": "https://github.com/example/third",
        }
        report = await self.check(self.service())
        self.assertEqual((report.discovered, report.updated), (1, 0))
        self.assertEqual(self.store.load().schema_version, 3)
        self.assertEqual(self.store.load().legacy_outbox_ids, [])

    async def test_cloud_rebase_does_not_silence_independent_new_discovery(self):
        self.seed_legacy()
        service = self.service()
        await service.prepare_migration()
        state = self.store.load()
        state.sources[SourceKind.GITHUB_DISCOVERY.value] = SourceState(
            baseline_established=True,
            complete=True,
        )
        self.store.save(state)
        service.fetchers[SourceKind.GITHUB_DISCOVERY] = GitHubSearchFetcher(
            CatalogHttp(
                {
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [
                        {
                            "id": 777,
                            "name": "astrbot_plugin_independent",
                            "owner": {"login": "example"},
                            "html_url": "https://github.com/example/astrbot_plugin_independent",
                        }
                    ],
                }
            ),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET, SourceKind.GITHUB_DISCOVERY},
            push_targets=[],
            max_items_per_push=10,
            include_star_count=False,
            image_send_interval_seconds=0,
        )
        self.assertEqual((report.discovered, report.updated), (1, 0))
        self.assertEqual(len(self.notifier.messages), 1)
        report = await service.check(
            enabled_sources={SourceKind.MARKET, SourceKind.GITHUB_DISCOVERY},
            push_targets=[],
            max_items_per_push=10,
            include_star_count=False,
            image_send_interval_seconds=0,
        )
        self.assertEqual((report.discovered, report.updated), (0, 0))
