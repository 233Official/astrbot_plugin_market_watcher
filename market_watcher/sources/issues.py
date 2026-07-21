"""Bounded GitHub Issues adapters for Collection and legacy publication data."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

from ..errors import classify_exception
from ..http import HttpClient, HttpResponse
from ..models import FetchResult, SourceKind, SourceObservation, SourceState
from ..normalize import (
    bounded_excerpt,
    extract_github_url,
    extract_json_code_block,
    fallback_canonical_id,
    normalize_github_repo,
    observation_hash,
    repo_parts,
    sanitize_text,
    utc_now,
)

COLLECTION_ISSUES_URL = (
    "https://api.github.com/repos/AstrBotDevs/"
    "AstrBot_Plugins_Collection/issues?state=all&per_page=100&page=1"
)
LEGACY_ISSUES_URL = (
    "https://api.github.com/repos/AstrBotDevs/AstrBot/issues"
    "?state=all&labels=plugin-publish&per_page=100&page=1"
)
MAX_ISSUE_PAGES = 20
MAX_REJECTED_RATIO = 0.05
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')


class IssuesFetcher:
    def __init__(
        self,
        http: HttpClient,
        *,
        source_kind: SourceKind,
        endpoint: str | None = None,
        clock: Callable[[], str] = utc_now,
    ) -> None:
        if source_kind not in {
            SourceKind.COLLECTION_ISSUE,
            SourceKind.LEGACY_PUBLISH_ISSUE,
        }:
            raise ValueError("IssuesFetcher requires an Issue source kind")
        self.http = http
        self.source_kind = source_kind
        self.endpoint = endpoint or (
            COLLECTION_ISSUES_URL
            if source_kind is SourceKind.COLLECTION_ISSUE
            else LEGACY_ISSUES_URL
        )
        self.clock = clock
        parsed = urlsplit(self.endpoint)
        self._expected_path = parsed.path
        self._expected_query = {
            key: value for key, value in parse_qs(parsed.query).items() if key != "page"
        }

    async def fetch(self, previous: SourceState | None = None) -> FetchResult:
        snapshot = previous.snapshot_for(self.endpoint) if previous else None
        conditional_snapshot = (
            snapshot if snapshot and snapshot.pages_fetched == 1 else None
        )
        url: str | None = self.endpoint
        observations: dict[str, SourceObservation] = {}
        received = rejected = candidates = pages = 0
        unsafe_duplicate = False
        visited: set[str] = set()
        first_response: HttpResponse | None = None
        current_page = _page_number(self.endpoint) or 1
        while url is not None:
            if pages >= MAX_ISSUE_PAGES or url in visited:
                return self._incomplete(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "unsafe_pagination",
                    "pagination loop or page limit reached",
                )
            visited.add(url)
            headers = (
                {"If-None-Match": conditional_snapshot.etag}
                if conditional_snapshot and conditional_snapshot.etag and pages == 0
                else {}
            )
            try:
                response = await self.http.get(url, headers=headers)
            except Exception as exc:
                code, summary = classify_exception(exc)
                return self._incomplete(
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
            if response.status == 304 and pages == 0:
                if conditional_snapshot is None:
                    return self._incomplete(
                        {},
                        0,
                        0,
                        0,
                        response,
                        "not_modified_without_endpoint_snapshot",
                        "304 had no matching endpoint snapshot",
                    )
                return FetchResult(
                    source_kind=self.source_kind,
                    success=True,
                    complete=True,
                    observations=list(conditional_snapshot.observations.values()),
                    endpoint=self.endpoint,
                    http_status=304,
                    etag=response.header("ETag") or conditional_snapshot.etag,
                    last_modified=response.header("Last-Modified")
                    or conditional_snapshot.last_modified,
                    records_received=len(conditional_snapshot.observations),
                    not_modified=True,
                )
            if not 200 <= response.status < 300:
                return self._incomplete(
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
                return self._incomplete(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "invalid_json",
                    "response was not valid JSON",
                )
            if type(payload) is not list:
                return self._incomplete(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "invalid_issue_shape",
                    "Issue payload was not an array",
                )
            pages += 1
            received += len(payload)
            observed_at = self.clock()
            for issue in payload:
                if type(issue) is dict and "pull_request" in issue:
                    continue
                candidates += 1
                observation = (
                    _issue_observation(
                        issue,
                        self.source_kind,
                        self.endpoint,
                        observed_at=observed_at,
                    )
                    if type(issue) is dict
                    else None
                )
                if observation is None:
                    rejected += 1
                    continue
                old = observations.get(observation.source_record_id)
                if old is None:
                    observations[observation.source_record_id] = observation
                else:
                    selected = _select_duplicate(old, observation)
                    if selected is None:
                        unsafe_duplicate = True
                    else:
                        observations[observation.source_record_id] = selected
            next_url = _next_url(response.header("Link"))
            if next_url is None:
                url = None
                continue
            next_page = _page_number(next_url)
            if not _safe_next_url(
                next_url,
                expected_path=self._expected_path,
                expected_query=self._expected_query,
                current_page=current_page,
                visited=visited,
            ):
                return self._incomplete(
                    observations,
                    received,
                    rejected,
                    pages,
                    first_response,
                    "unsafe_pagination",
                    "next Link was outside the expected Issue listing",
                )
            current_page = next_page or current_page
            url = next_url

        ratio_ok = candidates == 0 or rejected / candidates <= MAX_REJECTED_RATIO
        complete = ratio_ok and not unsafe_duplicate
        return FetchResult(
            source_kind=self.source_kind,
            success=True,
            complete=complete,
            observations=list(observations.values()),
            endpoint=self.endpoint,
            http_status=first_response.status if first_response else None,
            etag=first_response.header("ETag") if first_response else None,
            last_modified=first_response.header("Last-Modified")
            if first_response
            else None,
            pages_fetched=pages,
            records_received=received,
            records_rejected=rejected,
            error_code=None if complete else "incomplete_parse",
            error_summary=None
            if complete
            else "Issue parse or duplicate safety threshold failed",
        )

    def _incomplete(
        self,
        observations: dict[str, SourceObservation],
        received: int,
        rejected: int,
        pages: int,
        response: HttpResponse | None,
        code: str,
        summary: str,
    ) -> FetchResult:
        return FetchResult(
            source_kind=self.source_kind,
            success=pages > 0,
            complete=False,
            observations=list(observations.values()),
            endpoint=self.endpoint,
            http_status=response.status if response else None,
            etag=response.header("ETag") if response else None,
            last_modified=response.header("Last-Modified") if response else None,
            pages_fetched=pages,
            records_received=received,
            records_rejected=rejected,
            error_code=code,
            error_summary=summary,
        )


def _issue_observation(
    issue: dict[str, Any],
    source_kind: SourceKind,
    endpoint: str,
    *,
    observed_at: str,
) -> SourceObservation | None:
    number = issue.get("number")
    if type(number) is not int:
        return None
    body = sanitize_text(issue.get("body"), 65536)
    info = extract_json_code_block(body) or {}
    repo_value = _first(
        info, "repo", "repo_url", "repository", "plugin_repo", "plugin_url"
    ) or extract_github_url(body)
    normalized = normalize_github_repo(repo_value)
    title = sanitize_text(issue.get("title"), 512)
    labels = _labels(issue.get("labels"))
    if source_kind is SourceKind.LEGACY_PUBLISH_ISSUE:
        has_label = "plugin-publish" in {label.casefold() for label in labels}
        historical_template = bool(
            title
            and re.match(r"^\s*\[plugin\]", title, re.I)
            and normalized is not None
        )
        if not has_label and not historical_template:
            return None
    canonical_id = (
        normalized[0] if normalized else fallback_canonical_id(source_kind, str(number))
    )
    repo_url = normalized[1] if normalized else None
    owner, repo_name = repo_parts(repo_url)
    name = sanitize_text(_first(info, "name", "plugin_name"), 256)
    if not name and title:
        name = re.sub(r"^\s*\[plugin\]\s*", "", title, flags=re.I).strip() or None
    fields = {
        "name": name,
        "display_name": sanitize_text(_first(info, "display_name", "title"), 256),
        "description": sanitize_text(_first(info, "desc", "description"), 4096),
        "author": sanitize_text(_first(info, "author", "plugin_author"), 256),
        "version": sanitize_text(info.get("version"), 128),
        "repo_url": repo_url,
        "astrbot_version": sanitize_text(info.get("astrbot_version"), 256),
        "platforms": _string_tuple(_first(info, "support_platforms", "platforms")),
        "tags": _string_tuple(info.get("tags")),
        "market_status": None,
        "issue_state": sanitize_text(issue.get("state"), 64),
        "issue_labels": labels,
        "archived": None,
    }
    source_url = sanitize_text(issue.get("html_url"), 2048)
    if not source_url:
        return None
    return SourceObservation(
        source_kind=source_kind,
        source_record_id=str(number),
        source_url=source_url,
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
        issue_state=fields["issue_state"],
        issue_labels=labels,
        repo_updated_at=sanitize_text(issue.get("updated_at"), 128),
        observation_hash=observation_hash(
            **fields,
            repo_updated_at=sanitize_text(issue.get("updated_at"), 128),
        ),
        raw_excerpt=bounded_excerpt(
            {
                "number": number,
                "state": issue.get("state"),
                "title": title,
                "updated_at": issue.get("updated_at"),
            }
        ),
    )


def _select_duplicate(
    old: SourceObservation, new: SourceObservation
) -> SourceObservation | None:
    if old.observation_hash == new.observation_hash:
        return new if (new.repo_updated_at or "") > (old.repo_updated_at or "") else old
    if (
        old.repo_updated_at
        and new.repo_updated_at
        and old.repo_updated_at != new.repo_updated_at
    ):
        return new if new.repo_updated_at > old.repo_updated_at else old
    return None


def _safe_next_url(
    value: str,
    *,
    expected_path: str,
    expected_query: dict[str, list[str]],
    current_page: int,
    visited: set[str],
) -> bool:
    parsed = urlsplit(value)
    page = _page_number(value)
    query = parse_qs(parsed.query)
    fixed_query = {key: item for key, item in query.items() if key != "page"}
    return (
        parsed.scheme == "https"
        and parsed.hostname == "api.github.com"
        and parsed.username is None
        and parsed.password is None
        and parsed.path == expected_path
        and fixed_query == expected_query
        and value not in visited
        and page is not None
        and page > current_page
        and page <= MAX_ISSUE_PAGES
    )


def _page_number(value: str) -> int | None:
    values = parse_qs(urlsplit(value).query).get("page")
    if not values or len(values) != 1:
        return None
    try:
        page = int(values[0])
    except ValueError:
        return None
    return page if page > 0 else None


def _next_url(link: str | None) -> str | None:
    if not link:
        return None
    match = _NEXT_LINK.search(link)
    return match.group(1) if match else None


def _labels(value: Any) -> tuple[str, ...]:
    if type(value) is not list:
        return ()
    labels = {
        text
        for label in value
        if type(label) is dict and (text := sanitize_text(label.get("name"), 128))
    }
    return tuple(sorted(labels, key=str.casefold))


def _first(item: dict[str, Any], *keys: str) -> Any:
    return next((item[key] for key in keys if item.get(key) not in (None, "")), None)


def _string_tuple(value: Any) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple, set)) else [value]
    cleaned = {text for item in values if (text := sanitize_text(item, 128))}
    return tuple(sorted(cleaned, key=str.casefold))
