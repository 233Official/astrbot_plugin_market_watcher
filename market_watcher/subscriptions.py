"""Atomic group subscription operations independent from AstrBot imports."""

from __future__ import annotations

from typing import Protocol

from .models import WatcherState
from .outbox import validate_targets


class SubscriptionStore(Protocol):
    def load(self) -> WatcherState: ...

    def save(self, state: WatcherState) -> None: ...


def update_subscription(store: SubscriptionStore, event, *, subscribe: bool) -> str:
    if event.is_private_chat():
        return "private_chat"
    targets, invalid = validate_targets([event.unified_msg_origin])
    if invalid or not targets:
        return "invalid_origin"
    origin = targets[0]
    state = store.load()
    existing = set(state.subscriptions)
    if subscribe:
        if origin in existing:
            return "already_subscribed"
        existing.add(origin)
        result = "subscribed"
    else:
        if origin not in existing:
            return "already_unsubscribed"
        existing.remove(origin)
        result = "unsubscribed"
    state.subscriptions = sorted(existing)
    store.save(state)
    return result


def subscription_status(store: SubscriptionStore, event) -> tuple[int, bool | None]:
    state = store.load()
    if event.is_private_chat():
        return len(state.subscriptions), None
    targets, invalid = validate_targets([event.unified_msg_origin])
    current = bool(not invalid and targets and targets[0] in state.subscriptions)
    return len(state.subscriptions), current
