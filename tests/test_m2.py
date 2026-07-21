from __future__ import annotations

import asyncio
import hashlib
import unittest
from copy import deepcopy

from market_watcher.astrbot_adapter import AstrBotNotifier, FakeNotifier
from market_watcher.detect import detect_changes
from market_watcher.merge import merge_sources
from market_watcher.models import (
    ChangeKind,
    DeliveryStatus,
    FetchResult,
    PluginRecord,
    RunReport,
    SourceEvidence,
    SourceKind,
    SourceObservation,
    SourceState,
    WatcherState,
)
from market_watcher.outbox import (
    count_exhausted,
    count_pending,
    create_batches,
    deliver_pending,
    validate_targets,
)
from market_watcher.service import MarketWatcherService
from market_watcher.state import StateCorruptError, StateWriteError
from market_watcher.summary import render_summary

NOW = "2026-07-20T12:00:00Z"
LATER = "2026-07-20T13:00:00Z"


def observation(
    kind: SourceKind,
    record_id: str,
    canonical_id: str = "github:example/astrbot_plugin_demo",
    **values,
) -> SourceObservation:
    repo_name = canonical_id.rsplit("/", 1)[-1]
    defaults = {
        "repo_url": f"https://github.com/example/{repo_name}",
        "repo_owner": "example",
        "repo_name": repo_name,
        "name": repo_name,
        "display_name": "Demo",
        "description": "用途说明",
        "version": "1.0.0",
        "stars": 10,
        "observation_hash": f"hash:{kind.value}:{record_id}",
    }
    defaults.update(values)
    return SourceObservation(
        source_kind=kind,
        source_record_id=record_id,
        source_url=f"https://example.invalid/{kind.value}/{record_id}",
        observed_at=NOW,
        fetched_from=f"https://api.example.invalid/{kind.value}",
        canonical_id=canonical_id,
        **defaults,
    )


def source_state(*items: SourceObservation, baseline: bool = True) -> SourceState:
    return SourceState(
        baseline_established=baseline,
        complete=True,
        observations={item.source_record_id: item for item in items},
    )


def fetch_result(
    kind: SourceKind,
    *items: SourceObservation,
    pages_fetched: int = 1,
) -> FetchResult:
    return FetchResult(
        source_kind=kind,
        success=True,
        complete=True,
        observations=list(items),
        endpoint=f"https://api.example.invalid/{kind.value}",
        pages_fetched=pages_fetched,
    )


class FakeStore:
    def __init__(self, state: WatcherState | None = None, *, load_error=None) -> None:
        self.state = deepcopy(state or WatcherState())
        self.load_error = load_error
        self.save_error: Exception | None = None
        self.saves = 0

    def load(self) -> WatcherState:
        if self.load_error:
            raise self.load_error
        return deepcopy(self.state)

    def save(self, state: WatcherState) -> None:
        if self.save_error:
            raise self.save_error
        self.saves += 1
        self.state = deepcopy(state)


class FakeFetcher:
    def __init__(self, *results: FetchResult) -> None:
        self.results = list(results)

    async def fetch(self, previous=None) -> FetchResult:
        del previous
        return self.results.pop(0)


