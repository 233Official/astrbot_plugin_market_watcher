from __future__ import annotations

import asyncio
import json
import time
import unittest
from copy import deepcopy

from market_watcher.config import parse_runtime_config
from market_watcher.detect import detect_changes
from market_watcher.github import (
    METADATA_PRIORITY_HEADER,
    GitHubBudgetExceeded,
    GitHubGateway,
    GitHubMetadataService,
    GitHubRateLimited,
)
from market_watcher.http import GitHubAuthHttpClient, HttpResponse, RetryingHttpClient
from market_watcher.merge import merge_sources
from market_watcher.models import (
    EndpointSnapshot,
    FetchResult,
    GitHubRepoCache,
    PluginRecord,
    RunReport,
    SourceKind,
    SourceObservation,
    SourceState,
    WatcherState,
)
from market_watcher.outbox import validate_targets
from market_watcher.scheduler import FixedDelayScheduler
from market_watcher.service import MarketWatcherService
from market_watcher.sources.github_search import SEARCH_ENDPOINT, GitHubSearchFetcher
from market_watcher.sources.issues import COLLECTION_ISSUES_URL, IssuesFetcher
from market_watcher.status import format_status

NOW = "2026-07-20T12:00:00Z"
LATER = "2026-07-21T13:00:00Z"


class QueueHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.active = 0
        self.max_active = 0
        self.block = False

    async def get(self, url, *, headers=None):
        self.calls.append((url, dict(headers or {})))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.block:
                await asyncio.sleep(0.01)
            value = self.responses.pop(0) if self.responses else HttpResponse(200)
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self.active -= 1

    async def close(self):
        return None


def response(status: int, payload=None, headers=None) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode()
    return HttpResponse(status, body=body, headers=headers or {})


def repo_payload(stars=12):
    return {
        "stargazers_count": stars,
        "forks_count": 3,
        "archived": False,
        "updated_at": "2026-07-20T11:00:00Z",
    }


class MemoryStore:
    def __init__(self, state=None):
        self.state = deepcopy(state or WatcherState())

    def load(self):
        return deepcopy(self.state)

    def save(self, state):
        self.state = deepcopy(state)


class OneFetcher:
    def __init__(self, result):
        self.result = result

    async def fetch(self, previous=None):
        del previous
        return self.result


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    async def send(self, target, message):
        self.calls.append((target, message))
        return True, None


def market_observation(version: str) -> SourceObservation:
    return SourceObservation(
        source_kind=SourceKind.MARKET,
        source_record_id="demo",
        source_url="https://github.com/a/b",
        observed_at=NOW,
        fetched_from="https://market.invalid",
        canonical_id="github:a/b",
        repo_url="https://github.com/a/b",
        repo_owner="a",
        repo_name="b",
        name="b",
        display_name="B",
        version=version,
        observation_hash=f"hash:{version}",
    )


