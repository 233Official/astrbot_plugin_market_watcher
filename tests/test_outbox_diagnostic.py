from __future__ import annotations

import asyncio
import inspect
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from market_watcher.models import (
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    WatcherState,
)
from market_watcher.outbox import deliver_pending
from market_watcher.service import (
    OUTBOX_DIAGNOSTIC_HOLD,
    OUTBOX_DIAGNOSTIC_PREFIX,
    MarketWatcherService,
)
from market_watcher.state import JsonStateStore
from tests.test_test_push import DIAGNOSTIC_ORIGIN, Event, collect, load_main_module

NOW = "2026-07-21T12:00:00Z"


class MemoryStore:
    def __init__(self, state=None):
        self.state = deepcopy(state or WatcherState())
        self.save_count = 0

    def load(self):
        return deepcopy(self.state)

    def save(self, state):
        self.save_count += 1
        self.state = deepcopy(state)


class Notifier:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def send(self, target, message):
        self.calls.append((target, message))
        return self.results.pop(0) if self.results else (True, None)

    def clear_prepared(self):
        pass


def service(store, notifier=None):
    return MarketWatcherService(
        store=store,
        fetchers={},
        notifier=notifier or Notifier(),
        clock=lambda: NOW,
    )


def real_batch(target="real-target"):
    return DeliveryBatch(
        batch_id="batch:real",
        event_ids=("event:real",),
        message="real message",
        created_at=NOW,
        targets={target: DeliveryTargetState(target=target)},
    )


class OutboxDiagnosticServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_is_atomic_stable_idempotent_and_outbox_only(self):
        initial = WatcherState(
            updated_at="unchanged",
            subscriptions=["subscription"],
            last_run={"status": "unchanged"},
        )
        store = MemoryStore(initial)
        watcher = service(store)

        first = await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)
        second = await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)

        self.assertEqual(store.save_count, 1)
        self.assertEqual(first, second)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["pending"], 1)
        self.assertEqual(len(store.state.outbox), 1)
        batch_id, batch = next(iter(store.state.outbox.items()))
        self.assertTrue(batch_id.startswith(OUTBOX_DIAGNOSTIC_PREFIX))
        self.assertNotIn(DIAGNOSTIC_ORIGIN, batch_id)
        self.assertEqual(batch.event_ids, ())
        self.assertEqual(
            batch.targets[DIAGNOSTIC_ORIGIN].next_retry_at,
            OUTBOX_DIAGNOSTIC_HOLD,
        )
        self.assertEqual(store.state.updated_at, initial.updated_at)
        self.assertEqual(store.state.subscriptions, initial.subscriptions)
        self.assertEqual(store.state.plugins, initial.plugins)
        self.assertEqual(store.state.last_run, initial.last_run)

    async def test_invalid_origin_is_rejected_without_save(self):
        store = MemoryStore()
        with self.assertRaises(ValueError):
            await service(store).prepare_outbox_diagnostic("   ")
        self.assertEqual(store.save_count, 0)

    async def test_hold_blocks_ordinary_scheduler_delivery(self):
        store = MemoryStore()
        notifier = Notifier()
        watcher = service(store, notifier)
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)
        state = store.load()

        sent, pending = await deliver_pending(
            state, store, notifier, now=NOW, clock=lambda: NOW
        )

        self.assertEqual((sent, pending), (0, 1))
        self.assertEqual(notifier.calls, [])

    async def test_new_service_loads_same_store_and_reports_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JsonStateStore(Path(directory) / "state.json")
            await service(store).prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)

            restarted = service(JsonStateStore(Path(directory) / "state.json"))
            counts = await restarted.outbox_diagnostic_status()

        self.assertEqual(counts["count"], 1)
        self.assertEqual(counts["pending"], 1)

    async def test_deliver_clears_hold_and_uses_production_notifier(self):
        store = MemoryStore()
        notifier = Notifier((True, None))
        watcher = service(store, notifier)
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)

        counts = await watcher.deliver_outbox_diagnostic()

        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][0], DIAGNOSTIC_ORIGIN)
        self.assertIn("出站箱跨重启诊断", notifier.calls[0][1])
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(counts["pending"], 0)
        target = next(iter(store.state.outbox.values())).targets[DIAGNOSTIC_ORIGIN]
        self.assertIs(target.status, DeliveryStatus.SENT)
        self.assertIsNone(target.next_retry_at)

    async def test_delivery_failure_follows_production_attempt_and_backoff(self):
        store = MemoryStore()
        watcher = service(store, Notifier((False, "secret-provider-error")))
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)

        counts = await watcher.deliver_outbox_diagnostic()

        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["pending"], 0)
        target = next(iter(store.state.outbox.values())).targets[DIAGNOSTIC_ORIGIN]
        self.assertIs(target.status, DeliveryStatus.FAILED)
        self.assertEqual(target.attempts, 1)
        self.assertIsNotNone(target.next_retry_at)

    async def test_deliver_may_process_due_real_pending(self):
        state = WatcherState(outbox={"batch:real": real_batch()})
        store = MemoryStore(state)
        notifier = Notifier((True, None), (True, None))
        watcher = service(store, notifier)
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)

        await watcher.deliver_outbox_diagnostic()

        self.assertEqual(
            {call[0] for call in notifier.calls}, {"real-target", DIAGNOSTIC_ORIGIN}
        )
        self.assertIs(
            store.state.outbox["batch:real"].targets["real-target"].status,
            DeliveryStatus.SENT,
        )

    async def test_cleanup_only_removes_diagnostics_and_is_idempotent(self):
        state = WatcherState(outbox={"batch:real": real_batch()})
        store = MemoryStore(state)
        watcher = service(store)
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)

        first = await watcher.cleanup_outbox_diagnostic()
        second = await watcher.cleanup_outbox_diagnostic()

        self.assertEqual(first["removed"], 1)
        self.assertEqual(second["removed"], 0)
        self.assertEqual(set(store.state.outbox), {"batch:real"})

    async def test_diagnostic_operations_share_service_lock(self):
        class BlockingNotifier(Notifier):
            def __init__(self):
                super().__init__()
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def send(self, target, message):
                self.started.set()
                await self.release.wait()
                return await super().send(target, message)

        store = MemoryStore()
        notifier = BlockingNotifier()
        watcher = service(store, notifier)
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)
        delivering = asyncio.create_task(watcher.deliver_outbox_diagnostic())
        await notifier.started.wait()
        status = asyncio.create_task(watcher.outbox_diagnostic_status())
        await asyncio.sleep(0)
        self.assertFalse(status.done())
        notifier.release.set()
        await delivering
        self.assertEqual((await status)["sent"], 1)


class OutboxDiagnosticHandlerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = load_main_module()

    def plugin(self, watcher):
        plugin = object.__new__(self.main.MarketWatcherPlugin)
        plugin._service = watcher
        return plugin

    def test_handlers_are_admin_only_and_have_no_command_arguments(self):
        for name in (
            "test_outbox_prepare",
            "test_outbox_status",
            "test_outbox_deliver",
            "test_outbox_cleanup",
        ):
            handler = self.main.MarketWatcherPlugin.__dict__[name]
            self.assertIn("PermissionType.ADMIN", inspect.getsource(handler))
            self.assertEqual(
                list(inspect.signature(handler).parameters), ["self", "event"]
            )

    async def test_private_chat_is_rejected_without_state_access(self):
        class UnexpectedService:
            def __getattr__(self, name):
                raise AssertionError(name)

        replies = await collect(
            self.plugin(UnexpectedService()).test_outbox_prepare(Event(private=True))
        )
        self.assertEqual(replies, ["出站箱跨重启诊断仅可在群聊中使用。"])

    async def test_invalid_origin_and_state_errors_are_sanitized(self):
        store = MemoryStore()
        event = Event()
        event.unified_msg_origin = "   "
        replies = await collect(self.plugin(service(store)).test_outbox_prepare(event))
        self.assertIn("outbox_diagnostic_state_error", replies[0])
        self.assertNotIn(DIAGNOSTIC_ORIGIN, replies[0])

    async def test_status_output_contains_counts_only(self):
        store = MemoryStore()
        watcher = service(store)
        await watcher.prepare_outbox_diagnostic(DIAGNOSTIC_ORIGIN)
        replies = await collect(self.plugin(watcher).test_outbox_status(Event()))

        output = replies[0]
        self.assertIn("count=1", output)
        self.assertIn("pending=1", output)
        self.assertNotIn(DIAGNOSTIC_ORIGIN, output)
        self.assertNotIn(OUTBOX_DIAGNOSTIC_PREFIX, output)
        self.assertNotIn("持久化 pending outbox", output)
        self.assertNotIn("9999-12-31", output)


if __name__ == "__main__":
    unittest.main()
