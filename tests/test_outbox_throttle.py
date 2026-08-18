from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone

from market_watcher.models import (
    ChangeEvent,
    ChangeKind,
    DeliveryBatch,
    DeliveryStatus,
    PluginRecord,
    WatcherState,
)
from market_watcher.outbox import (
    create_batches,
    deliver_pending,
    next_outbox_due_at,
)

NOW = "2026-08-07T12:00:00Z"


def event(index: int) -> ChangeEvent:
    canonical_id = f"github:example/plugin-{index}"
    return ChangeEvent(
        event_id=f"event-{index}",
        kind=ChangeKind.UPDATED,
        canonical_id=canonical_id,
        current=PluginRecord(canonical_id=canonical_id, name=f"plugin-{index}"),
        previous=None,
        changed_fields=("version",),
        detected_at=NOW,
    )


class MemorySaver:
    def __init__(self) -> None:
        self.snapshots: list[WatcherState] = []

    def save(self, state: WatcherState) -> None:
        self.snapshots.append(copy.deepcopy(state))


class RecordingNotifier:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.results = list(results or [])
        self.prepared: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.cleared = 0

    async def prepare(self, batch: DeliveryBatch) -> bytes | None:
        self.prepared.append(batch.batch_id)
        return b"png" if batch.card_payload is not None else None

    async def send(self, target: str, message: str) -> tuple[bool, str | None]:
        self.sent.append((target, message))
        success = self.results.pop(0) if self.results else True
        return success, None if success else "send_failed"

    def clear_prepared(self) -> None:
        self.cleared += 1


