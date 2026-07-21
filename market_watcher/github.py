"""Shared GitHub request budget, rate-limit handling, and repository cache."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import classify_exception
from .http import GitHubAuthHttpClient, HttpClient, HttpResponse
from .models import GitHubRepoCache, PluginRecord, WatcherState

TOKEN_BUDGET = 20
ANONYMOUS_BUDGET = 5
TOKEN_TTL = timedelta(hours=6)
ANONYMOUS_TTL = timedelta(hours=24)
METADATA_PRIORITY_HEADER = "X-AstrBot-Metadata-Priority"
RATE_LIMIT_BODY_MAX_BYTES = 4096
SECONDARY_RATE_LIMIT_SIGNALS = (
    "secondary rate limit",
    "secondary-rate-limit",
    "abuse detection mechanism",
    "abuse detection",
)


class GitHubBudgetExceeded(RuntimeError):
    pass


class GitHubRateLimited(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubResponseClassification:
    status: str
    error_code: str | None = None


def classify_github_response(response: HttpResponse) -> GitHubResponseClassification:
    """Return the stable, secret-safe GitHub HTTP classification."""
    if 200 <= response.status < 400:
        return GitHubResponseClassification("ok")
    if response.status == 401:
        return GitHubResponseClassification("auth_failed", "github_auth_failed")
    if response.status == 403:
        remaining = _header_int(response, "X-RateLimit-Remaining")
        if _is_rate_limit_response(response, remaining):
            return GitHubResponseClassification("rate_limited", "github_rate_limited")
        return GitHubResponseClassification(
            "permission_denied", "github_permission_denied"
        )
    if response.status == 429:
        return GitHubResponseClassification("rate_limited", "github_rate_limited")
    return GitHubResponseClassification("http_error", "github_http_error")


class GitHubGateway:
    """Count every GitHub attempt while delegating retry sleeps to HTTP only."""

    def __init__(
        self,
        inner: HttpClient,
        auth: GitHubAuthHttpClient,
        *,
        concurrency: int = 2,
    ) -> None:
        self.inner = inner
        self.auth = auth
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.limit = ANONYMOUS_BUDGET
        self.remaining = ANONYMOUS_BUDGET
        self.used = 0
        self.blocked = False
        self.state: WatcherState | None = None
        self.request_priorities: list[int] = []
        self._budget_lock = asyncio.Lock()
        self.token_mode = False

    def begin_run(self, state: WatcherState) -> None:
        self.auth.reset()
        self.token_mode = self.auth.has_token
        self.limit = TOKEN_BUDGET if self.token_mode else ANONYMOUS_BUDGET
        self.remaining = self.limit
        self.used = 0
        self.blocked = False
        self.state = state
        self.request_priorities = []
        state.github.rate_limit.status = "unknown"
        state.github.rate_limit.error_code = None

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> HttpResponse:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            return await self.inner.get(url, headers=headers)
        request_headers = dict(headers or {})
        priority = _request_priority(url, request_headers)
        async with self.semaphore:
            async with self._budget_lock:
                if self.blocked:
                    raise GitHubRateLimited("GitHub requests blocked for this run")
                if self.remaining <= 0:
                    raise GitHubBudgetExceeded("GitHub request budget exhausted")
                self.remaining -= 1
                self.used += 1
                self.request_priorities.append(priority)
            response = await self.inner.get(url, headers=request_headers)
            async with self._budget_lock:
                self._record_response(response)
        return response

    async def close(self) -> None:
        await self.inner.close()

    def _record_response(self, response: HttpResponse) -> None:
        if self.state is None:
            return
        rate = self.state.github.rate_limit
        remaining = _header_int(response, "X-RateLimit-Remaining")
        reset = response.header("X-RateLimit-Reset")
        if remaining is not None:
            rate.remaining = remaining
        if reset:
            rate.reset_at = _epoch_to_iso(reset)
        if self.blocked:
            return
        classification = classify_github_response(response)
        if classification.status == "auth_failed":
            self.auth.disable()
            self.token_mode = False
            self.limit = ANONYMOUS_BUDGET
            self.remaining = min(self.remaining, max(0, ANONYMOUS_BUDGET - self.used))
            rate.status = classification.status
            rate.error_code = classification.error_code
        elif classification.status == "rate_limited":
            self.blocked = True
            rate.status = classification.status
            rate.error_code = classification.error_code
        elif classification.status == "permission_denied":
            rate.status = classification.status
            rate.error_code = classification.error_code
        elif classification.status == "ok" and rate.status != "auth_failed":
            rate.status = classification.status
            rate.error_code = classification.error_code


class GitHubMetadataService:
    def __init__(
        self,
        http: HttpClient,
        gateway: GitHubGateway,
        *,
        clock: Callable[[], str],
    ) -> None:
        self.http = http
        self.gateway = gateway
        self.clock = clock

    async def enrich(
        self,
        state: WatcherState,
        plugins: dict[str, PluginRecord],
        urgent_ids: set[str],
        *,
        include_star_count: bool,
    ) -> None:
        if not include_star_count:
            for plugin in plugins.values():
                plugin.stars = None
                plugin.forks = None
                plugin.github_metadata_status = "disabled"
                plugin.github_metadata_fetched_at = None
            return
        now = self.clock()
        urgent: list[str] = []
        normal: list[str] = []
        for canonical_id in sorted(plugins):
            if not canonical_id.startswith("github:"):
                continue
            cache = state.github.repos.get(canonical_id)
            if _fresh(cache, now, self.gateway.token_mode):
                continue
            if canonical_id in urgent_ids:
                urgent.append(canonical_id)
            else:
                normal.append(canonical_id)
        await self._refresh_group(state, urgent, priority=2)
        await self._refresh_group(state, normal, priority=3)
        self.apply_cache(state, plugins, include_star_count=True)

    def apply_cache(
        self,
        state: WatcherState,
        plugins: dict[str, PluginRecord],
        *,
        include_star_count: bool,
    ) -> None:
        if not include_star_count:
            for plugin in plugins.values():
                plugin.stars = None
                plugin.forks = None
                plugin.github_metadata_status = "disabled"
                plugin.github_metadata_fetched_at = None
            return
        for canonical_id, plugin in plugins.items():
            cache = state.github.repos.get(canonical_id)
            if cache is None:
                plugin.github_metadata_status = "unknown"
                continue
            if cache.stars is not None:
                plugin.stars = cache.stars
            if cache.forks is not None:
                plugin.forks = cache.forks
            if cache.archived is not None:
                plugin.archived = cache.archived
            if cache.repo_updated_at is not None:
                plugin.repo_updated_at = cache.repo_updated_at
            plugin.github_metadata_fetched_at = cache.fetched_at
            plugin.github_metadata_status = cache.status

    async def _refresh_group(
        self, state: WatcherState, canonical_ids: list[str], *, priority: int
    ) -> None:
        await asyncio.gather(
            *(self._refresh_one(state, item, priority) for item in canonical_ids)
        )

    async def _refresh_one(
        self, state: WatcherState, canonical_id: str, priority: int
    ) -> None:
        cache = state.github.repos.get(canonical_id) or GitHubRepoCache(canonical_id)
        state.github.repos[canonical_id] = cache
        repo = canonical_id.removeprefix("github:")
        headers = {METADATA_PRIORITY_HEADER: str(priority)}
        if cache.etag:
            headers["If-None-Match"] = cache.etag
        try:
            response = await self.http.get(
                f"https://api.github.com/repos/{repo}", headers=headers
            )
        except Exception as exc:
            code, _ = classify_exception(exc)
            cache.status = "stale" if cache.fetched_at else "failed"
            cache.error_code = code
            return
        now = self.clock()
        if response.status == 304:
            cache.fetched_at = now
            cache.status = "fresh"
            cache.error_code = None
            cache.etag = response.header("ETag") or cache.etag
            return
        if response.status == 200:
            try:
                payload = response.json()
            except (TypeError, ValueError):
                payload = None
            if not _valid_repo_payload(payload):
                cache.status = "stale" if cache.fetched_at else "failed"
                cache.error_code = "github_invalid_metadata"
                return
            cache.etag = response.header("ETag") or cache.etag
            cache.stars = payload.get("stargazers_count")
            cache.forks = payload.get("forks_count")
            cache.archived = payload.get("archived")
            cache.repo_updated_at = payload.get("updated_at")
            cache.fetched_at = now
            cache.status = "fresh"
            cache.error_code = None
            return
        mapping = {
            401: "github_auth_failed",
            404: "github_inaccessible",
            429: "github_rate_limited",
        }
        if response.status == 403:
            code = (
                "github_rate_limited"
                if _is_rate_limit_response(
                    response, _header_int(response, "X-RateLimit-Remaining")
                )
                else "github_permission_denied"
            )
        else:
            code = mapping.get(response.status, "github_http_error")
        cache.status = "inaccessible" if response.status == 404 else "stale"
        cache.error_code = code


def _request_priority(url: str, headers: dict[str, str]) -> int:
    explicit = headers.pop(METADATA_PRIORITY_HEADER, None)
    if explicit is not None:
        try:
            return int(explicit)
        except ValueError:
            return 3
    parsed = urlsplit(url)
    if parsed.path.endswith("/AstrBot_Plugins_Collection/issues"):
        return 0
    if parsed.path.endswith("/AstrBot/issues"):
        return 1
    if parsed.path == "/search/repositories":
        page = parse_qs(parsed.query).get("page", ["1"])[0]
        return 5 if page != "1" else 4
    return 3


def _fresh(cache: GitHubRepoCache | None, now: str, token_mode: bool) -> bool:
    if cache is None or not cache.fetched_at:
        return False
    try:
        fetched = datetime.fromisoformat(cache.fetched_at.replace("Z", "+00:00"))
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return False
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current - fetched < (TOKEN_TTL if token_mode else ANONYMOUS_TTL)


def _valid_repo_payload(value: Any) -> bool:
    if type(value) is not dict:
        return False
    return (
        (
            value.get("stargazers_count") is None
            or type(value.get("stargazers_count")) is int
        )
        and (value.get("forks_count") is None or type(value.get("forks_count")) is int)
        and (value.get("archived") is None or type(value.get("archived")) is bool)
        and (value.get("updated_at") is None or type(value.get("updated_at")) is str)
    )


def _header_int(response: HttpResponse, name: str) -> int | None:
    value = response.header(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _is_rate_limit_response(response: HttpResponse, remaining: int | None) -> bool:
    if remaining == 0 or response.header("Retry-After") is not None:
        return True
    text = _safe_response_excerpt(response.body).lower()
    return any(signal in text for signal in SECONDARY_RATE_LIMIT_SIGNALS)


def _safe_response_excerpt(body: bytes) -> str:
    text = body[:RATE_LIMIT_BODY_MAX_BYTES].decode("utf-8", errors="replace")
    text = re.sub(r"(?i)bearer\s+[^\s\"']+", "Bearer [redacted]", text)
    return re.sub(r"(?i)github_(?:pat|oauth)_[a-z0-9_-]+", "[redacted]", text)


def _epoch_to_iso(value: str) -> str | None:
    try:
        return (
            datetime.fromtimestamp(int(value), timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (ValueError, OverflowError, OSError):
        return None
