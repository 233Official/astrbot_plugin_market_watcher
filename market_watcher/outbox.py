"""Stable at-least-once outbox creation and per-target delivery."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import (
    ChangeEvent,
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    WatcherState,
)
from .summary import MAX_MESSAGE_LENGTH, chunk_events, render_summary

MAX_TARGET_LENGTH = 512


class Notifier(Protocol):
    async def send(self, target: str, message: str) -> tuple[bool, str | None]: ...


class StateSaver(Protocol):
    def save(self, state: WatcherState) -> None: ...


def create_batches(
    events: list[ChangeEvent],
    targets: object,
    *,
    max_items: int,
    created_at: str,
    intro: str | None = None,
) -> list[DeliveryBatch]:
    clean_targets, _ = validate_targets(targets)
    if not events or not clean_targets:
        return []
    chunks = chunk_events(events, max_items)
    batches: list[DeliveryBatch] = []
    for index, chunk in enumerate(chunks, 1):
        facts = render_summary(chunk, index, len(chunks), total_items=len(events))
        candidate = f"{intro}\n\n{facts}" if intro else facts
        message = candidate if len(candidate) <= MAX_MESSAGE_LENGTH else facts
        event_ids = tuple(event.event_id for event in chunk)
        digest = hashlib.sha256(
            (
                "\0".join(
                    (*event_ids, *clean_targets, f"batch-index:{index}/{len(chunks)}")
                )
            ).encode()
        ).hexdigest()
        batch_id = f"batch:{digest}"
        batches.append(
            DeliveryBatch(
                batch_id=batch_id,
                event_ids=event_ids,
                message=message,
                created_at=created_at,
                targets={
                    target: DeliveryTargetState(target=target)
                    for target in clean_targets
                },
            )
        )
    return batches


async def deliver_pending(
    state: WatcherState,
    saver: StateSaver,
    notifier: Notifier,
    *,
    now: str,
    clock: Callable[[], str],
) -> tuple[int, int]:
    sent = 0
    pending = 0
    normalized = False
    for batch in state.outbox.values():
        for target_state in batch.targets.values():
            if (
                target_state.status is DeliveryStatus.FAILED
                and target_state.attempts >= batch.max_attempts
            ):
                target_state.status = DeliveryStatus.EXHAUSTED
                target_state.next_retry_at = None
                normalized = True
            if target_state.status in {
                DeliveryStatus.SENT,
                DeliveryStatus.EXHAUSTED,
            }:
                continue
            if target_state.next_retry_at and target_state.next_retry_at > now:
                pending += 1
                continue
            target_state.last_attempt_at = clock()
            try:
                success, error_code = await notifier.send(
                    target_state.target, batch.message
                )
            except Exception:
                success, error_code = False, "delivery_exception"
            target_state.attempts += 1
            if success:
                target_state.status = DeliveryStatus.SENT
                target_state.last_error_code = None
                target_state.next_retry_at = None
                sent += 1
            else:
                target_state.last_error_code = error_code or "delivery_failed"
                if target_state.attempts >= batch.max_attempts:
                    target_state.status = DeliveryStatus.EXHAUSTED
                    target_state.next_retry_at = None
                else:
                    target_state.status = DeliveryStatus.FAILED
                    target_state.next_retry_at = _retry_at(
                        target_state.last_attempt_at, target_state.attempts
                    )
                    pending += 1
            saver.save(state)
    if normalized:
        saver.save(state)
    return sent, pending


def count_pending(state: WatcherState) -> int:
    return sum(
        target.status in {DeliveryStatus.PENDING, DeliveryStatus.FAILED}
        for batch in state.outbox.values()
        for target in batch.targets.values()
    )


def count_exhausted(state: WatcherState) -> int:
    return sum(
        target.status is DeliveryStatus.EXHAUSTED
        for batch in state.outbox.values()
        for target in batch.targets.values()
    )


def validate_targets(targets: object) -> tuple[list[str], int]:
    if type(targets) is not list:
        return [], 1
    clean: set[str] = set()
    invalid = 0
    for target in targets:
        if not isinstance(target, str):
            invalid += 1
            continue
        normalized = target.strip()
        if not normalized or len(normalized) > MAX_TARGET_LENGTH:
            invalid += 1
            continue
        clean.add(normalized)
    return sorted(clean), invalid


def merge_targets(configured: object, subscriptions: object) -> tuple[list[str], int]:
    configured_targets, configured_invalid = validate_targets(configured)
    subscription_targets, subscription_invalid = validate_targets(subscriptions)
    return (
        sorted(set(configured_targets) | set(subscription_targets)),
        configured_invalid + subscription_invalid,
    )


def _retry_at(value: str, attempts: int) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (
        (parsed + timedelta(seconds=min(300, 2 ** max(0, attempts - 1))))
        .isoformat()
        .replace("+00:00", "Z")
    )
