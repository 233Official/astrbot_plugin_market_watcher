from __future__ import annotations

import json
import unittest
from collections import deque
from copy import deepcopy
from pathlib import Path

from market_watcher.detect import detect_changes
from market_watcher.http import HttpResponse
from market_watcher.merge import merge_sources
from market_watcher.models import (
    EndpointSnapshot,
    SourceKind,
    SourceState,
)
from market_watcher.normalize import fallback_canonical_id
from market_watcher.sources.github_search import (
    SEARCH_ENDPOINT,
    GitHubSearchFetcher,
)
from market_watcher.sources.market import MARKET_API_URL, MarketFetcher

FIXTURES = Path(__file__).parent / "fixtures"
NOW = "2026-07-20T12:00:00Z"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def response(status: int, payload=None, *, headers=None) -> HttpResponse:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return HttpResponse(status=status, body=body, headers=headers or {})


class FakeHttpClient:
    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self.responses = deque(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, headers=None) -> HttpResponse:
        self.calls.append((url, dict(headers or {})))
        item = self.responses.popleft()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        return None


def source_state(
    endpoint: str,
    observations,
    *,
    etag='"old"',
    pages_fetched: int = 1,
) -> SourceState:
    mapping = {item.source_record_id: item for item in observations}
    return SourceState(
        complete=True,
        observations=mapping,
        snapshots={
            endpoint: EndpointSnapshot(
                endpoint=endpoint,
                pages_fetched=pages_fetched,
                etag=etag,
                observations=mapping,
            )
        },
    )


def repo_item(index: int) -> dict:
    return {
        "id": index,
        "name": f"astrbot_plugin_{index}",
        "full_name": f"Example/astrbot_plugin_{index}",
        "html_url": f"https://github.com/Example/astrbot_plugin_{index}",
        "owner": {"login": "Example"},
        "private": False,
        "fork": False,
        "archived": False,
        "mirror_url": None,
    }


class MarketSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cloud_parsing_excludes_meta_and_uses_repo_identity(self) -> None:
        http = FakeHttpClient(
            [response(200, fixture("market_api.json"), headers={"ETag": '"cloud"'})]
        )
        result = await MarketFetcher(http, clock=lambda: NOW).fetch()
        self.assertTrue(result.success and result.complete)
        self.assertEqual(http.calls, [(MARKET_API_URL, {})])
        self.assertEqual(result.endpoint, MARKET_API_URL)
        self.assertEqual(result.records_received, 1)
        self.assertEqual(result.records_rejected, 0)
        observation = result.observations[0]
        self.assertEqual(observation.observed_at, NOW)
        self.assertEqual(observation.canonical_id, "github:example/astrbot_plugin_demo")
        self.assertEqual(observation.repo_owner, "example")
        self.assertEqual(observation.repo_name, "astrbot_plugin_demo")
        self.assertEqual(observation.version, "1.2.0")
        self.assertEqual(observation.tags, ("工具",))
        self.assertEqual(observation.stars, 12)
        self.assertFalse(observation.sparse)

    async def test_no_fallback_for_timeout_http_or_invalid_payload(self) -> None:
        failures = [
            TimeoutError(),
            response(503),
            HttpResponse(200, body=b"not-json"),
            response(200, fixture("market_incomplete.json")),
            response(200, {"$meta": {"name": "Market"}}),
            response(200, {"broken": {}}),
            response(200, {"broken": {"stars": 100}}),
            response(200, []),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                http = FakeHttpClient([failure])
                result = await MarketFetcher(http, clock=lambda: NOW).fetch()
                self.assertFalse(result.complete)
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(result.endpoint, MARKET_API_URL)

    async def test_slug_fallback_identity_without_github_repository(self) -> None:
        result = await MarketFetcher(
            FakeHttpClient(
                [response(200, {"author/plugin": {"name": "Plugin", "desc": "Useful"}})]
            ),
            clock=lambda: NOW,
        ).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(
            result.observations[0].canonical_id,
            fallback_canonical_id(SourceKind.MARKET, "author/plugin"),
        )
        self.assertIsNone(result.observations[0].repo_url)

    async def test_304_reuses_only_cloud_snapshot(self) -> None:
        seed = await MarketFetcher(
            FakeHttpClient([response(200, fixture("market_api.json"))]),
            clock=lambda: NOW,
        ).fetch()
        previous = source_state(MARKET_API_URL, seed.observations, etag='"cloud-old"')
        http = FakeHttpClient([response(304)])
        result = await MarketFetcher(http, clock=lambda: "later").fetch(previous)
        self.assertTrue(result.success and result.not_modified)
        self.assertEqual(result.observations, seed.observations)
        self.assertEqual(
            http.calls, [(MARKET_API_URL, {"If-None-Match": '"cloud-old"'})]
        )

        previous = source_state("https://old-market.invalid/plugins", seed.observations)
        http = FakeHttpClient([response(304)])
        result = await MarketFetcher(http, clock=lambda: NOW).fetch(previous)
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "not_modified_without_endpoint_snapshot")
        self.assertEqual(http.calls, [(MARKET_API_URL, {})])

        result = await MarketFetcher(
            FakeHttpClient([response(304)]), clock=lambda: NOW
        ).fetch(source_state(MARKET_API_URL, []))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "no_valid_plugins")

    async def test_meta_counts_and_times_never_change_plugin_content(self) -> None:
        first_payload = fixture("market_api.json")
        second_payload = deepcopy(first_payload)
        second_payload["$meta"]["version"] = "later"
        second_payload["$meta"]["updated_at"] = "later"
        second_payload["demo"].update(
            stars=999,
            forks=40,
            download_count=99999,
            updated_at="2026-11-01T00:00:00Z",
        )
        previous = {}
        hashes = []
        for payload in (first_payload, second_payload):
            result = await MarketFetcher(
                FakeHttpClient([response(200, payload)]), clock=lambda: NOW
            ).fetch()
            self.assertTrue(result.complete)
            self.assertEqual(result.records_received, 1)
            self.assertEqual(result.records_rejected, 0)
            hashes.append(result.observations[0].observation_hash)
            current = merge_sources(
                {
                    SourceKind.MARKET.value: source_state(
                        MARKET_API_URL, result.observations
                    )
                },
                previous,
            )
            if previous:
                self.assertEqual(detect_changes(previous, current, detected_at=NOW), [])
            previous = current
        self.assertEqual(hashes[0], hashes[1])

    async def test_fixed_a_b_shrink_failure_recovery_and_real_changes(self) -> None:
        snapshot_a = fixture("market_cloud_a.json")
        snapshot_b = fixture("market_cloud_b.json")
        changed = deepcopy(snapshot_a)
        changed["demo"]["desc"] = "实质描述更新"
        changed["demo"]["version"] = "2.0.0"
        changed["demo"]["name"] = "renamed-demo"
        changed["demo"]["support_platforms"] = ["telegram"]
        changed["demo"]["tags"] = ["新标签"]
        changed["new"] = {
            "name": "new",
            "repo": "https://github.com/example/astrbot_plugin_new",
            "version": "1.0.0",
        }
        http = FakeHttpClient(
            [
                response(200, snapshot_a),
                response(200, snapshot_b),
                response(200, snapshot_a),
                TimeoutError(),
                response(200, {"$meta": {"version": "invalid"}}),
                HttpResponse(200, body=b"invalid"),
                response(200, snapshot_a),
                response(200, changed),
                response(200, changed),
            ]
        )
        fetcher = MarketFetcher(http, clock=lambda: NOW)
        previous = {}
        accepted_source = None
        for index in range(9):
            result = await fetcher.fetch(accepted_source)
            if not result.success or not result.complete:
                self.assertIn(index, (3, 4, 5))
                self.assertEqual(len(previous), 2)
                continue
            accepted_source = source_state(MARKET_API_URL, result.observations)
            current = merge_sources(
                {SourceKind.MARKET.value: accepted_source}, previous
            )
            events = detect_changes(previous, current, detected_at=NOW)
            if index == 0:
                self.assertEqual(len(events), 2)
            elif index == 7:
                self.assertEqual(
                    [event.kind.value for event in events], ["discovered", "updated"]
                )
                self.assertEqual(
                    events[1].changed_fields,
                    ("name", "description", "version", "platforms", "tags"),
                )
            else:
                self.assertEqual(events, [])
            if index == 1:
                missing_id = "github:example/astrbot_plugin_second"
                self.assertEqual(current[missing_id], previous[missing_id])
            previous = current
        self.assertEqual(len(previous), 3)
        self.assertEqual(len(http.calls), 9)

    async def test_exception_summary_does_not_persist_secret(self) -> None:
        secret = "Bearer CANARY_TOKEN https://user:pass@example.invalid/path"
        result = await MarketFetcher(
            FakeHttpClient([RuntimeError(secret)]), clock=lambda: NOW
        ).fetch()
        self.assertFalse(result.success)
        self.assertNotIn("CANARY_TOKEN", result.error_summary or "")
        self.assertNotIn("user:pass", result.error_summary or "")


class GitHubSearchSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_filters_mirror_and_uses_full_name_without_id(self) -> None:
        result = await GitHubSearchFetcher(
            FakeHttpClient(
                [response(200, fixture("github_search_missing_id_mirror.json"))]
            ),
            clock=lambda: NOW,
        ).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(
            result.observations[0].source_record_id,
            "full_name:example/astrbot_plugin_no_id",
        )

    async def test_malformed_owner_is_rejected_and_marks_incomplete(self) -> None:
        result = await GitHubSearchFetcher(
            FakeHttpClient(
                [response(200, fixture("github_search_malformed_owner.json"))]
            ),
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(result.complete)
        self.assertEqual(result.records_rejected, 1)

    async def test_two_pages_are_complete_only_when_total_is_consumed(self) -> None:
        page_one = {
            "total_count": 101,
            "incomplete_results": False,
            "items": [repo_item(index) for index in range(1, 101)],
        }
        result = await GitHubSearchFetcher(
            FakeHttpClient(
                [
                    response(200, page_one),
                    response(200, fixture("github_search_second_page.json")),
                ]
            ),
            clock=lambda: NOW,
        ).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(result.records_received, 101)

    async def test_duplicate_repository_id_across_pages_is_incomplete(self) -> None:
        page_one = {
            "total_count": 101,
            "incomplete_results": False,
            "items": [repo_item(index) for index in range(1, 101)],
        }
        duplicate = {
            "total_count": 101,
            "incomplete_results": False,
            "items": [repo_item(1)],
        }
        result = await GitHubSearchFetcher(
            FakeHttpClient([response(200, page_one), response(200, duplicate)]),
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(result.complete)

    async def test_truncation_incomplete_flag_and_total_mismatch(self) -> None:
        truncated = {
            "total_count": 250,
            "incomplete_results": False,
            "items": [repo_item(index) for index in range(1, 101)],
        }
        second = {
            "total_count": 250,
            "incomplete_results": False,
            "items": [repo_item(index) for index in range(101, 201)],
        }
        truncated_result = await GitHubSearchFetcher(
            FakeHttpClient([response(200, truncated), response(200, second)]),
            clock=lambda: NOW,
        ).fetch()
        incomplete_result = await GitHubSearchFetcher(
            FakeHttpClient([response(200, fixture("github_search_incomplete.json"))]),
            clock=lambda: NOW,
        ).fetch()
        mismatch_second = deepcopy(fixture("github_search_second_page.json"))
        mismatch_second["total_count"] = 102
        mismatch_result = await GitHubSearchFetcher(
            FakeHttpClient(
                [
                    response(
                        200,
                        {
                            "total_count": 101,
                            "incomplete_results": False,
                            "items": [repo_item(index) for index in range(1, 101)],
                        },
                    ),
                    response(200, mismatch_second),
                ]
            ),
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(truncated_result.complete)
        self.assertFalse(incomplete_result.complete)
        self.assertFalse(mismatch_result.complete)

    async def test_304_reuses_only_matching_search_snapshot(self) -> None:
        seed = await GitHubSearchFetcher(
            FakeHttpClient([response(200, fixture("github_search.json"))]),
            clock=lambda: NOW,
        ).fetch()
        previous = source_state(SEARCH_ENDPOINT, seed.observations)
        result = await GitHubSearchFetcher(
            FakeHttpClient([response(304)]), clock=lambda: "later"
        ).fetch(previous)
        self.assertTrue(result.complete and result.not_modified)
        self.assertEqual(result.observations, seed.observations)

    async def test_multi_page_snapshot_forces_full_search_refetch(self) -> None:
        seed = await GitHubSearchFetcher(
            FakeHttpClient([response(200, fixture("github_search.json"))]),
            clock=lambda: NOW,
        ).fetch()
        previous = source_state(
            SEARCH_ENDPOINT,
            seed.observations,
            pages_fetched=2,
        )
        http = FakeHttpClient([response(200, fixture("github_search.json"))])
        result = await GitHubSearchFetcher(http, clock=lambda: NOW).fetch(previous)
        self.assertTrue(result.complete)
        self.assertNotIn("If-None-Match", http.calls[0][1])

    async def test_304_does_not_reuse_an_unrelated_snapshot(self) -> None:
        previous = SourceState(
            snapshots={
                "https://api.github.com/other": EndpointSnapshot(
                    endpoint="https://api.github.com/other",
                    observations={},
                )
            }
        )
        result = await GitHubSearchFetcher(
            FakeHttpClient([response(304)]), clock=lambda: NOW
        ).fetch(previous)
        self.assertFalse(result.success)
        self.assertFalse(result.complete)
        self.assertEqual(
            result.error_code,
            "not_modified_without_endpoint_snapshot",
        )


if __name__ == "__main__":
    unittest.main()
