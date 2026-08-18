"""Interruptible fixed-delay and wakeable due-time schedulers.

``FixedDelayScheduler`` drives automatic checks at a fixed interval.
``DrainScheduler`` drives the outbox drain: its callback reports how many
seconds until the next due outbox action (or None when nothing is due) and
the loop waits on a wake event or that timeout, never polling state while
idle.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .models import RunReport
from .normalize import utc_now


@dataclass(slots=True)
class SchedulerStatus:
    state: str = "stopped"
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_error_code: str | None = None


class FixedDelayScheduler:
    def __init__(
        self,
        run: Callable[[], Awaitable[RunReport]],
        interval_seconds: Callable[[], float],
        *,
        first_delay_seconds: float = 10,
        clock: Callable[[], str] = utc_now,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.run = run
        self.interval_seconds = interval_seconds
        self.first_delay_seconds = first_delay_seconds
        self.clock = clock
        self.on_error = on_error or (lambda code: None)
        self.status = SchedulerStatus()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.stop_event.clear()
        self.status.state = "waiting"
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self.stop_event.set()
        task = self.task
        self.task = None
        if task is None:
            self.status.state = "stopped"
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.status.state = "stopped"

    async def _loop(self) -> None:
        try:
            if await self._wait(self.first_delay_seconds):
                return
            while not self.stop_event.is_set():
                self.status.state = "running"
                self.status.last_attempt_at = self.clock()
                try:
                    report = await self.run()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.status.state = "error"
                    self.status.last_error_code = "scheduler_run_exception"
                    self.on_error("scheduler_run_exception")
                else:
                    if report.busy:
                        self.status.state = "busy_skipped"
                        self.status.last_error_code = "run_skipped_busy"
                    elif report.status in {"success", "partial"}:
                        self.status.state = "waiting"
                        self.status.last_success_at = self.clock()
                        self.status.last_error_code = None
                    else:
                        self.status.state = "error"
                        self.status.last_error_code = report.error_code or report.status
                if await self._wait(max(0, self.interval_seconds())):
                    return
        finally:
            if self.stop_event.is_set():
                self.status.state = "stopped"

    async def _wait(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0, delay))
            return True
        except asyncio.TimeoutError:
            return False


class DrainScheduler:
    """Wakeable scheduler for outbox drains driven by the next due time.

    The ``callback`` performs the drain and returns the number of seconds
    until the next due outbox action (``0`` means drain immediately) or
    ``None`` when no outbox work remains.  When nothing is due the loop parks
    on the wake event without polling; ``wake()`` interrupts any wait early so
    the next due time is recomputed after the outbox changed.
    """

    def __init__(
        self,
        callback: Callable[[], Awaitable[float | None]],
        *,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.callback = callback
        self.on_error = on_error or (lambda code: None)
        self.status = SchedulerStatus()
        self.wake_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.task is not None and not self.task.done():
            return
        self.stop_event.clear()
        self.wake_event.clear()
        self.status.state = "waiting"
        self.task = asyncio.create_task(self._loop())

    def wake(self) -> None:
        """Interrupt the current wait so the loop recomputes the next due time."""
        self.wake_event.set()

    async def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()
        task = self.task
        self.task = None
        if task is None:
            self.status.state = "stopped"
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.status.state = "stopped"

    async def _loop(self) -> None:
        try:
            while not self.stop_event.is_set():
                self.wake_event.clear()
                self.status.state = "running"
                self.status.last_attempt_at = _drain_clock()
                try:
                    delay = await self.callback()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.status.state = "error"
                    self.status.last_error_code = "drain_callback_exception"
                    self.on_error("drain_callback_exception")
                    delay = 30.0
                else:
                    self.status.last_success_at = _drain_clock()
                    self.status.last_error_code = None
                if self.stop_event.is_set():
                    break
                if delay is None:
                    self.status.state = "waiting"
                    await self._park()
                else:
                    self.status.state = "waiting"
                    await self._wait_for(max(0.0, delay))
        finally:
            if self.stop_event.is_set():
                self.status.state = "stopped"

    async def _park(self) -> None:
        """Wait for wake (or stop, which also sets the wake event)."""
        await self.wake_event.wait()
        self.wake_event.clear()

    async def _wait_for(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.wake_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        else:
            self.wake_event.clear()


def _drain_clock() -> str:
    return utc_now()
