"""AstrBot market API with an endpoint-isolated raw fallback."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import classify_exception
from ..http import HttpClient, HttpResponse
from ..models import FetchResult, SourceKind, SourceObservation, SourceState
from ..normalize import (
    bounded_excerpt,
    fallback_canonical_id,
    normalize_github_repo,
    observation_hash,
    repo_parts,
    sanitize_text,
    utc_now,
)

MARKET_API_URL = "https://api.soulter.top/astrbot/plugins"
MARKET_RAW_URL = (
    "https://raw.githubusercontent.com/AstrBotDevs/"
    "AstrBot_Plugins_Collection/main/plugins.json"
)


class MarketFetcher:
    def __init__(self, http: HttpClient, *, clock: Callable[[], str] = utc_now) -> None:
        self.http = http
        self.clock = clock

    async def fetch(self, previous: SourceState | None = None) -> FetchResult:
        primary = await self._request(MARKET_API_URL, previous, sparse=False)
        if primary.success and primary.complete:
            return primary
        fallback = await self._request(MARKET_RAW_URL, previous, sparse=True)
        fallback.from_fallback = True
        if not (fallback.success and fallback.complete):
            fallback.error_code = "market_and_fallback_failed"
            fallback.error_summary = "primary and fallback were not complete"
        return fallback

    async def _request(
        self,
        endpoint: str,
        previous: SourceState | None,
        *,
        sparse: bool,
    ) -> FetchResult:
        snapshot = previous.snapshot_for(endpoint) if previous else None
        headers = _conditional_headers(
            snapshot.etag if snapshot else None,
            snapshot.last_modified if snapshot else None,
        )
        try:
            response = await self.http.get(endpoint, headers=headers)
        except Exception as exc:
            code, summary = classify_exception(exc)
            return _failure(endpoint, None, code, summary)
        if response.status == 304:
            if snapshot is None:
                return _failure(
                    endpoint,
                    response,
                    "not_modified_without_endpoint_snapshot",
                    "304 had no matching endpoint snapshot",
                )
            return FetchResult(
                source_kind=SourceKind.MARKET,
                success=True,
                complete=True,
                observations=list(snapshot.observations.values()),
                endpoint=endpoint,
                http_status=304,
                etag=response.header("ETag") or snapshot.etag,
                last_modified=(
                    response.header("Last-Modified") or snapshot.last_modified
                ),
                records_received=len(snapshot.observations),
                not_modified=True,
            )
        if not 200 <= response.status < 300:
            return _failure(
                endpoint,
                response,
                "http_error",
                f"HTTP {response.status}",
            )
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return _failure(
                endpoint, response, "invalid_json", "response was not valid JSON"
            )
        return self._parse(payload, endpoint, response, sparse=sparse)

    def _parse(
        self,
        payload: Any,
        endpoint: str,
        response: HttpResponse,
        *,
        sparse: bool,
    ) -> FetchResult:
        if type(payload) is not dict or not payload:
            return _failure(
                endpoint,
                response,
                "invalid_market_shape",
                "market payload was not a non-empty mapping",
            )
        observed_at = self.clock()
        observations: list[SourceObservation] = []
        rejected = 0
        for slug, item in payload.items():
            if type(slug) is not str or type(item) is not dict:
                rejected += 1
                continue
            observation = _market_observation(
                slug,
                item,
                endpoint,
                observed_at=observed_at,
                sparse=sparse,
            )
            if observation is None:
                rejected += 1
            else:
                observations.append(observation)
        received = len(payload)
        complete = bool(observations) and rejected / received <= 0.05
        return FetchResult(
            source_kind=SourceKind.MARKET,
            success=bool(observations),
            complete=complete,
            observations=observations,
            endpoint=endpoint,
            http_status=response.status,
            etag=response.header("ETag"),
            last_modified=response.header("Last-Modified"),
            pages_fetched=1,
            records_received=received,
            records_rejected=rejected,
            error_code=None if complete else "incomplete_parse",
            error_summary=None
            if complete
            else "market parse rejection threshold exceeded",
        )


def _market_observation(
    slug: str,
    item: dict[str, Any],
    endpoint: str,
    *,
    observed_at: str,
    sparse: bool,
) -> SourceObservation | None:
    repo_value = _first(item, "repo", "repo_url", "repository", "url")
    normalized = normalize_github_repo(repo_value)
    canonical_id = (
        normalized[0] if normalized else fallback_canonical_id(SourceKind.MARKET, slug)
    )
    repo_url = normalized[1] if normalized else None
    owner, repo_name = repo_parts(repo_url)
    name = sanitize_text(_first(item, "name", "plugin_name") or slug, 256)
    if not name:
        return None
    fields = {
        "name": name,
        "display_name": sanitize_text(_first(item, "display_name", "title"), 256),
        "description": sanitize_text(_first(item, "desc", "description"), 4096),
        "author": sanitize_text(item.get("author"), 256),
        "version": sanitize_text(item.get("version"), 128),
        "repo_url": repo_url,
        "astrbot_version": sanitize_text(item.get("astrbot_version"), 256),
        "platforms": _string_tuple(_first(item, "support_platforms", "platforms")),
        "tags": _string_tuple(item.get("tags")),
        "market_status": sanitize_text(item.get("status"), 128),
        "issue_state": None,
        "issue_labels": (),
        "archived": _optional_bool(item.get("archived")),
    }
    excerpt_keys = {
        "author",
        "astrbot_version",
        "desc",
        "display_name",
        "repo",
        "status",
        "support_platforms",
        "tags",
        "updated_at",
        "version",
    }
    return SourceObservation(
        source_kind=SourceKind.MARKET,
        source_record_id=slug,
        source_url=repo_url or endpoint,
        observed_at=observed_at,
        fetched_from=endpoint,
        canonical_id=canonical_id,
        repo_url=repo_url,
        repo_owner=owner,
        repo_name=repo_name,
        name=name,
        display_name=fields["display_name"],
        description=fields["description"],
        author=fields["author"],
        version=fields["version"],
        astrbot_version=fields["astrbot_version"],
        platforms=fields["platforms"],
        tags=fields["tags"],
        market_status=fields["market_status"],
        stars=_optional_int(item.get("stars")),
        forks=_optional_int(item.get("forks")),
        archived=fields["archived"],
        repo_updated_at=sanitize_text(item.get("updated_at"), 128),
        observation_hash=observation_hash(
            **fields,
            stars=_optional_int(item.get("stars")),
            forks=_optional_int(item.get("forks")),
            repo_updated_at=sanitize_text(item.get("updated_at"), 128),
        ),
        sparse=sparse,
        raw_excerpt=bounded_excerpt(
            {key: item[key] for key in sorted(item) if key in excerpt_keys}
        ),
    )


def _conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def _failure(
    endpoint: str,
    response: HttpResponse | None,
    code: str,
    summary: str,
) -> FetchResult:
    return FetchResult(
        source_kind=SourceKind.MARKET,
        success=False,
        complete=False,
        endpoint=endpoint,
        http_status=response.status if response else None,
        etag=response.header("ETag") if response else None,
        last_modified=response.header("Last-Modified") if response else None,
        error_code=code,
        error_summary=summary,
    )


def _first(item: dict[str, Any], *keys: str) -> Any:
    return next((item[key] for key in keys if item.get(key) not in (None, "")), None)


def _string_tuple(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    cleaned = {text for item in values if (text := sanitize_text(item, 128))}
    return tuple(sorted(cleaned, key=str.casefold))


def _optional_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None
