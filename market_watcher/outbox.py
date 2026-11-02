"""Stable at-least-once outbox creation, per-target delivery and image throttling.

Image mode paginates with the Designer ``partition_events_balanced`` contract
(one page per DeliveryBatch) and throttles image sends per target with a
persisted 60-second window and a per-target minimum send gap.  Pure text
batches keep the existing ``max_items_per_push`` semantics and are never
window/interval limited.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .card_renderer import build_card_payload, partition_events_balanced
from .models import (
    ChangeEvent,
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    ImageDeliveryWindow,
    WatcherState,
)
from .summary import MAX_MESSAGE_LENGTH, chunk_events, render_summary

MAX_TARGET_LENGTH = 512
OUTBOX_DIAGNOSTIC_PREFIX = "diagnostic:outbox:"

# Fixed internal image delivery window length in seconds.  Not configurable in
# the first release per issue #3 design constraints.
IMAGE_WINDOW_SECONDS = 60


class Notifier(Protocol):
    async def prepare(self, batch: DeliveryBatch) -> bytes | None:
        """Render the card for a batch. Stores result for subsequent send calls."""
        ...

    async def send(self, target: str, message: str) -> tuple[bool, str | None]: ...

    def clear_prepared(self) -> None:
        """Clear stored image bytes and delivery mode after a batch completes."""
        ...


class StateSaver(Protocol):
    def save(self, state: WatcherState) -> None: ...


@dataclass(frozen=True, slots=True)
class OutboxCompactionStats:
    """Non-content counts describing one in-memory outbox compaction."""

    removed_batches: int = 0
    removed_sent_targets: int = 0
    removed_exhausted_targets: int = 0
    normalized_exhausted_targets: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.removed_batches or self.normalized_exhausted_targets)

    def __getitem__(self, key: str) -> int:
        return getattr(self, key)


def compact_terminal_batches(state: WatcherState) -> OutboxCompactionStats:
    """Normalize exhausted targets and remove terminal production batches.

    This helper only mutates the supplied in-memory state.  Diagnostic batches
    are deliberately retained for the explicit diagnostic cleanup command.
    """
    removed_batches = 0
    removed_sent_targets = 0
    removed_exhausted_targets = 0
    normalized_exhausted_targets = 0
    for batch_id, batch in list(state.outbox.items()):
        if batch_id in state.legacy_outbox_ids:
            continue
        for target_state in batch.targets.values():
            if (
                target_state.status is DeliveryStatus.FAILED
                and target_state.attempts >= batch.max_attempts
            ):
                target_state.status = DeliveryStatus.EXHAUSTED
                target_state.next_retry_at = None
                normalized_exhausted_targets += 1
        if batch_id.startswith(OUTBOX_DIAGNOSTIC_PREFIX):
            continue
        if all(
            target_state.status in {DeliveryStatus.SENT, DeliveryStatus.EXHAUSTED}
            for target_state in batch.targets.values()
        ):
            removed_batches += 1
            removed_sent_targets += sum(
                target_state.status is DeliveryStatus.SENT
                for target_state in batch.targets.values()
            )
            removed_exhausted_targets += sum(
                target_state.status is DeliveryStatus.EXHAUSTED
                for target_state in batch.targets.values()
            )
            del state.outbox[batch_id]
    return OutboxCompactionStats(
        removed_batches=removed_batches,
        removed_sent_targets=removed_sent_targets,
        removed_exhausted_targets=removed_exhausted_targets,
        normalized_exhausted_targets=normalized_exhausted_targets,
    )


def create_batches(
    events: list[ChangeEvent],
    targets: object,
    *,
    max_items: int,
    created_at: str,
    intro: str | None = None,
    enable_image_card: bool = False,
    image_card_max_items: int = 15,
) -> list[DeliveryBatch]:
    clean_targets, _ = validate_targets(targets)
    if not events or not clean_targets:
        return []
    if enable_image_card:
        chunks = partition_events_balanced(events, image_card_max_items)
    else:
        chunks = chunk_events(events, max_items)
    group_id = _delivery_group_id(
        tuple(event.event_id for event in events),
        clean_targets,
        "image" if enable_image_card else "text",
    )
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
        card_payload: dict[str, object] | None = None
        if enable_image_card:
            card_payload = build_card_payload(
                chunk,
                intro=_safe_card_intro(intro),
                batch_index=index,
                batch_total=len(chunks),
                total_items=len(events),
            )
            # Stable per-check group id (template ignores unknown fields).
            card_payload["delivery_group_id"] = group_id
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
                card_payload=card_payload,
            )
        )
    return batches


def _safe_card_intro(intro: str | None) -> str:
    return intro or "本次市场监测到新的插件动态。"


def _delivery_group_id(
    event_ids: tuple[str, ...], targets: list[str], mode: str
) -> str:
    digest = hashlib.sha256(
        ("\0".join((*event_ids, *sorted(targets), mode))).encode()
    ).hexdigest()
    return f"delivery:{digest}"


def _batch_order_key(batch: DeliveryBatch) -> tuple[object, ...]:
    payload = batch.card_payload or {}
    batch_index = payload.get("batch_index")
    # Image pages sort before text batches on a created_at tie so a text
    # fallback never overtakes an earlier image page of the same check.
    mode_rank = 0 if batch.card_payload is not None else 1
    return (
        batch.created_at,
        mode_rank,
        str(payload.get("delivery_group_id") or ""),
        batch_index if isinstance(batch_index, int) else 0,
        batch.batch_id,
    )


async def deliver_pending(
    state: WatcherState,
    saver: StateSaver,
    notifier: Notifier,
    *,
    now: str,
    clock: Callable[[], str],
    sleeper: Callable[[float], Awaitable[None]] | None = None,
    max_images_per_delivery: int = 2,
    image_send_interval_seconds: int = 2,
    image_window_seconds: int = IMAGE_WINDOW_SECONDS,
) -> tuple[int, int]:
    wait_sleeper = sleeper or asyncio.sleep
    sent = 0
    pending = 0
    normalized = False
    # Track targets that already waited on the send gap in this pass so a
    # non-advancing clock can never busy-loop.
    interval_waited: set[str] = set()
    blocked_groups: set[tuple[str, str]] = set()
    for batch in _ordered_batches(state):
        group_id = _image_group_id(batch)
        # Normalise exhausted targets before checking due-ness
        for target_state in batch.targets.values():
            if (
                target_state.status is DeliveryStatus.FAILED
                and target_state.attempts >= batch.max_attempts
            ):
                target_state.status = DeliveryStatus.EXHAUSTED
                target_state.next_retry_at = None
                normalized = True

        # Determine eligible targets before prepare; quota skips never prepare.
        eligible: list[DeliveryTargetState] = []
        for target_state in batch.targets.values():
            if target_state.status in {DeliveryStatus.SENT, DeliveryStatus.EXHAUSTED}:
                continue
            group_key = (
                (group_id, target_state.target) if group_id is not None else None
            )
            if group_key is not None and group_key in blocked_groups:
                pending += 1
                continue
            if target_state.next_retry_at and target_state.next_retry_at > now:
                pending += 1
                if group_key is not None:
                    blocked_groups.add(group_key)
                continue
            if batch.card_payload is None:
                # Pure text batch: no image window or interval constraints.
                eligible.append(target_state)
                continue
            window = state.image_delivery_windows.setdefault(
                target_state.target, ImageDeliveryWindow()
            )
            now_dt = parse_utc(now)
            if not _window_allows(
                window, now_dt, max_images_per_delivery, image_window_seconds
            ):
                # Quota skip: keep state unchanged, no attempt/error/retry.
                pending += 1
                if group_key is not None:
                    blocked_groups.add(group_key)
                continue
            if window.last_send_attempt_at is not None:
                min_at = parse_utc(window.last_send_attempt_at) + timedelta(
                    seconds=image_send_interval_seconds
                )
                if min_at > now_dt:
                    if target_state.target in interval_waited:
                        pending += 1
                        if group_key is not None:
                            blocked_groups.add(group_key)
                        continue
                    wait = (min_at - now_dt).total_seconds()
                    if wait <= 0:
                        pending += 1
                        if group_key is not None:
                            blocked_groups.add(group_key)
                        continue
                    await wait_sleeper(wait)
                    interval_waited.add(target_state.target)
                    now = clock()
                    now_dt = parse_utc(now)
                    if not _window_allows(
                        window,
                        now_dt,
                        max_images_per_delivery,
                        image_window_seconds,
                    ):
                        pending += 1
                        if group_key is not None:
                            blocked_groups.add(group_key)
                        continue
            eligible.append(target_state)

        if not eligible:
            # No eligible target for this batch; skip prepare entirely.
            continue

        # Prepare phase: clears temp image bytes; if card_payload is None,
        # prepare returns None and no image will be attempted.
        # try/finally ensures clear_prepared() runs after every batch attempt,
        # including CancelledError / exception exits.
        try:
            try:
                await notifier.prepare(batch)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

            for target_state in eligible:
                # Persist this target's slot immediately before its real send.
                # A cancellation before another target starts must not consume
                # that other target's quota.
                if batch.card_payload is not None:
                    _reserve_slot(
                        state.image_delivery_windows[target_state.target],
                        clock(),
                        image_window_seconds,
                    )
                    saver.save(state)
                target_state.last_attempt_at = clock()
                try:
                    success, error_code = await notifier.send(
                        target_state.target, batch.message
                    )
                except asyncio.CancelledError:
                    raise
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
                        if group_id is not None:
                            blocked_groups.add((group_id, target_state.target))
                saver.save(state)
        finally:
            notifier.clear_prepared()
    if normalized:
        saver.save(state)
    return sent, pending


def _window_allows(
    window: ImageDeliveryWindow,
    now_dt: datetime,
    max_images: int,
    window_seconds: int,
) -> bool:
    if window.window_started_at is None:
        return True
    started = parse_utc(window.window_started_at)
    if (now_dt - started).total_seconds() >= window_seconds:
        return True
    return window.used_attempts < max_images


def _reserve_slot(
    window: ImageDeliveryWindow, now_value: str, window_seconds: int
) -> None:
    now_dt = parse_utc(now_value)
    if (
        window.window_started_at is None
        or (now_dt - parse_utc(window.window_started_at)).total_seconds()
        >= window_seconds
    ):
        window.window_started_at = now_value
        window.used_attempts = 0
    window.used_attempts += 1
    window.last_send_attempt_at = now_value


def _ordered_batches(state: WatcherState) -> list[DeliveryBatch]:
    quarantined = set(state.legacy_outbox_ids)
    return sorted(
        (batch for key, batch in state.outbox.items() if key not in quarantined),
        key=_batch_order_key,
    )


def _image_group_id(batch: DeliveryBatch) -> str | None:
    if batch.card_payload is None:
        return None
    value = batch.card_payload.get("delivery_group_id")
    return value if isinstance(value, str) and value else batch.batch_id


def next_outbox_due_at(
    state: WatcherState,
    *,
    now: str,
    max_images_per_delivery: int = 2,
    image_send_interval_seconds: int = 2,
    image_window_seconds: int = IMAGE_WINDOW_SECONDS,
) -> str | None:
    """Return the earliest UTC time pending outbox work can proceed.

    Returns ``now`` when actionable work exists immediately (drain now), a
    strictly future timestamp when all remaining work is time-blocked, and
    None when nothing remains to deliver.
    """
    now_dt = parse_utc(now)
    actionable = False
    candidates: list[datetime] = []
    blocked_groups: set[tuple[str, str]] = set()
    for batch in _ordered_batches(state):
        group_id = _image_group_id(batch)
        for target_state in batch.targets.values():
            if target_state.status in {DeliveryStatus.SENT, DeliveryStatus.EXHAUSTED}:
                continue
            group_key = (
                (group_id, target_state.target) if group_id is not None else None
            )
            if group_key is not None and group_key in blocked_groups:
                continue
            earliest = now_dt
            if target_state.next_retry_at:
                retry_dt = parse_utc(target_state.next_retry_at)
                earliest = max(earliest, retry_dt)
            if batch.card_payload is not None:
                window = state.image_delivery_windows.get(target_state.target)
                if window is not None:
                    if window.window_started_at is not None:
                        started = parse_utc(window.window_started_at)
                        if (
                            (now_dt - started).total_seconds() < image_window_seconds
                            and window.used_attempts >= max_images_per_delivery
                        ):
                            earliest = max(
                                earliest,
                                started + timedelta(seconds=image_window_seconds),
                            )
                    if window.last_send_attempt_at is not None:
                        earliest = max(
                            earliest,
                            parse_utc(window.last_send_attempt_at)
                            + timedelta(seconds=image_send_interval_seconds),
                        )
            if earliest > now_dt:
                candidates.append(earliest)
                if group_key is not None:
                    blocked_groups.add(group_key)
            else:
                actionable = True
    if actionable:
        return now
    if not candidates:
        return None
    return _format_utc(min(candidates))


def count_throttled(
    state: WatcherState,
    *,
    now: str,
    max_images_per_delivery: int = 2,
    image_send_interval_seconds: int = 2,
    image_window_seconds: int = IMAGE_WINDOW_SECONDS,
) -> int:
    """Count image targets currently deferred by window quota or send gap."""
    now_dt = parse_utc(now)
    count = 0
    for batch in state.outbox.values():
        if batch.batch_id in state.legacy_outbox_ids:
            continue
        if batch.card_payload is None:
            continue
        for target_state in batch.targets.values():
            if target_state.status not in {
                DeliveryStatus.PENDING,
                DeliveryStatus.FAILED,
            }:
                continue
            if (
                target_state.next_retry_at
                and parse_utc(target_state.next_retry_at) > now_dt
            ):
                continue
            window = state.image_delivery_windows.get(target_state.target)
            if window is None:
                continue
            if not _window_allows(
                window, now_dt, max_images_per_delivery, image_window_seconds
            ):
                count += 1
                continue
            if window.last_send_attempt_at is not None:
                min_at = parse_utc(window.last_send_attempt_at) + timedelta(
                    seconds=image_send_interval_seconds
                )
                if min_at > now_dt:
                    count += 1
    return count


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


def parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _retry_at(value: str, attempts: int) -> str:
    parsed = parse_utc(value)
    return (
        (parsed + timedelta(seconds=min(300, 2 ** max(0, attempts - 1))))
        .isoformat()
        .replace("+00:00", "Z")
    )
