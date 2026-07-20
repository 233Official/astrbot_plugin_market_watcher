"""Small, source-neutral data models for future collection and detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SourceKind(str, Enum):
    """Supported discovery source categories."""

    MARKET = "market"
    COLLECTION_ISSUE = "collection_issue"
    LEGACY_PUBLISH_ISSUE = "legacy_publish_issue"
    GITHUB_DISCOVERY = "github_discovery"


class ChangeKind(str, Enum):
    """Normalized changes that may eventually trigger a notification."""

    DISCOVERED = "discovered"
    UPDATED = "updated"
    REMOVED = "removed"
    STAR_CHANGED = "star_changed"


@dataclass(frozen=True, slots=True)
class PluginRecord:
    """Represent one normalized plugin observation."""

    canonical_id: str
    name: str
    repo_url: str
    source: SourceKind
    observed_at: datetime
    version: str | None = None
    description: str | None = None
    stars: int | None = None
    source_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """Describe a detected transition between stored and current records."""

    kind: ChangeKind
    current: PluginRecord
    previous: PluginRecord | None = None
    changed_fields: tuple[str, ...] = ()
