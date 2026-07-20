"""Protocols reserved for fetcher, detector, summarizer, pusher, and state layers."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import ChangeEvent, PluginRecord


class Fetcher(Protocol):
    """Fetch and normalize observations from one external source."""

    async def fetch(self) -> Sequence[PluginRecord]: ...


class ChangeDetector(Protocol):
    """Compare observations with persisted state without performing I/O."""

    def detect(
        self,
        previous: Sequence[PluginRecord],
        current: Sequence[PluginRecord],
    ) -> Sequence[ChangeEvent]: ...


class Summarizer(Protocol):
    """Create an optional human-readable summary for detected changes."""

    async def summarize(self, events: Sequence[ChangeEvent]) -> str: ...


class UpdatePusher(Protocol):
    """Deliver prepared notifications to configured AstrBot targets."""

    async def push(self, target: str, message: str) -> None: ...


class StateStore(Protocol):
    """Load and atomically save normalized observations and run metadata."""

    async def load_records(self) -> Sequence[PluginRecord]: ...

    async def save_records(self, records: Sequence[PluginRecord]) -> None: ...
