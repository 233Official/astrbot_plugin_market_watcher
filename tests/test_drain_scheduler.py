from __future__ import annotations

import asyncio
import unittest

from market_watcher.scheduler import DrainScheduler


class DrainSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def test_idle_scheduler_parks_until_wake_and_stops_cleanly(self) -> None:
        calls = 0
        first = asyncio.Event()
        second = asyncio.Event()

        async def callback() -> float | None:
            nonlocal calls
            calls += 1
            (first if calls == 1 else second).set()
            return None

        scheduler = DrainScheduler(callback)
        scheduler.start()
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(calls, 1)

        scheduler.wake()
        await asyncio.wait_for(second.wait(), 1)
        await asyncio.wait_for(scheduler.stop(), 1)

        self.assertIsNone(scheduler.task)
        self.assertEqual(scheduler.status.state, "stopped")

    async def test_due_timeout_runs_callback_without_busy_polling(self) -> None:
        calls = 0
        twice = asyncio.Event()

        async def callback() -> float | None:
            nonlocal calls
            calls += 1
            if calls == 1:
                return 0.01
            twice.set()
            return None

        scheduler = DrainScheduler(callback)
        scheduler.start()
        await asyncio.wait_for(twice.wait(), 1)
        self.assertEqual(calls, 2)
        await asyncio.wait_for(scheduler.stop(), 1)

    async def test_callback_exception_is_contained_and_reported(self) -> None:
        error = asyncio.Event()

        async def callback() -> float | None:
            raise RuntimeError("boom")

        scheduler = DrainScheduler(callback, on_error=lambda _: error.set())
        scheduler.start()
        await asyncio.wait_for(error.wait(), 1)
        self.assertEqual(scheduler.status.last_error_code, "drain_callback_exception")
        await asyncio.wait_for(scheduler.stop(), 1)


if __name__ == "__main__":
    unittest.main()
