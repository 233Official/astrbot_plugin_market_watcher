"""Bounded low-priority GitHub repository search adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..errors import classify_exception
from ..http import HttpClient, HttpResponse
from ..models import FetchResult, SourceKind, SourceObservation, SourceState
from ..normalize import (
    bounded_excerpt,
    normalize_github_repo,
    observation_hash,
    repo_parts,
    sanitize_text,
    utc_now,
)

SEARCH_URL = (
    "https://api.github.com/search/repositories"
    "?q=astrbot_plugin_+in:name&sort=updated&order=desc&per_page=100&page={page}"
)
SEARCH_ENDPOINT = SEARCH_URL.format(page=1)
MAX_SEARCH_PAGES = 2


class GitHubSearchFetcher:
    def __init__(
        self,
        http: HttpClient,
        *,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        self.http = http
        self.clock = clock

    async def fetch(self, previous: SourceState | None = None) -> FetchResult:
        snapshot = previous.snapshot_for(SEARCH_ENDPOINT) if previous else None
        conditional_snapshot = (
            snapshot if snapshot and snapshot.pages_fetched == 1 else None
        )
        observations: dict[str, SourceObservation] = {}
        received = rejected = pages = 0
        declared_total: int | None = None
        complete = True
        first_response: HttpResponse | None = None
        for page in range(1, MAX_SEARCH_PAGES + 1):
            endpoint = SEARCH_URL.format(page=page)
            headers = (
                {"If-None-Match": conditional_snapshot.etag}
                if conditional_snapshot and conditional_snapshot.etag and page == 1
                else {}
            )
            try:
                response = await self.http.get(endpoint, headers=headers)
            except Exception as exc:
                code, summary = classify_exception(exc)
                return _failure(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    code,
                    summary,
                )
            if first_response is None:
                first_response = response
            if response.status == 304 and page == 1:
                if conditional_snapshot is None:
                    return _failure(
                        {},
                        0,
                        0,
                        0,
                        response,
                        "not_modified_without_endpoint_snapshot",
                        "304 had no matching endpoint snapshot",
                    )
                return FetchResult(
                    source_kind=SourceKind.GITHUB_DISCOVERY,
                    success=True,
                    complete=True,
                    observations=list(conditional_snapshot.observations.values()),
                    endpoint=SEARCH_ENDPOINT,
                    http_status=304,
                    etag=response.header("ETag") or conditional_snapshot.etag,
                    last_modified=response.header("Last-Modified")
                    or conditional_snapshot.last_modified,
                    records_received=len(conditional_snapshot.observations),
                    not_modified=True,
                )
            if not 200 <= response.status < 300:
                return _failure(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "http_error",
                    f"HTTP {response.status}",
                )
            try:
                payload = response.json()
            except (ValueError, TypeError):
                return _failure(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "invalid_json",
                    "response was not valid JSON",
                )
            if (
                type(payload) is not dict
                or type(payload.get("items")) is not list
                or type(payload.get("total_count")) is not int
                or type(payload.get("incomplete_results")) is not bool
            ):
                return _failure(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "invalid_search_shape",
                    "search response fields had invalid types",
                )
            total = payload["total_count"]
            if total < 0 or (declared_total is not None and total != declared_total):
                complete = False
            declared_total = total if declared_total is None else declared_total
            complete = complete and not payload["incomplete_results"]
            items = payload["items"]
            pages += 1
            received += len(items)
            observed_at = self.clock()
            for item in items:
                observation, malformed = _repository_observation(
                    item,
                    endpoint,
                    observed_at=observed_at,
                )
                if malformed:
                    complete = False
                if observation is None:
                    rejected += 1
                    continue
                old = observations.get(observation.source_record_id)
                if old:
                    complete = False
                    continue
                observations[observation.source_record_id] = observation
            if received >= total:
                break
            if len(items) < 100:
                complete = False
                break
        if declared_total is None or received < declared_total:
            complete = False
        if declared_total is not None and declared_total > MAX_SEARCH_PAGES * 100:
            complete = False
        return FetchResult(
            source_kind=SourceKind.GITHUB_DISCOVERY,
            success=True,
            complete=complete,
            observations=list(observations.values()),
            endpoint=SEARCH_ENDPOINT,
            http_status=first_response.status if first_response else None,
            etag=first_response.header("ETag") if first_response else None,
            last_modified=first_response.header("Last-Modified")
            if first_response
            else None,
            pages_fetched=pages,
            records_received=received,
            records_rejected=rejected,
            error_code=None if complete else "incomplete_search",
            error_summary=None
            if complete
            else "search results were incomplete or truncated",
        )


def _repository_observation(
    item: Any,
    endpoint: str,
    *,
    observed_at: str,
) -> tuple[SourceObservation | None, bool]:
    if type(item) is not dict:
        return None, True
    if type(item.get("name")) is not str:
        return None, True
    for key in ("private", "fork", "archived"):
        if key in item and type(item[key]) is not bool:
            return None, True
    if item.get("mirror_url") is not None and type(item.get("mirror_url")) is not str:
        return None, True
    name = sanitize_text(item["name"], 256)
    if (
        not name
        or not name.lower().startswith("astrbot_plugin_")
        or item.get("visibility") not in (None, "public")
        or bool(item.get("private"))
        or bool(item.get("fork"))
        or bool(item.get("archived"))
        or bool(item.get("mirror_url"))
    ):
        return None, False
    owner_data = item.get("owner")
    if type(owner_data) is not dict or type(owner_data.get("login")) is not str:
        return None, True
    if item.get("id") is not None and type(item.get("id")) is not int:
        return None, True
    if item.get("full_name") is not None and type(item.get("full_name")) is not str:
        return None, True
    for key in ("stargazers_count", "forks_count"):
        if item.get(key) is not None and type(item.get(key)) is not int:
            return None, True
    repo_value = item.get("html_url") or item.get("url")
    if type(repo_value) is not str:
        return None, True
    normalized = normalize_github_repo(repo_value)
    if normalized is None:
        return None, True
    identifier = item.get("id")
    if type(identifier) is int:
        source_record_id = str(identifier)
    else:
        full_name = sanitize_text(item.get("full_name"), 512)
        full_normalized = normalize_github_repo(
            f"https://github.com/{full_name}" if full_name else None
        )
        source_record_id = (
            f"full_name:{full_name.casefold()}"
            if full_normalized == normalized and full_name
            else f"canonical:{normalized[0]}"
        )
    owner, repo_name = repo_parts(normalized[1])
    fields = {
        "name": name,
        "display_name": name,
        "description": sanitize_text(item.get("description"), 4096),
        "author": sanitize_text(owner_data["login"], 256),
        "version": None,
        "repo_url": normalized[1],
        "astrbot_version": None,
        "platforms": (),
        "tags": (),
        "market_status": None,
        "issue_state": None,
        "issue_labels": (),
        "archived": False,
    }
    return SourceObservation(
        source_kind=SourceKind.GITHUB_DISCOVERY,
        source_record_id=source_record_id,
        source_url=normalized[1],
        observed_at=observed_at,
        fetched_from=endpoint,
        canonical_id=normalized[0],
        repo_url=normalized[1],
        repo_owner=owner,
        repo_name=repo_name,
        name=name,
        display_name=name,
        description=fields["description"],
        author=fields["author"],
        stars=_optional_int(item.get("stargazers_count")),
        forks=_optional_int(item.get("forks_count")),
        archived=False,
        repo_updated_at=sanitize_text(item.get("updated_at"), 128),
        observation_hash=observation_hash(
            **fields,
            stars=_optional_int(item.get("stargazers_count")),
            forks=_optional_int(item.get("forks_count")),
            repo_updated_at=sanitize_text(item.get("updated_at"), 128),
        ),
        raw_excerpt=bounded_excerpt(
            {
                "default_branch": item.get("default_branch"),
                "full_name": item.get("full_name"),
                "id": item.get("id"),
                "name": name,
                "updated_at": item.get("updated_at"),
            }
        ),
    ), False


def _failure(
    observations: dict[str, SourceObservation],
    received: int,
    rejected: int,
    pages: int,
    response: HttpResponse | None,
    code: str,
    summary: str,
) -> FetchResult:
    return FetchResult(
        source_kind=SourceKind.GITHUB_DISCOVERY,
        success=pages > 0,
        complete=False,
        observations=list(observations.values()),
        endpoint=SEARCH_ENDPOINT,
        http_status=response.status if response else None,
        etag=response.header("ETag") if response else None,
        last_modified=response.header("Last-Modified") if response else None,
        pages_fetched=pages,
        records_received=received,
        records_rejected=rejected,
        error_code=code,
        error_summary=summary,
    )


def _optional_int(value: Any) -> int | None:
    if type(value) is int:
        return value
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