class GitHubGatewayTests(unittest.IsolatedAsyncioTestCase):
    def make_gateway(self, inner, token="token"):
        auth = GitHubAuthHttpClient(inner, token)
        gateway = GitHubGateway(auth, auth)
        state = WatcherState()
        gateway.begin_run(state)
        return gateway, state

    async def test_token_and_anonymous_budgets(self) -> None:
        token_gateway, _ = self.make_gateway(QueueHttp())
        for index in range(20):
            await token_gateway.get(f"https://api.github.com/repos/a/{index}")
        with self.assertRaises(GitHubBudgetExceeded):
            await token_gateway.get("https://api.github.com/repos/a/overflow")
        self.assertEqual(token_gateway.used, 20)

        anonymous_gateway, _ = self.make_gateway(QueueHttp(), token="")
        for index in range(5):
            await anonymous_gateway.get(f"https://api.github.com/repos/a/{index}")
        with self.assertRaises(GitHubBudgetExceeded):
            await anonymous_gateway.get("https://api.github.com/repos/a/overflow")

    async def test_concurrency_is_bounded_to_two(self) -> None:
        inner = QueueHttp()
        inner.block = True
        gateway, _ = self.make_gateway(inner)
        await asyncio.gather(
            *(
                gateway.get(f"https://api.github.com/repos/a/{index}")
                for index in range(8)
            )
        )
        self.assertEqual(inner.max_active, 2)

    async def test_queued_requests_do_not_start_after_secondary_limit(self) -> None:
        class ControlledHttp:
            def __init__(self):
                self.calls = []
                self.two_started = asyncio.Event()
                self.release_rate_limit = asyncio.Event()
                self.release_second = asyncio.Event()

            async def get(self, url, *, headers=None):
                self.calls.append((url, dict(headers or {})))
                position = len(self.calls)
                if position == 2:
                    self.two_started.set()
                if position == 1:
                    await self.release_rate_limit.wait()
                    return response(
                        403,
                        {"message": "secondary rate limit"},
                        headers={"X-RateLimit-Remaining": "42"},
                    )
                await self.release_second.wait()
                return response(200)

            async def close(self):
                return None

        inner = ControlledHttp()
        gateway, _ = self.make_gateway(inner, token="")
        tasks = [
            asyncio.create_task(gateway.get(f"https://api.github.com/repos/a/{index}"))
            for index in range(6)
        ]
        await asyncio.wait_for(inner.two_started.wait(), 1)
        self.assertEqual(len(inner.calls), 2)
        self.assertEqual(gateway.used, 2)
        self.assertEqual(gateway.remaining, 3)
        inner.release_rate_limit.set()
        await tasks[0]
        await asyncio.sleep(0)
        self.assertTrue(gateway.blocked)
        self.assertEqual(len(inner.calls), 2)
        self.assertEqual(gateway.used, 2)
        self.assertEqual(gateway.remaining, 3)
        inner.release_second.set()
        results = await asyncio.gather(*tasks[1:], return_exceptions=True)
        self.assertEqual(len(inner.calls), 2)
        self.assertEqual(
            sum(isinstance(item, GitHubRateLimited) for item in results), 4
        )
        self.assertEqual(gateway.state.github.rate_limit.status, "rate_limited")

    async def test_priorities_are_classified(self) -> None:
        gateway, _ = self.make_gateway(QueueHttp())
        urls_and_headers = [
            (
                "https://api.github.com/repos/AstrBotDevs/AstrBot_Plugins_Collection/issues?page=1",
                {},
            ),
            ("https://api.github.com/repos/AstrBotDevs/AstrBot/issues?page=1", {}),
            ("https://api.github.com/repos/a/b", {METADATA_PRIORITY_HEADER: "2"}),
            ("https://api.github.com/repos/c/d", {METADATA_PRIORITY_HEADER: "3"}),
            ("https://api.github.com/search/repositories?page=1", {}),
            ("https://api.github.com/search/repositories?page=2", {}),
        ]
        for url, headers in urls_and_headers:
            await gateway.get(url, headers=headers)
        self.assertEqual(gateway.request_priorities, [0, 1, 2, 3, 4, 5])

    async def test_issue_pagination_consumes_shared_budget_per_page(self) -> None:
        page_two = COLLECTION_ISSUES_URL.replace("&page=1", "&page=2")
        inner = QueueHttp(
            response(200, [], headers={"Link": f'<{page_two}>; rel="next"'}),
            response(200, []),
        )
        gateway, _ = self.make_gateway(inner)
        result = await IssuesFetcher(
            gateway,
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        self.assertTrue(result.complete, result.error_code)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(gateway.used, 2)
        self.assertEqual(gateway.request_priorities, [0, 0])

    async def test_401_disables_token_without_leaking_it(self) -> None:
        inner = QueueHttp(response(401), response(200))
        gateway, state = self.make_gateway(inner)
        await gateway.get("https://api.github.com/repos/a/one")
        await gateway.get("https://api.github.com/repos/a/two")
        self.assertIn("Authorization", inner.calls[0][1])
        self.assertNotIn("Authorization", inner.calls[1][1])
        self.assertEqual(state.github.rate_limit.error_code, "github_auth_failed")
        self.assertLessEqual(gateway.remaining, 3)

    async def test_403_rate_limit_and_429_block_followups_without_sleep(self) -> None:
        gateway, state = self.make_gateway(
            QueueHttp(
                response(
                    403,
                    headers={
                        "X-RateLimit-Remaining": "0",
                        "X-RateLimit-Reset": "1784552400",
                    },
                )
            )
        )
        await gateway.get("https://api.github.com/repos/a/one")
        self.assertEqual(state.github.rate_limit.status, "rate_limited")
        self.assertIsNotNone(state.github.rate_limit.reset_at)
        with self.assertRaises(GitHubRateLimited):
            await gateway.get("https://api.github.com/repos/a/two")

    async def test_secondary_403_signals_block_but_permission_403_does_not(
        self,
    ) -> None:
        secret = "github_pat_secret-value"
        gateway, state = self.make_gateway(
            QueueHttp(
                response(
                    403,
                    {"message": "Please retry later"},
                    headers={
                        "X-RateLimit-Remaining": "42",
                        "Retry-After": "60",
                    },
                )
            )
        )
        await gateway.get("https://api.github.com/repos/a/one")
        self.assertTrue(gateway.blocked)
        self.assertEqual(state.github.rate_limit.error_code, "github_rate_limited")
        self.assertNotIn(secret, repr(state.github.rate_limit))
        with self.assertRaises(GitHubRateLimited):
            await gateway.get("https://api.github.com/repos/a/two")

        gateway, state = self.make_gateway(
            QueueHttp(
                response(
                    403,
                    {
                        "message": "You have exceeded a secondary rate limit. "
                        f"Authorization: Bearer {secret}"
                    },
                    headers={"X-RateLimit-Remaining": "42"},
                )
            )
        )
        await gateway.get("https://api.github.com/repos/a/body-signal")
        self.assertTrue(gateway.blocked)
        self.assertEqual(state.github.rate_limit.error_code, "github_rate_limited")
        self.assertNotIn(secret, repr(state.github.rate_limit))

        gateway, state = self.make_gateway(
            QueueHttp(
                response(
                    403,
                    {"message": "Resource not accessible by integration"},
                    headers={"X-RateLimit-Remaining": "42"},
                )
            )
        )
        await gateway.get("https://api.github.com/repos/a/private")
        self.assertFalse(gateway.blocked)
        self.assertEqual(state.github.rate_limit.error_code, "github_permission_denied")

    async def test_http_retry_owns_sleep_and_gateway_counts_once(self) -> None:
        inner = QueueHttp(
            response(429, headers={"Retry-After": "1"}),
            response(500),
            response(200),
        )
        auth = GitHubAuthHttpClient(inner, "token")
        sleeps = []

        async def sleep(delay):
            sleeps.append(delay)

        retry = RetryingHttpClient(auth, sleep=sleep, jitter=lambda delay: 0)
        gateway = GitHubGateway(retry, auth)
        gateway.begin_run(WatcherState())
        result = await gateway.get("https://api.github.com/repos/a/b")
        self.assertEqual(result.status, 200)
        self.assertEqual(gateway.used, 1)
        self.assertEqual(sleeps, [1.0, 2])

        gateway, state = self.make_gateway(QueueHttp(response(403)))
        await gateway.get("https://api.github.com/repos/a/private")
        self.assertFalse(gateway.blocked)
        self.assertEqual(state.github.rate_limit.status, "permission_denied")

        gateway, state = self.make_gateway(QueueHttp(response(429)))
        await gateway.get("https://api.github.com/repos/a/one")
        self.assertEqual(state.github.rate_limit.error_code, "github_rate_limited")
        with self.assertRaises(GitHubRateLimited):
            await gateway.get("https://api.github.com/repos/a/two")


class GitHubMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setup_service(self, inner, *, token="token", clock=lambda: NOW):
        auth = GitHubAuthHttpClient(inner, token)
        gateway = GitHubGateway(auth, auth)
        state = WatcherState()
        gateway.begin_run(state)
        service = GitHubMetadataService(gateway, gateway, clock=clock)
        return service, gateway, state

    async def test_ttl_304_and_stale_failure_preserve_values(self) -> None:
        inner = QueueHttp()
        service, _, state = self.setup_service(inner)
        state.github.repos["github:a/b"] = GitHubRepoCache(
            canonical_id="github:a/b",
            etag='"etag"',
            stars=9,
            fetched_at=NOW,
            status="fresh",
        )
        plugins = {"github:a/b": PluginRecord("github:a/b", "b")}
        await service.enrich(state, plugins, set(), include_star_count=True)
        self.assertEqual(inner.calls, [])
        self.assertEqual(plugins["github:a/b"].stars, 9)

        inner.responses.append(response(304, headers={"ETag": '"etag"'}))
        service.clock = lambda: LATER
        await service.enrich(state, plugins, set(), include_star_count=True)
        self.assertEqual(state.github.repos["github:a/b"].fetched_at, LATER)
        self.assertEqual(state.github.repos["github:a/b"].stars, 9)

        inner.responses.append(OSError("offline"))
        service.clock = lambda: "2026-07-22T14:00:00Z"
        await service.enrich(state, plugins, set(), include_star_count=True)
        cache = state.github.repos["github:a/b"]
        self.assertEqual(cache.stars, 9)
        self.assertEqual(cache.status, "stale")
        self.assertEqual(cache.fetched_at, LATER)

    async def test_token_ttl_is_six_hours_and_anonymous_ttl_is_twenty_four(
        self,
    ) -> None:
        seven_hours = "2026-07-20T19:00:00Z"
        token_inner = QueueHttp(response(200, repo_payload()))
        service, _, state = self.setup_service(
            token_inner, token="token", clock=lambda: seven_hours
        )
        state.github.repos["github:a/b"] = GitHubRepoCache(
            canonical_id="github:a/b", stars=1, fetched_at=NOW, status="fresh"
        )
        await service.enrich(
            state,
            {"github:a/b": PluginRecord("github:a/b", "b")},
            set(),
            include_star_count=True,
        )
        self.assertEqual(len(token_inner.calls), 1)

        anonymous_inner = QueueHttp(response(200, repo_payload()))
        service, _, state = self.setup_service(
            anonymous_inner, token="", clock=lambda: seven_hours
        )
        state.github.repos["github:a/b"] = GitHubRepoCache(
            canonical_id="github:a/b", stars=1, fetched_at=NOW, status="fresh"
        )
        await service.enrich(
            state,
            {"github:a/b": PluginRecord("github:a/b", "b")},
            set(),
            include_star_count=True,
        )
        self.assertEqual(anonymous_inner.calls, [])

    async def test_404_is_inaccessible_and_never_zero(self) -> None:
        service, _, state = self.setup_service(QueueHttp(response(404)))
        plugins = {"github:a/missing": PluginRecord("github:a/missing", "missing")}
        await service.enrich(state, plugins, set(), include_star_count=True)
        cache = state.github.repos["github:a/missing"]
        self.assertEqual(cache.status, "inaccessible")
        self.assertEqual(cache.error_code, "github_inaccessible")
        self.assertIsNone(cache.stars)

    async def test_include_star_false_makes_no_request_and_hides_star(self) -> None:
        inner = QueueHttp(response(200, repo_payload()))
        service, _, state = self.setup_service(inner)
        plugin = PluginRecord("github:a/b", "b", stars=99)
        await service.enrich(
            state,
            {plugin.canonical_id: plugin},
            {plugin.canonical_id},
            include_star_count=False,
        )
        self.assertEqual(inner.calls, [])
        self.assertIsNone(plugin.stars)
        self.assertEqual(plugin.github_metadata_status, "disabled")

    async def test_anonymous_budget_prioritizes_event_repo_over_cold_cache(
        self,
    ) -> None:
        event_id = "github:z/event"
        normal_ids = [f"github:a/cold-{index}" for index in range(8)]
        plugins = {
            canonical_id: PluginRecord(canonical_id, canonical_id.rsplit("/", 1)[-1])
            for canonical_id in [*normal_ids, event_id]
        }
        inner = QueueHttp(*(response(200, repo_payload()) for _ in range(5)))
        service, gateway, state = self.setup_service(inner, token="")
        await service.enrich(state, plugins, {event_id}, include_star_count=True)
        requested = [url for url, _ in inner.calls]
        self.assertEqual(len(requested), 5)
        self.assertTrue(requested[0].endswith("/repos/z/event"))
        self.assertEqual(gateway.request_priorities[0], 2)
        self.assertEqual(gateway.request_priorities[1:], [3, 3, 3, 3])

    async def test_metadata_star_change_does_not_create_event(self) -> None:
        old = PluginRecord("github:a/b", "b", stars=1)
        current = deepcopy(old)
        current.stars = 999
        self.assertEqual(
            detect_changes(
                {old.canonical_id: old},
                {current.canonical_id: current},
                detected_at=NOW,
            ),
            [],
        )

    def test_github_state_roundtrip_and_strict_validation(self) -> None:
        state = WatcherState()
        state.github.repos["github:a/b"] = GitHubRepoCache(
            canonical_id="github:a/b", stars=1, status="fresh", fetched_at=NOW
        )
        self.assertEqual(WatcherState.from_dict(state.to_dict()), state)
        invalid = state.to_dict()
        invalid["github"]["repos"]["github:a/b"]["extra"] = True
        with self.assertRaises(ValueError):
            WatcherState.from_dict(invalid)
        legacy = WatcherState().to_dict()
        legacy.pop("github")
        legacy["github_cache"] = {}
        self.assertEqual(WatcherState.from_dict(legacy), WatcherState())
        legacy["github_cache"] = {"unexpected": {}}
        with self.assertRaises(ValueError):
            WatcherState.from_dict(legacy)

    async def test_search_second_page_budget_failure_is_incomplete(self) -> None:
        first_page = {
            "total_count": 150,
            "incomplete_results": False,
            "items": [
                {
                    "id": index,
                    "name": f"astrbot_plugin_{index}",
                    "full_name": f"owner/astrbot_plugin_{index}",
                    "html_url": f"https://github.com/owner/astrbot_plugin_{index}",
                    "owner": {"login": "owner"},
                    "private": False,
                    "fork": False,
                    "archived": False,
                }
                for index in range(100)
            ],
        }
        service, gateway, _ = self.setup_service(QueueHttp(response(200, first_page)))
        del service
        gateway.remaining = 1
        result = await GitHubSearchFetcher(gateway, clock=lambda: NOW).fetch(
            SourceState()
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.error_code, "github_budget_exhausted")
        self.assertEqual(gateway.used, 1)

    async def test_budget_incomplete_search_does_not_overwrite_snapshot(self) -> None:
        old = SourceObservation(
            source_kind=SourceKind.GITHUB_DISCOVERY,
            source_record_id="old",
            source_url="https://github.com/owner/astrbot_plugin_old",
            observed_at=NOW,
            fetched_from=SEARCH_ENDPOINT,
            canonical_id="github:owner/astrbot_plugin_old",
            repo_url="https://github.com/owner/astrbot_plugin_old",
            repo_owner="owner",
            repo_name="astrbot_plugin_old",
            name="astrbot_plugin_old",
            observation_hash="old-hash",
        )
        old_source = SourceState(
            baseline_established=True,
            complete=True,
            observations={old.source_record_id: old},
            snapshots={
                SEARCH_ENDPOINT: EndpointSnapshot(
                    endpoint=SEARCH_ENDPOINT,
                    pages_fetched=1,
                    observations={old.source_record_id: old},
                )
            },
        )
        first_page = {
            "total_count": 150,
            "incomplete_results": False,
            "items": [
                {
                    "id": index,
                    "name": f"astrbot_plugin_{index}",
                    "full_name": f"owner/astrbot_plugin_{index}",
                    "html_url": f"https://github.com/owner/astrbot_plugin_{index}",
                    "owner": {"login": "owner"},
                    "private": False,
                    "fork": False,
                    "archived": False,
                }
                for index in range(100)
            ],
        }
        inner = QueueHttp(response(200, first_page))
        auth = GitHubAuthHttpClient(inner, "")

        class OneRequestGateway(GitHubGateway):
            def begin_run(self, state):
                super().begin_run(state)
                self.remaining = 1

        gateway = OneRequestGateway(auth, auth)
        state = WatcherState(
            sources={SourceKind.GITHUB_DISCOVERY.value: old_source},
            plugins=merge_sources({SourceKind.GITHUB_DISCOVERY.value: old_source}),
        )
        store = MemoryStore(state)
        service = MarketWatcherService(
            store=store,
            fetchers={
                SourceKind.GITHUB_DISCOVERY: GitHubSearchFetcher(
                    gateway, clock=lambda: NOW
                )
            },
            notifier=RecordingNotifier(),
            github_gateway=gateway,
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.GITHUB_DISCOVERY},
            push_targets=[],
            max_items_per_push=10,
        )
        preserved = store.state.sources[SourceKind.GITHUB_DISCOVERY.value]
        self.assertEqual(report.sources_failed, 1)
        self.assertEqual(set(preserved.observations), {"old"})
        self.assertEqual(set(preserved.snapshots), {SEARCH_ENDPOINT})

    async def test_updated_event_is_enriched_before_summary(self) -> None:
        old = market_observation("1.0.0")
        current = market_observation("2.0.0")
        old_source = SourceState(
            baseline_established=True,
            complete=True,
            observations={old.source_record_id: old},
        )
        state = WatcherState(
            sources={SourceKind.MARKET.value: old_source},
            plugins=merge_sources({SourceKind.MARKET.value: old_source}),
        )
        inner = QueueHttp(response(200, repo_payload(stars=42)))
        auth = GitHubAuthHttpClient(inner, "token")
        gateway = GitHubGateway(auth, auth)
        metadata = GitHubMetadataService(gateway, gateway, clock=lambda: NOW)
        notifier = RecordingNotifier()
        service = MarketWatcherService(
            store=MemoryStore(state),
            fetchers={
                SourceKind.MARKET: OneFetcher(
                    FetchResult(
                        SourceKind.MARKET,
                        True,
                        True,
                        observations=[current],
                        endpoint="https://market.invalid",
                        pages_fetched=1,
                    )
                )
            },
            notifier=notifier,
            github_gateway=gateway,
            github_metadata=metadata,
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["umo"],
            max_items_per_push=10,
        )
        self.assertEqual(report.updated, 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertIn("Star：42（缓存于", notifier.calls[0][1])

    async def test_silent_baselines_are_not_urgent_before_real_update(self) -> None:
        def observation(
            kind: SourceKind, record_id: str, canonical_id: str, version: str
        ) -> SourceObservation:
            owner, name = canonical_id.removeprefix("github:").split("/", 1)
            return SourceObservation(
                source_kind=kind,
                source_record_id=record_id,
                source_url=f"https://github.com/{owner}/{name}",
                observed_at=NOW,
                fetched_from=f"https://source.invalid/{kind.value}",
                canonical_id=canonical_id,
                repo_url=f"https://github.com/{owner}/{name}",
                repo_owner=owner,
                repo_name=name,
                name=name,
                version=version,
                observation_hash=f"hash:{kind.value}:{record_id}:{version}",
            )

        event_id = "github:z/updated"
        old = observation(SourceKind.MARKET, "updated", event_id, "1.0.0")
        current = observation(SourceKind.MARKET, "updated", event_id, "2.0.0")
        cold = [
            observation(
                SourceKind.COLLECTION_ISSUE,
                f"cold-{index}",
                f"github:a/cold-{index}",
                "1.0.0",
            )
            for index in range(8)
        ]
        old_market = SourceState(
            baseline_established=True,
            complete=True,
            observations={old.source_record_id: old},
        )
        state = WatcherState(
            sources={SourceKind.MARKET.value: old_market},
            plugins=merge_sources({SourceKind.MARKET.value: old_market}),
        )
        inner = QueueHttp(*(response(200, repo_payload()) for _ in range(5)))
        auth = GitHubAuthHttpClient(inner, "")
        gateway = GitHubGateway(auth, auth)
        service = MarketWatcherService(
            store=MemoryStore(state),
            fetchers={
                SourceKind.COLLECTION_ISSUE: OneFetcher(
                    FetchResult(
                        SourceKind.COLLECTION_ISSUE,
                        True,
                        True,
                        observations=cold,
                        endpoint="https://source.invalid/collection",
                        pages_fetched=1,
                    )
                ),
                SourceKind.MARKET: OneFetcher(
                    FetchResult(
                        SourceKind.MARKET,
                        True,
                        True,
                        observations=[current],
                        endpoint="https://source.invalid/market",
                        pages_fetched=1,
                    )
                ),
            },
            notifier=RecordingNotifier(),
            github_gateway=gateway,
            github_metadata=GitHubMetadataService(gateway, gateway, clock=lambda: NOW),
            clock=lambda: NOW,
        )
        report = await service.check(
            enabled_sources={SourceKind.COLLECTION_ISSUE, SourceKind.MARKET},
            push_targets=[],
            max_items_per_push=10,
        )
        self.assertEqual(report.updated, 1)
        self.assertEqual(report.discovered, 0)
        self.assertEqual(len(inner.calls), 5)
        self.assertTrue(inner.calls[0][0].endswith("/repos/z/updated"))
        self.assertEqual(gateway.request_priorities, [2, 3, 3, 3, 3])


class ConfigSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_ai_timeout_defaults_bounds_and_invalid_values(self) -> None:
        self.assertEqual(parse_runtime_config({}).ai_timeout_seconds, 60)
        for value in (10, 45, 120):
            with self.subTest(valid=value):
                self.assertEqual(
                    parse_runtime_config(
                        {"ai_timeout_seconds": value}
                    ).ai_timeout_seconds,
                    value,
                )
        for value in (True, False, 9, 121, "60", None):
            with self.subTest(invalid=value):
                self.assertEqual(
                    parse_runtime_config(
                        {"ai_timeout_seconds": value}
                    ).ai_timeout_seconds,
                    60,
                )

    def test_config_is_strict_and_string_targets_are_not_split(self) -> None:
        config = parse_runtime_config(
            {
                "enabled": "true",
                "poll_interval_minutes": "5",
                "push_targets": "umo:one",
                "github_token": 123,
                "llm_provider_id": [],
                "include_star_count": "yes",
            }
        )
        self.assertFalse(config.enabled)
        self.assertEqual(config.poll_interval_minutes, 30)
        self.assertEqual(config.github_token, "")
        self.assertEqual(config.llm_provider_id, "")
        self.assertEqual(config.ai_timeout_seconds, 60)
        self.assertTrue(config.include_star_count)
        self.assertEqual(validate_targets(config.push_targets), ([], 1))

    async def test_fixed_delay_busy_and_exception_are_consumed(self) -> None:
        reports = [
            RunReport(status="busy", started_at=NOW, busy=True),
            RuntimeError("boom"),
        ]
        attempts = asyncio.Event()

        async def run():
            value = reports.pop(0)
            if not reports:
                attempts.set()
            if isinstance(value, Exception):
                raise value
            return value

        scheduler = FixedDelayScheduler(
            run,
            lambda: 0.001,
            first_delay_seconds=0,
            clock=lambda: NOW,
        )
        scheduler.start()
        await asyncio.wait_for(attempts.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(scheduler.status.last_error_code, "scheduler_run_exception")
        self.assertFalse(scheduler.task.done())
        await scheduler.stop()
        self.assertEqual(scheduler.status.state, "stopped")

    async def test_busy_skip_error_is_visible_and_next_success_clears_it(self) -> None:
        reports = [
            RunReport(
                status="busy",
                started_at=NOW,
                error_code="run_skipped_busy",
                busy=True,
            ),
            RunReport(status="success", started_at=NOW),
        ]
        busy_seen = asyncio.Event()
        success_seen = asyncio.Event()

        async def run():
            report = reports.pop(0)
            if report.busy:
                busy_seen.set()
            else:
                success_seen.set()
            return report

        scheduler = FixedDelayScheduler(
            run, lambda: 0.01, first_delay_seconds=0, clock=lambda: NOW
        )
        scheduler.start()
        await asyncio.wait_for(busy_seen.wait(), 1)
        await asyncio.sleep(0)
        self.assertEqual(scheduler.status.last_error_code, "run_skipped_busy")
        await asyncio.wait_for(success_seen.wait(), 1)
        await asyncio.sleep(0)
        self.assertIsNone(scheduler.status.last_error_code)
        await scheduler.stop()

    def test_status_text_contract_is_complete_and_redacted(self) -> None:
        token = "github_pat_do-not-print"
        target = "umo:secret-target"
        runtime = parse_runtime_config(
            {
                "enabled": False,
                "poll_interval_minutes": 45,
                "push_targets": [target, ""],
                "github_token": token,
                "llm_provider_id": "provider-secret",
            }
        )
        text = format_status(
            runtime=runtime,
            enabled_sources={SourceKind.MARKET, SourceKind.COLLECTION_ISSUE},
            scheduler_state="disabled",
            scheduler_last_attempt=NOW,
            scheduler_last_success=LATER,
            scheduler_error="run_skipped_busy",
            service_busy=True,
            health="正常",
            schema_version="1",
            plugin_count=12,
            source_states="market:ok",
            github_remaining="3",
            github_reset=LATER,
            github_cache_count=7,
            configured_target_count=1,
            subscription_count=2,
            effective_target_count=3,
            pending=2,
            exhausted=1,
            last_text="上次运行报告",
        )
        expected_lines = {
            "- 配置启用：disabled",
            "- 自动调度：disabled",
            "- 服务忙碌：yes",
            "- 轮询间隔：45 分钟",
            f"- 调度上次尝试：{NOW}",
            f"- 调度上次成功：{LATER}",
            "- 调度错误：run_skipped_busy",
            "- 状态 schema_version：1",
            "- 来源开关：market=enabled, collection_issue=enabled, "
            "legacy_publish_issue=disabled, github_discovery=disabled",
            "- GitHub Token：已配置",
            "- 配置目标数：1",
            "- 群订阅数：2",
            "- 本轮有效目标数：3",
            f"- GitHub 剩余/重置：3/{LATER}",
            "- GitHub 缓存：7",
            "- 待投递目标：2",
            "- 永久失败目标：1",
            "上次运行报告",
        }
        self.assertTrue(expected_lines.issubset(set(text.splitlines())))
        self.assertNotIn(token, text)
        self.assertNotIn(target, text)
        self.assertNotIn("provider-secret", text)

    async def test_service_busy_report_has_stable_error_code(self) -> None:
        service = MarketWatcherService(
            store=MemoryStore(),
            fetchers={},
            notifier=RecordingNotifier(),
            clock=lambda: NOW,
        )
        await service.lock.acquire()
        try:
            report = await service.check(
                enabled_sources=set(), push_targets=[], max_items_per_push=10
            )
        finally:
            service.lock.release()
        self.assertTrue(report.busy)
        self.assertEqual(report.error_code, "run_skipped_busy")

    async def test_fixed_delay_waits_after_completion_and_stop_interrupts(self) -> None:
        starts = []
        twice = asyncio.Event()

        async def run():
            starts.append(time.monotonic())
            await asyncio.sleep(0.02)
            if len(starts) == 2:
                twice.set()
            return RunReport(status="success", started_at=NOW)

        scheduler = FixedDelayScheduler(
            run, lambda: 0.01, first_delay_seconds=0.01, clock=lambda: NOW
        )
        scheduler.start()
        await asyncio.wait_for(twice.wait(), 1)
        self.assertGreaterEqual(starts[1] - starts[0], 0.025)
        await scheduler.stop()

        sleeper = FixedDelayScheduler(run, lambda: 10, first_delay_seconds=10)
        sleeper.start()
        await asyncio.sleep(0)
        await asyncio.wait_for(sleeper.stop(), 0.1)
        self.assertEqual(sleeper.status.state, "stopped")


if __name__ == "__main__":
    unittest.main()
