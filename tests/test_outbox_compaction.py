from __future__ import annotations

import json
import unittest
from copy import deepcopy

from market_watcher.models import (
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    WatcherState,
)
from market_watcher.outbox import OUTBOX_DIAGNOSTIC_PREFIX, compact_terminal_batches
from market_watcher.service import MarketWatcherService

NOW = "2026-08-20T12:00:00Z"


class MemoryStore:
    def __init__(self, state: WatcherState) -> None:
        self.state = deepcopy(state)
        self.save_count = 0

    def load(self) -> WatcherState:
        return deepcopy(self.state)

    def save(self, state: WatcherState) -> None:
        self.save_count += 1
        self.state = deepcopy(state)


class Notifier:
    def __init__(self, *results: tuple[bool, str | None]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, str]] = []

    async def prepare(self, batch: DeliveryBatch) -> bytes | None:
        return None

    async def send(self, target: str, message: str) -> tuple[bool, str | None]:
        self.calls.append((target, message))
        return self.results.pop(0) if self.results else (True, None)

    def clear_prepared(self) -> None:
        return None


def batch(
    batch_id: str,
    statuses: tuple[DeliveryStatus, ...],
    *,
    max_attempts: int = 3,
) -> DeliveryBatch:
    targets = {}
    for index, status in enumerate(statuses):
        target = DeliveryTargetState(target=f"target:{index}", status=status)
        if status is DeliveryStatus.FAILED:
            target.attempts = 1
            target.next_retry_at = "2099-01-01T00:00:00Z"
        targets[target.target] = target
    return DeliveryBatch(
        batch_id=batch_id,
        event_ids=(),
        message="safe message",
        created_at=NOW,
        targets=targets,
        max_attempts=max_attempts,
    )


class OutboxCompactionTests(unittest.TestCase):
    def test_terminal_batches_are_removed_but_mixed_and_pending_are_retained(self):
        sent = batch("batch:sent", (DeliveryStatus.SENT,))
        exhausted = batch("batch:exhausted", (DeliveryStatus.EXHAUSTED,))
        mixed = batch("batch:mixed", (DeliveryStatus.SENT, DeliveryStatus.PENDING))
        retryable = batch("batch:retryable", (DeliveryStatus.FAILED,))
        state = WatcherState(
            outbox={item.batch_id: item for item in (sent, exhausted, mixed, retryable)}
        )

        stats = compact_terminal_batches(state)

        self.assertEqual(stats.removed_batches, 2)
        self.assertEqual(stats.removed_sent_targets, 1)
        self.assertEqual(stats.removed_exhausted_targets, 1)
        self.assertEqual(set(state.outbox), {mixed.batch_id, retryable.batch_id})

    def test_failed_at_limit_is_normalized_then_removed(self):
        item = batch("batch:limit", (DeliveryStatus.FAILED,), max_attempts=3)
        target = next(iter(item.targets.values()))
        target.attempts = item.max_attempts
        state = WatcherState(outbox={item.batch_id: item})

        stats = compact_terminal_batches(state)

        self.assertEqual(stats.normalized_exhausted_targets, 1)
        self.assertEqual(stats.removed_batches, 1)
        self.assertEqual(state.outbox, {})

    def test_empty_production_batch_is_terminal_and_diagnostic_is_retained(self):
        empty = batch("batch:empty", ())
        diagnostic = batch(
            f"{OUTBOX_DIAGNOSTIC_PREFIX}terminal", (DeliveryStatus.SENT,)
        )
        state = WatcherState(
            outbox={empty.batch_id: empty, diagnostic.batch_id: diagnostic}
        )

        compact_terminal_batches(state)

        self.assertEqual(set(state.outbox), {diagnostic.batch_id})


class OutboxCompactionServiceTests(unittest.IsolatedAsyncioTestCase):
    def service(self, store: MemoryStore, notifier: Notifier | None = None):
        return MarketWatcherService(
            store=store,
            fetchers={},
            notifier=notifier or Notifier(),
            clock=lambda: NOW,
        )

    async def test_next_outbox_due_is_read_only(self):
        item = batch("batch:pending", (DeliveryStatus.PENDING,))
        store = MemoryStore(WatcherState(outbox={item.batch_id: item}))

        due = await self.service(store).next_outbox_due()

        self.assertEqual(due, NOW)
        self.assertEqual(store.save_count, 0)

    async def test_successful_delivery_removes_production_terminal_batch(self):
        delivered = batch("batch:delivered", (DeliveryStatus.PENDING,))
        pending = batch("batch:pending", (DeliveryStatus.FAILED,))
        store = MemoryStore(
            WatcherState(
                outbox={delivered.batch_id: delivered, pending.batch_id: pending}
            )
        )

        await self.service(store, Notifier((True, None))).drain_pending()

        self.assertNotIn(delivered.batch_id, store.state.outbox)
        self.assertIn(pending.batch_id, store.state.outbox)

    async def test_large_terminal_fixture_compacts_to_live_work(self):
        terminal = {
            f"batch:terminal:{index}": batch(
                f"batch:terminal:{index}", (DeliveryStatus.SENT,)
            )
            for index in range(613)
        }
        pending = batch("batch:pending", (DeliveryStatus.PENDING,))
        diagnostic = batch(
            f"{OUTBOX_DIAGNOSTIC_PREFIX}terminal", (DeliveryStatus.EXHAUSTED,)
        )
        state = WatcherState(
            outbox={
                **terminal,
                pending.batch_id: pending,
                diagnostic.batch_id: diagnostic,
            }
        )
        before = len(json.dumps(state.to_dict(), ensure_ascii=False).encode())

        stats = compact_terminal_batches(state)

        after = len(json.dumps(state.to_dict(), ensure_ascii=False).encode())
        self.assertEqual(stats.removed_batches, 613)
        self.assertEqual(set(state.outbox), {pending.batch_id, diagnostic.batch_id})
        self.assertLess(after, before // 2)


if __name__ == "__main__":
    unittest.main()