class MergeDetectTests(unittest.TestCase):
    def test_priority_evidence_and_sparse_preserve(self) -> None:
        market = observation(
            SourceKind.MARKET,
            "market",
            description="市场描述",
            author="市场作者",
        )
        collection = observation(
            SourceKind.COLLECTION_ISSUE,
            "issue",
            description="Issue 描述",
            version="2.0.0",
        )
        merged = merge_sources(
            {
                SourceKind.MARKET.value: source_state(market),
                SourceKind.COLLECTION_ISSUE.value: source_state(collection),
            }
        )
        record = next(iter(merged.values()))
        self.assertEqual(record.description, "市场描述")
        self.assertEqual(record.version, "1.0.0")
        self.assertEqual(len(record.evidence), 2)

        sparse = observation(
            SourceKind.MARKET,
            "market",
            description=None,
            author=None,
            sparse=True,
        )
        preserved = merge_sources(
            {SourceKind.MARKET.value: source_state(sparse)},
            merged,
        )
        self.assertEqual(next(iter(preserved.values())).description, "市场描述")
        self.assertEqual(next(iter(preserved.values())).author, "市场作者")

    def test_detect_substantive_and_non_substantive_changes(self) -> None:
        old = PluginRecord(
            canonical_id="github:example/demo",
            name="old-name",
            display_name="Demo",
            description="old",
            version="1.0.0",
            stars=1,
            tags=("old",),
            observed_at=NOW,
        )
        non_substantive = deepcopy(old)
        non_substantive.name = "new-name"
        non_substantive.tags = ("new",)
        non_substantive.stars = 999
        non_substantive.observed_at = LATER
        self.assertEqual(
            detect_changes(
                {old.canonical_id: old},
                {old.canonical_id: non_substantive},
                detected_at=NOW,
            ),
            [],
        )
        updated = deepcopy(non_substantive)
        updated.version = "2.0.0"
        updated.description = "new"
        events = detect_changes(
            {old.canonical_id: old},
            {old.canonical_id: updated},
            detected_at=NOW,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, ChangeKind.UPDATED)
        self.assertEqual(events[0].changed_fields, ("description", "version"))
        self.assertEqual(
            events[0].event_id,
            detect_changes(
                {old.canonical_id: old}, {old.canonical_id: updated}, detected_at=LATER
            )[0].event_id,
        )

    def test_same_plugin_from_two_sources_folds_to_one_event(self) -> None:
        market = observation(SourceKind.MARKET, "m")
        issue = observation(SourceKind.COLLECTION_ISSUE, "i")
        merged = merge_sources(
            {
                SourceKind.MARKET.value: source_state(market),
                SourceKind.COLLECTION_ISSUE.value: source_state(issue),
            }
        )
        events = detect_changes({}, merged, detected_at=NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].current.evidence), 2)

    def test_aliases_only_allow_fallback_to_github_and_converge(self) -> None:
        first = observation(
            SourceKind.MARKET,
            "m",
            canonical_id="source:market:first",
            description="market",
        )
        second = observation(
            SourceKind.COLLECTION_ISSUE,
            "c",
            canonical_id="source:collection_issue:second",
            description="collection",
        )
        aliases = {
            first.canonical_id: "github:example/demo",
            second.canonical_id: "github:example/demo",
            "github:example/demo": "github:attacker/rewritten",
        }
        merged = merge_sources(
            {
                SourceKind.MARKET.value: source_state(first),
                SourceKind.COLLECTION_ISSUE.value: source_state(second),
            },
            aliases=aliases,
        )
        self.assertEqual(set(merged), {"github:example/demo"})
        self.assertEqual(len(merged["github:example/demo"].evidence), 2)

    def test_event_order_and_id_algorithm_are_frozen(self) -> None:
        old = PluginRecord(canonical_id="github:z/updated", name="updated", version="1")
        updated = deepcopy(old)
        updated.version = "2"
        discovered = PluginRecord(canonical_id="github:a/new", name="new")
        events = detect_changes(
            {old.canonical_id: old},
            {old.canonical_id: updated, discovered.canonical_id: discovered},
            detected_at=NOW,
        )
        self.assertEqual(
            [(item.kind.value, item.canonical_id) for item in events],
            [("discovered", "github:a/new"), ("updated", "github:z/updated")],
        )
        expected = hashlib.sha256(
            (
                "discovered\0github:a/new\0none\0" + discovered.compute_content_hash()
            ).encode("utf-8")
        ).hexdigest()
        self.assertEqual(events[0].event_id, f"event:discovered:{expected}")


