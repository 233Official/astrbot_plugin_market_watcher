"""Interruptible fixed-delay scheduler for M3 automatic checks."""

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
        except TimeoutError:
            return False
