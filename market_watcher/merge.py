"""Deterministic cross-source merge for normalized observations."""

from __future__ import annotations

from collections import defaultdict

from .models import (
    PluginRecord,
    SourceEvidence,
    SourceKind,
    SourceObservation,
    SourceState,
)

SOURCE_PRIORITY = {
    SourceKind.MARKET: 0,
    SourceKind.COLLECTION_ISSUE: 1,
    SourceKind.LEGACY_PUBLISH_ISSUE: 2,
    SourceKind.GITHUB_DISCOVERY: 3,
}
MERGED_FIELDS = (
    "name",
    "repo_url",
    "repo_owner",
    "repo_name",
    "display_name",
    "description",
    "author",
    "version",
    "astrbot_version",
    "platforms",
    "tags",
    "market_status",
    "issue_state",
    "issue_labels",
    "stars",
    "forks",
    "archived",
    "repo_updated_at",
)


def merge_sources(
    sources: dict[str, SourceState],
    previous_plugins: dict[str, PluginRecord] | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[str, PluginRecord]:
    previous_plugins = previous_plugins or {}
    aliases = aliases or {}
    grouped: dict[str, list[SourceObservation]] = defaultdict(list)
    for state in sources.values():
        for observation in state.observations.values():
            grouped[_resolve_alias(observation.canonical_id, aliases)].append(
                observation
            )

    result: dict[str, PluginRecord] = {}
    for canonical_id, observations in sorted(grouped.items()):
        observations.sort(
            key=lambda item: (
                SOURCE_PRIORITY[item.source_kind],
                item.source_record_id,
            )
        )
        previous = previous_plugins.get(canonical_id)
        evidence = _merge_evidence(observations, previous)
        values: dict[str, object] = {}
        field_sources: dict[str, SourceEvidence] = {}
        for field_name in MERGED_FIELDS:
            selected = _select_field(field_name, observations, previous)
            if selected is None:
                values[field_name] = getattr(previous, field_name, None)
                if previous and field_name in previous.field_sources:
                    field_sources[field_name] = previous.field_sources[field_name]
                continue
            value, observation = selected
            values[field_name] = value
            field_sources[field_name] = _evidence(observation)

        name = values["name"] or values["repo_name"] or canonical_id
        observed_values = [item.observed_at for item in observations]
        first_seen = previous.first_seen_at if previous else min(observed_values)
        record = PluginRecord(
            canonical_id=canonical_id,
            name=str(name),
            repo_url=_optional_str(values["repo_url"]),
            repo_owner=_optional_str(values["repo_owner"]),
            repo_name=_optional_str(values["repo_name"]),
            display_name=_optional_str(values["display_name"]),
            description=_optional_str(values["description"]),
            author=_optional_str(values["author"]),
            version=_optional_str(values["version"]),
            astrbot_version=_optional_str(values["astrbot_version"]),
            platforms=_tuple(values["platforms"]),
            tags=_tuple(values["tags"]),
            market_status=_optional_str(values["market_status"]),
            issue_state=_optional_str(values["issue_state"]),
            issue_labels=_tuple(values["issue_labels"]),
            stars=_optional_int(values["stars"]),
            forks=_optional_int(values["forks"]),
            archived=_optional_bool(values["archived"]),
            repo_updated_at=_optional_str(values["repo_updated_at"]),
            observed_at=max(observed_values),
            first_seen_at=first_seen,
            last_seen_at=max(observed_values),
            field_sources=field_sources,
            evidence=evidence,
        )
        record.content_hash = record.compute_content_hash()
        result[canonical_id] = record
    return result


def _select_field(
    field_name: str,
    observations: list[SourceObservation],
    previous: PluginRecord | None,
) -> tuple[object, SourceObservation] | None:
    candidates: list[tuple[int, object, SourceObservation]] = []
    for observation in observations:
        value = getattr(observation, field_name)
        if _is_empty(value):
            continue
        candidates.append(
            (SOURCE_PRIORITY[observation.source_kind], value, observation)
        )
    candidates.sort(key=lambda item: (item[0], item[2].source_record_id))
    if not candidates:
        return None
    if previous and not _is_empty(getattr(previous, field_name)):
        old_evidence = previous.field_sources.get(field_name)
        old_priority = (
            SOURCE_PRIORITY[old_evidence.source_kind]
            if old_evidence
            else len(SOURCE_PRIORITY)
        )
        if old_priority < candidates[0][0]:
            return None
    return candidates[0][1], candidates[0][2]


def _merge_evidence(
    observations: list[SourceObservation], previous: PluginRecord | None
) -> tuple[SourceEvidence, ...]:
    values = {
        (
            evidence.source_kind.value,
            evidence.source_record_id,
            evidence.source_url,
        ): evidence
        for evidence in (previous.evidence if previous else ())
    }
    for observation in observations:
        evidence = _evidence(observation)
        values[
            (evidence.source_kind.value, evidence.source_record_id, evidence.source_url)
        ] = evidence
    return tuple(
        sorted(
            values.values(),
            key=lambda item: (
                SOURCE_PRIORITY[item.source_kind],
                item.source_record_id,
                item.source_url,
            ),
        )
    )


def _evidence(observation: SourceObservation) -> SourceEvidence:
    return SourceEvidence(
        source_kind=observation.source_kind,
        source_record_id=observation.source_record_id,
        source_url=observation.source_url,
        observed_at=observation.observed_at,
    )


def _resolve_alias(value: str, aliases: dict[str, str]) -> str:
    visited: set[str] = set()
    while (
        value in aliases
        and value not in visited
        and value.startswith("source:")
        and aliases[value].startswith("github:")
    ):
        visited.add(value)
        value = aliases[value]
    return value


def _is_empty(value: object) -> bool:
    return value is None or value == "" or value == () or value == []


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _tuple(value: object) -> tuple[str, ...]:
    return tuple(value) if isinstance(value, (tuple, list)) else ()


def _optional_int(value: object) -> int | None:
    return value if type(value) is int else None


def _optional_bool(value: object) -> bool | None:
    return value if type(value) is bool else None