class SummaryOutboxTests(unittest.IsolatedAsyncioTestCase):
    def make_events(self, count: int):
        records = {}
        for index in range(count):
            canonical = f"github:example/plugin_{index}"
            records[canonical] = PluginRecord(
                canonical_id=canonical,
                name=f"plugin_{index}",
                display_name=f"插件 {index}",
                description="公开用途说明",
                version="1.0.0",
                stars=5,
                evidence=(
                    SourceEvidence(
                        SourceKind.MARKET,
                        str(index),
                        f"https://example.invalid/{index}",
                        NOW,
                    ),
                ),
            )
        return detect_changes({}, records, detected_at=NOW)

    def test_summary_facts_batching_and_stable_ids(self) -> None:
        events = self.make_events(3)
        first = create_batches(events, ["umo:b", "umo:a"], max_items=2, created_at=NOW)
        second = create_batches(
            events, ["umo:a", "umo:b"], max_items=2, created_at=LATER
        )
        self.assertEqual(len(first), 2)
        self.assertEqual(
            create_batches(events, [], max_items=2, created_at=NOW),
            [],
        )
        self.assertEqual(
            [item.batch_id for item in first], [item.batch_id for item in second]
        )
        message = render_summary(events[:1], 1, 1)
        self.assertIn("新增", message)
        self.assertIn("用途", message)
        self.assertIn("Star", message)
        self.assertIn("https://example.invalid/0", message)
        for forbidden in ("审核通过", "安全", "质量"):
            self.assertNotIn(forbidden, message)

    async def test_per_target_delivery_restart_and_max_attempts(self) -> None:
        batch = create_batches(
            self.make_events(1), ["ok", "bad"], max_items=10, created_at=NOW
        )[0]
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        notifier = FakeNotifier({"ok": [True], "bad": [False]})
        sent, pending = await deliver_pending(
            state, store, notifier, now=NOW, clock=lambda: NOW
        )
        self.assertEqual((sent, pending), (1, 1))
        self.assertEqual(
            state.outbox[batch.batch_id].targets["ok"].status, DeliveryStatus.SENT
        )

        restarted = deepcopy(store.state)
        retry = FakeNotifier({"bad": [True]})
        sent, pending = await deliver_pending(
            restarted, store, retry, now=LATER, clock=lambda: LATER
        )
        self.assertEqual((sent, pending), (1, 0))
        self.assertEqual([target for target, _ in retry.calls], ["bad"])

        exhausted = create_batches(
            self.make_events(1), ["never"], max_items=10, created_at=NOW
        )[0]
        exhausted.targets["never"].attempts = exhausted.max_attempts
        exhausted.targets["never"].status = DeliveryStatus.FAILED
        exhausted_state = WatcherState(outbox={exhausted.batch_id: exhausted})
        never = FakeNotifier()
        await deliver_pending(
            exhausted_state, store, never, now=LATER, clock=lambda: LATER
        )
        self.assertEqual(never.calls, [])
        self.assertEqual(exhausted.targets["never"].status, DeliveryStatus.EXHAUSTED)

    def test_outbox_state_round_trip(self) -> None:
        batch = create_batches(
            self.make_events(1), ["umo"], max_items=10, created_at=NOW
        )[0]
        state = WatcherState(outbox={batch.batch_id: batch})
        self.assertEqual(WatcherState.from_dict(state.to_dict()), state)

        legacy = state.to_dict()
        target = next(iter(next(iter(legacy["outbox"].values()))["targets"].values()))
        target.pop("last_attempt_at")
        target["status"] = "failed"
        target["attempts"] = batch.max_attempts
        restored = WatcherState.from_dict(legacy)
        restored_target = next(
            iter(next(iter(restored.outbox.values())).targets.values())
        )
        self.assertEqual(restored_target.status, DeliveryStatus.EXHAUSTED)
        self.assertIsNone(restored_target.next_retry_at)
        self.assertEqual(count_pending(restored), 0)
        self.assertEqual(count_exhausted(restored), 1)

    def test_run_report_round_trip_and_exhausted_status_text(self) -> None:
        report = RunReport(
            status="success",
            started_at=NOW,
            targets_pending=2,
            targets_exhausted=3,
            invalid_targets=4,
            target_error_code="invalid_push_targets",
        )
        restored = RunReport(**report.to_dict())
        self.assertEqual(restored, report)
        text = restored.to_chinese()
        self.assertIn("已发送/待处理/永久失败目标：0/2/3", text)
        self.assertIn("跳过非法目标：4", text)
        self.assertIn("invalid_push_targets", text)

    def test_target_validation_skips_non_strings_empty_and_oversized(self) -> None:
        valid, invalid = validate_targets(
            [" umo:ok ", "", "   ", 123, "x" * 513, "umo:ok"]
        )
        self.assertEqual(valid, ["umo:ok"])
        self.assertEqual(invalid, 4)

    async def test_sender_exception_is_stable_failure(self) -> None:
        batch = create_batches(
            self.make_events(1), ["boom"], max_items=10, created_at=NOW
        )[0]
        state = WatcherState(outbox={batch.batch_id: batch})
        await deliver_pending(
            state,
            FakeStore(state),
            FakeNotifier({"boom": [RuntimeError("secret")]}),
            now=NOW,
            clock=lambda: NOW,
        )
        target = state.outbox[batch.batch_id].targets["boom"]
        self.assertEqual(target.last_error_code, "delivery_exception")
        self.assertEqual(target.last_attempt_at, NOW)

    def test_summary_neutralizes_external_text_and_has_hard_limit(self) -> None:
        record = PluginRecord(
            canonical_id="github:example/unsafe",
            name="@all\n- [CQ:at,qq=all] `x`",
            description=("**@everyone**\r\n> injected " * 300),
            version="[link](bad)\n2",
            evidence=(
                SourceEvidence(
                    SourceKind.MARKET,
                    "unsafe",
                    "https://example.invalid/[CQ:at,qq=all]?x=@all",
                    NOW,
                ),
            ),
        )
        message = render_summary(
            detect_changes({}, {record.canonical_id: record}, detected_at=NOW), 1, 1
        )
        self.assertLessEqual(len(message), 3500)
        for unsafe in ("@all", "@everyone", "[CQ:", "`", "**", "\r"):
            self.assertNotIn(unsafe, message)
        self.assertNotIn("\n- ［CQ", message)

    async def test_batch_target_snapshot_and_sent_target_are_immutable(self) -> None:
        events = self.make_events(1)
        old = create_batches(events, ["old"], max_items=10, created_at=NOW)[0]
        state = WatcherState(outbox={old.batch_id: old})
        store = FakeStore(state)
        notifier = FakeNotifier({"old": [True]})
        await deliver_pending(state, store, notifier, now=NOW, clock=lambda: NOW)
        new = create_batches(events, ["new"], max_items=10, created_at=LATER)[0]
        state.outbox.setdefault(new.batch_id, new)
        await deliver_pending(state, store, notifier, now=LATER, clock=lambda: LATER)
        self.assertEqual(set(old.targets), {"old"})
        self.assertEqual(set(new.targets), {"new"})
        self.assertEqual([target for target, _ in notifier.calls].count("old"), 1)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_pending_is_delivered_before_blocking_fetch(self) -> None:
        events = SummaryOutboxTests().make_events(1)
        batch = create_batches(events, ["umo"], max_items=10, created_at=NOW)[0]
        order = []
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        class OrderedStore(FakeStore):
            def save(self, state):
                order.append("save")
                super().save(state)

        class OrderedNotifier(FakeNotifier):
            async def send(self, target, message):
                order.append("send")
                return await super().send(target, message)

        class BlockingFetcher:
            async def fetch(self, previous=None):
                del previous
                order.append("fetch")
                fetch_started.set()
                await release_fetch.wait()
                raise RuntimeError("fetch failed after old delivery")

        store = OrderedStore(WatcherState(outbox={batch.batch_id: batch}))
        notifier = OrderedNotifier({"umo": [True]})
        service = MarketWatcherService(
            store=store,
            fetchers={SourceKind.MARKET: BlockingFetcher()},
            notifier=notifier,
            clock=lambda: LATER,
        )
        task = asyncio.create_task(
            service.check(
                enabled_sources={SourceKind.MARKET},
                push_targets=[],
                max_items_per_push=10,
            )
        )
        await fetch_started.wait()
        self.assertEqual(order[:4], ["save", "send", "save", "fetch"])
        self.assertEqual(len(notifier.calls), 1)
        release_fetch.set()
        report = await task
        self.assertEqual(report.targets_sent, 1)
        self.assertEqual(report.status, "failed")

    async def test_new_batch_is_saved_before_delivery(self) -> None:
        old = observation(SourceKind.MARKET, "old")
        new = observation(
            SourceKind.MARKET,
            "new",
            canonical_id="github:example/new",
            name="new",
            repo_name="new",
        )
        old_source = source_state(old)
        order = []

        class OrderedStore(FakeStore):
            def save(self, state):
                order.append("save")
                super().save(state)

        class OrderedFetcher(FakeFetcher):
            async def fetch(self, previous=None):
                order.append("fetch")
                return await super().fetch(previous)

        class OrderedNotifier(FakeNotifier):
            async def send(self, target, message):
                order.append("send")
                return await super().send(target, message)

        store = OrderedStore(
            WatcherState(
                sources={SourceKind.MARKET.value: old_source},
                plugins=merge_sources({SourceKind.MARKET.value: old_source}),
            )
        )
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.MARKET: OrderedFetcher(
                    fetch_result(SourceKind.MARKET, old, new)
                )
            },
            notifier=OrderedNotifier(),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["umo"],
            max_items_per_push=10,
        )
        self.assertEqual(report.batches_created, 1)
        self.assertLess(order.index("fetch"), order.index("send"))
        self.assertEqual(order[order.index("send") - 1], "save")

    async def test_service_report_keeps_exhausted_visible(self) -> None:
        batch = create_batches(
            SummaryOutboxTests().make_events(1),
            ["never"],
            max_items=10,
            created_at=NOW,
        )[0]
        target = batch.targets["never"]
        target.status = DeliveryStatus.FAILED
        target.attempts = batch.max_attempts
        store = FakeStore(WatcherState(outbox={batch.batch_id: batch}))
        service = MarketWatcherService(
            store=store, fetchers={}, notifier=FakeNotifier(), clock=lambda: LATER
        )
        report = await service.check(
            enabled_sources=set(), push_targets=[], max_items_per_push=10
        )
        self.assertEqual(report.targets_pending, 0)
        self.assertEqual(report.targets_exhausted, 1)
        self.assertEqual(store.state.last_run["targets_exhausted"], 1)

    async def test_service_persists_endpoint_page_count(self) -> None:
        item = observation(SourceKind.COLLECTION_ISSUE, "one")
        store = FakeStore()
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.COLLECTION_ISSUE: FakeFetcher(
                    fetch_result(
                        SourceKind.COLLECTION_ISSUE,
                        item,
                        pages_fetched=2,
                    )
                )
            },
            notifier=FakeNotifier(),
            clock=lambda: NOW,
        )
        await service.check(
            enabled_sources={SourceKind.COLLECTION_ISSUE},
            push_targets=[],
            max_items_per_push=10,
        )
        snapshot = next(
            iter(
                store.state.sources[
                    SourceKind.COLLECTION_ISSUE.value
                ].snapshots.values()
            )
        )
        self.assertEqual(snapshot.pages_fetched, 2)

    async def test_baseline_then_same_then_discovered(self) -> None:
        first = observation(SourceKind.MARKET, "one")
        second = observation(
            SourceKind.MARKET,
            "two",
            canonical_id="github:example/astrbot_plugin_two",
            name="astrbot_plugin_two",
            repo_name="astrbot_plugin_two",
        )
        store = FakeStore()
        notifier = FakeNotifier()
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.MARKET: FakeFetcher(
                    fetch_result(SourceKind.MARKET, first),
                    fetch_result(SourceKind.MARKET, first),
                    fetch_result(SourceKind.MARKET, first, second),
                )
            },
            notifier=notifier,
            clock=lambda: NOW,
        )
        reports = []
        for _ in range(3):
            reports.append(
                await service.check(
                    enabled_sources={SourceKind.MARKET},
                    push_targets=["umo"],
                    max_items_per_push=10,
                )
            )
        self.assertEqual(reports[0].discovered, 0)
        self.assertEqual(reports[1].discovered, 0)
        self.assertEqual(reports[2].discovered, 1)
        self.assertEqual(len(notifier.calls), 1)

    async def test_new_source_baseline_is_silent_and_failed_source_preserved(
        self,
    ) -> None:
        market = observation(SourceKind.MARKET, "m")
        collection = observation(
            SourceKind.COLLECTION_ISSUE,
            "c",
            canonical_id="github:example/collection_only",
            name="collection_only",
            repo_name="collection_only",
        )
        state = WatcherState(
            sources={SourceKind.MARKET.value: source_state(market)},
            plugins=merge_sources({SourceKind.MARKET.value: source_state(market)}),
        )
        failure = FetchResult(SourceKind.MARKET, False, False, error_code="down")
        store = FakeStore(state)
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.MARKET: FakeFetcher(failure),
                SourceKind.COLLECTION_ISSUE: FakeFetcher(
                    fetch_result(SourceKind.COLLECTION_ISSUE, collection)
                ),
            },
            notifier=FakeNotifier(),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET, SourceKind.COLLECTION_ISSUE},
            push_targets=["umo"],
            max_items_per_push=10,
        )
        self.assertEqual(report.discovered, 0)
        self.assertIn("m", store.state.sources[SourceKind.MARKET.value].observations)
        self.assertIn("github:example/collection_only", store.state.plugins)

    async def test_new_source_baseline_can_update_existing_plugin(self) -> None:
        market = observation(SourceKind.MARKET, "m", description="old")
        collection = observation(SourceKind.COLLECTION_ISSUE, "c", description="new")
        old_source = source_state(market)
        state = WatcherState(
            sources={SourceKind.MARKET.value: old_source},
            plugins=merge_sources({SourceKind.MARKET.value: old_source}),
        )
        store = FakeStore(state)
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.COLLECTION_ISSUE: FakeFetcher(
                    fetch_result(SourceKind.COLLECTION_ISSUE, collection)
                )
            },
            notifier=FakeNotifier(),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.COLLECTION_ISSUE},
            push_targets=["umo"],
            max_items_per_push=10,
        )
        self.assertEqual((report.discovered, report.updated), (0, 0))

        higher_priority = observation(SourceKind.MARKET, "m", description="changed")
        collection_state = source_state(collection)
        state = WatcherState(
            sources={SourceKind.COLLECTION_ISSUE.value: collection_state},
            plugins=merge_sources(
                {SourceKind.COLLECTION_ISSUE.value: collection_state}
            ),
        )
        store = FakeStore(state)
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.MARKET: FakeFetcher(
                    fetch_result(SourceKind.MARKET, higher_priority)
                )
            },
            notifier=FakeNotifier(),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET}, push_targets=[], max_items_per_push=10
        )
        self.assertEqual(report.updated, 1)

    async def test_disabled_sources_remain_stable_and_old_outbox_is_delivered(
        self,
    ) -> None:
        market = observation(SourceKind.MARKET, "m")
        market_state = source_state(market)
        batch = create_batches(
            detect_changes(
                {},
                merge_sources({SourceKind.MARKET.value: market_state}),
                detected_at=NOW,
            ),
            ["umo"],
            max_items=10,
            created_at=NOW,
        )[0]
        state = WatcherState(
            sources={SourceKind.MARKET.value: market_state},
            plugins=merge_sources({SourceKind.MARKET.value: market_state}),
            outbox={batch.batch_id: batch},
        )
        store = FakeStore(state)
        notifier = FakeNotifier({"umo": [True]})
        service = MarketWatcherService(
            store=store, fetchers={}, notifier=notifier, clock=lambda: LATER
        )
        report = await service.check(
            enabled_sources=set(), push_targets=[], max_items_per_push=10
        )
        self.assertIn(market.canonical_id, store.state.plugins)
        self.assertEqual(report.targets_sent, 1)

    async def test_old_outbox_is_delivered_when_all_enabled_sources_fail(self) -> None:
        events = SummaryOutboxTests().make_events(1)
        batch = create_batches(events, ["umo"], max_items=10, created_at=NOW)[0]
        store = FakeStore(WatcherState(outbox={batch.batch_id: batch}))
        notifier = FakeNotifier({"umo": [True]})
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.MARKET: FakeFetcher(
                    FetchResult(SourceKind.MARKET, False, False, error_code="down")
                )
            },
            notifier=notifier,
            clock=lambda: LATER,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET}, push_targets=[], max_items_per_push=10
        )
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.targets_sent, 1)

    async def test_all_sources_fail_and_empty_targets_do_not_send(self) -> None:
        failures = {
            kind: FakeFetcher(FetchResult(kind, False, False, error_code="down"))
            for kind in SourceKind
        }
        notifier = FakeNotifier()
        service = MarketWatcherService(
            store=FakeStore(),
            fetchers=failures,
            notifier=notifier,
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources=set(SourceKind),
            push_targets=[],
            max_items_per_push=10,
        )
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.sources_failed, 4)
        self.assertEqual(notifier.calls, [])

    async def test_load_or_first_save_failure_sends_nothing(self) -> None:
        load_notifier = FakeNotifier()
        load_service = MarketWatcherService(
            store=FakeStore(load_error=StateCorruptError("broken")),
            fetchers={},
            notifier=load_notifier,
            clock=lambda: NOW,
        )
        load_report = await load_service.check(
            enabled_sources=set(), push_targets=["umo"], max_items_per_push=10
        )
        self.assertEqual(load_report.status, "state_error")
        self.assertEqual(load_notifier.calls, [])

        pending_batch = create_batches(
            SummaryOutboxTests().make_events(1),
            ["old-pending"],
            max_items=10,
            created_at=NOW,
        )[0]
        old = observation(SourceKind.MARKET, "old")
        new = observation(
            SourceKind.MARKET,
            "new",
            canonical_id="github:example/new_plugin",
            name="new_plugin",
            repo_name="new_plugin",
        )
        old_source = source_state(old)
        save_store = FakeStore(
            WatcherState(
                sources={SourceKind.MARKET.value: old_source},
                plugins=merge_sources({SourceKind.MARKET.value: old_source}),
                outbox={pending_batch.batch_id: pending_batch},
            )
        )
        save_store.save_error = StateWriteError("disk")
        save_notifier = FakeNotifier()
        save_service = MarketWatcherService(
            store=save_store,
            fetchers={
                SourceKind.MARKET: FakeFetcher(
                    fetch_result(SourceKind.MARKET, old, new)
                )
            },
            notifier=save_notifier,
            clock=lambda: NOW,
        )
        report = await save_service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["umo"],
            max_items_per_push=10,
        )
        self.assertEqual(report.status, "state_save_failed")
        self.assertEqual(save_notifier.calls, [])

    async def test_invalid_targets_and_actual_batch_insert_count_are_reported(
        self,
    ) -> None:
        old = observation(SourceKind.MARKET, "old")
        new = observation(
            SourceKind.MARKET,
            "new",
            canonical_id="github:example/new",
            name="new",
            repo_name="new",
        )
        old_source = source_state(old)
        state = WatcherState(
            sources={SourceKind.MARKET.value: old_source},
            plugins=merge_sources({SourceKind.MARKET.value: old_source}),
        )
        expected_plugins = merge_sources(
            {SourceKind.MARKET.value: source_state(old, new)}, state.plugins
        )
        expected_events = detect_changes(
            state.plugins, expected_plugins, detected_at=NOW
        )
        existing = create_batches(
            expected_events, ["umo"], max_items=10, created_at=NOW
        )[0]
        existing.targets["umo"].status = DeliveryStatus.SENT
        state.outbox[existing.batch_id] = existing
        store = FakeStore(state)
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.MARKET: FakeFetcher(
                    fetch_result(SourceKind.MARKET, old, new)
                )
            },
            notifier=FakeNotifier(),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["umo", "", 123, "x" * 513],
            max_items_per_push=10,
        )
        self.assertEqual(report.invalid_targets, 3)
        self.assertEqual(report.target_error_code, "invalid_push_targets")
        self.assertEqual(report.batches_created, 0)
        self.assertEqual(set(store.state.outbox), {existing.batch_id})

    async def test_lock_busy_returns_immediately(self) -> None:
        service = MarketWatcherService(
            store=FakeStore(), fetchers={}, notifier=FakeNotifier(), clock=lambda: NOW
        )
        await service.lock.acquire()
        try:
            report = await service.check(
                enabled_sources=set(), push_targets=[], max_items_per_push=10
            )
        finally:
            service.lock.release()
        self.assertTrue(report.busy)


class AstrBotAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_qq_official_umo_is_forwarded_unchanged(self) -> None:
        class MessageChain:
            def message(self, text):
                return f"chain:{text}"

        class Context:
            def __init__(self):
                self.calls = []

            async def send_message(self, target, message):
                self.calls.append((target, message))
                return True

        origin = "qqws:GROUP_MESSAGE:group_openid"
        context = Context()
        result = await AstrBotNotifier(
            context, message_chain_loader=lambda: MessageChain
        ).send(origin, "text")
        self.assertEqual(result, (True, None))
        self.assertEqual(context.calls, [(origin, "chain:text")])

    async def test_false_and_exception_mapping(self) -> None:
        class MessageChain:
            def message(self, text):
                return text

        class Context:
            def __init__(self, result=None, error=None):
                self.result = result
                self.error = error

            async def send_message(self, target, message):
                del target, message
                if self.error:
                    raise self.error
                return self.result

        def loader():
            return MessageChain

        self.assertEqual(
            await AstrBotNotifier(Context(False), message_chain_loader=loader).send(
                "secret-umo", "text"
            ),
            (False, "astrbot_send_false"),
        )
        self.assertEqual(
            await AstrBotNotifier(
                Context(error=RuntimeError("secret")), message_chain_loader=loader
            ).send("secret-umo", "text"),
            (False, "astrbot_send_exception"),
        )


if __name__ == "__main__":
    unittest.main()
