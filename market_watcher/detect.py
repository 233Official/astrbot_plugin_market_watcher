"""Pure MVP discovered/updated event detection."""

from __future__ import annotations

import hashlib

from .models import ChangeEvent, ChangeKind, PluginRecord

UPDATE_FIELDS = (
    "display_name",
    "description",
    "author",
    "version",
    "repo_url",
    "astrbot_version",
    "platforms",
    "market_status",
    "issue_state",
    "issue_labels",
    "archived",
)


def detect_changes(
    previous: dict[str, PluginRecord],
    current: dict[str, PluginRecord],
    *,
    detected_at: str,
) -> list[ChangeEvent]:
    events: list[ChangeEvent] = []
    for canonical_id, record in sorted(current.items()):
        old = previous.get(canonical_id)
        if old is None:
            events.append(_event(ChangeKind.DISCOVERED, record, None, (), detected_at))
            continue
        current_hash = record.compute_content_hash()
        old_hash = old.compute_content_hash()
        if current_hash == old_hash:
            continue
        changed_fields = tuple(
            field_name
            for field_name in UPDATE_FIELDS
            if getattr(old, field_name) != getattr(record, field_name)
        )
        if changed_fields:
            events.append(
                _event(
                    ChangeKind.UPDATED,
                    record,
                    old,
                    changed_fields,
                    detected_at,
                )
            )
    return sorted(
        events,
        key=lambda event: (
            0 if event.kind is ChangeKind.DISCOVERED else 1,
            event.canonical_id,
        ),
    )


def _event(
    kind: ChangeKind,
    current: PluginRecord,
    previous: PluginRecord | None,
    changed_fields: tuple[str, ...],
    detected_at: str,
) -> ChangeEvent:
    current_hash = current.compute_content_hash()
    previous_hash = previous.compute_content_hash() if previous else "none"
    digest = hashlib.sha256(
        f"{kind.value}\0{current.canonical_id}\0{previous_hash}\0{current_hash}".encode()
    ).hexdigest()
    return ChangeEvent(
        event_id=f"event:{kind.value}:{digest}",
        kind=kind,
        canonical_id=current.canonical_id,
        current=current,
        previous=previous,
        changed_fields=changed_fields,
        detected_at=detected_at,
    )