class MutableClock:
    def __init__(self, value: str = NOW) -> None:
        self.value = datetime.fromisoformat(value.replace("Z", "+00:00"))

    def __call__(self) -> str:
        return self.value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class OutboxThrottleTests(unittest.IsolatedAsyncioTestCase):
    def image_batches(self, count: int, targets: list[str] | None = None):
        return create_batches(
            [event(index) for index in range(count)],
            targets or ["umo:target"],
            max_items=10,
            created_at=NOW,
            enable_image_card=True,
            image_card_max_items=15,
        )

    def test_create_batches_uses_balanced_image_partition_contract(self) -> None:
        expected = {
            1: [1],
            5: [5],
            6: [6],
            10: [10],
            11: [11],
            15: [15],
            16: [8, 8],
            30: [15, 15],
            50: [13, 13, 12, 12],
        }
        for count, sizes in expected.items():
            with self.subTest(count=count):
                batches = self.image_batches(count)
                self.assertEqual([len(batch.event_ids) for batch in batches], sizes)
                self.assertEqual(
                    [
                        int((batch.card_payload or {})["batch_index"])
                        for batch in batches
                    ],
                    list(range(1, len(sizes) + 1)),
                )

    async def test_fifty_events_continue_in_page_order_across_windows(self) -> None:
        batches = self.image_batches(50)
        state = WatcherState(outbox={batch.batch_id: batch for batch in batches})
        saver = MemorySaver()
        notifier = RecordingNotifier()
        clock = MutableClock()

        sent, _ = await deliver_pending(
            state,
            saver,
            notifier,
            now=clock(),
            clock=clock,
            max_images_per_delivery=2,
            image_send_interval_seconds=0,
        )
        ordered = sorted(
            batches, key=lambda item: int((item.card_payload or {})["batch_index"])
        )
        self.assertEqual(sent, 2)
        self.assertEqual(
            [batch.targets["umo:target"].status for batch in ordered],
            [
                DeliveryStatus.SENT,
                DeliveryStatus.SENT,
                DeliveryStatus.PENDING,
                DeliveryStatus.PENDING,
            ],
        )
        self.assertEqual(
            [batch.targets["umo:target"].attempts for batch in ordered],
            [1, 1, 0, 0],
        )
        self.assertEqual(len(notifier.prepared), 2)

        clock.advance(60)
        sent, _ = await deliver_pending(
            state,
            saver,
            notifier,
            now=clock(),
            clock=clock,
            max_images_per_delivery=2,
            image_send_interval_seconds=0,
        )
        self.assertEqual(sent, 2)
        self.assertTrue(
            all(
                batch.targets["umo:target"].status is DeliveryStatus.SENT
                for batch in ordered
            )
        )

    async def test_image_attempts_respect_per_target_send_interval(self) -> None:
        batches = self.image_batches(16)
        state = WatcherState(outbox={batch.batch_id: batch for batch in batches})
        clock = MutableClock()
        sleeps: list[float] = []

        async def sleeper(seconds: float) -> None:
            sleeps.append(seconds)
            clock.advance(seconds)

        sent, _ = await deliver_pending(
            state,
            MemorySaver(),
            RecordingNotifier(),
            now=clock(),
            clock=clock,
            sleeper=sleeper,
            max_images_per_delivery=2,
            image_send_interval_seconds=2,
        )

        self.assertEqual(sent, 2)
        self.assertEqual(sleeps, [2.0])

    async def test_slot_is_persisted_immediately_before_send(self) -> None:
        batch = self.image_batches(1)[0]
        state = WatcherState(outbox={batch.batch_id: batch})
        saver = MemorySaver()

        class CheckpointNotifier(RecordingNotifier):
            async def send(inner_self, target, message):
                self.assertTrue(saver.snapshots)
                window = saver.snapshots[-1].image_delivery_windows[target]
                self.assertEqual(window.used_attempts, 1)
                self.assertIsNotNone(window.last_send_attempt_at)
                return await super().send(target, message)

        await deliver_pending(
            state,
            saver,
            CheckpointNotifier(),
            now=NOW,
            clock=lambda: NOW,
            image_send_interval_seconds=0,
        )

    async def test_early_page_backoff_blocks_later_page_without_prepare(self) -> None:
        batches = self.image_batches(16)
        ordered = sorted(
            batches, key=lambda item: int((item.card_payload or {})["batch_index"])
        )
        ordered[0].targets["umo:target"].status = DeliveryStatus.FAILED
        ordered[0].targets["umo:target"].attempts = 1
        ordered[0].targets["umo:target"].next_retry_at = "2026-08-07T12:00:30Z"
        state = WatcherState(outbox={batch.batch_id: batch for batch in batches})
        notifier = RecordingNotifier()

        sent, _ = await deliver_pending(
            state,
            MemorySaver(),
            notifier,
            now=NOW,
            clock=lambda: NOW,
            image_send_interval_seconds=0,
        )

        self.assertEqual(sent, 0)
        self.assertEqual(notifier.prepared, [])
        self.assertEqual(ordered[1].targets["umo:target"].attempts, 0)
        self.assertEqual(next_outbox_due_at(state, now=NOW), "2026-08-07T12:00:30Z")

    async def test_failed_send_uses_slot_but_targets_have_independent_quotas(
        self,
    ) -> None:
        batch = self.image_batches(1, ["umo:a", "umo:b"])[0]
        state = WatcherState(outbox={batch.batch_id: batch})
        notifier = RecordingNotifier([False, True])

        await deliver_pending(
            state,
            MemorySaver(),
            notifier,
            now=NOW,
            clock=lambda: NOW,
            max_images_per_delivery=1,
            image_send_interval_seconds=0,
        )

        self.assertEqual(state.image_delivery_windows["umo:a"].used_attempts, 1)
        self.assertEqual(state.image_delivery_windows["umo:b"].used_attempts, 1)
        self.assertEqual(batch.targets["umo:a"].attempts, 1)
        self.assertEqual(batch.targets["umo:b"].attempts, 1)
        self.assertEqual(len(notifier.sent), 2)

    async def test_text_batches_bypass_image_quota(self) -> None:
        batches = create_batches(
            [event(index) for index in range(4)],
            ["umo:target"],
            max_items=1,
            created_at=NOW,
        )
        state = WatcherState(outbox={batch.batch_id: batch for batch in batches})
        notifier = RecordingNotifier()

        sent, _ = await deliver_pending(
            state,
            MemorySaver(),
            notifier,
            now=NOW,
            clock=lambda: NOW,
            max_images_per_delivery=1,
            image_send_interval_seconds=30,
        )

        self.assertEqual(sent, 4)
        self.assertEqual(len(notifier.sent), 4)
        self.assertEqual(state.image_delivery_windows, {})


if __name__ == "__main__":
    unittest.main()
