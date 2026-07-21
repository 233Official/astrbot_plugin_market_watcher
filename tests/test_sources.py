from __future__ import annotations

import json
import unittest
from collections import deque
from copy import deepcopy
from pathlib import Path

from market_watcher.http import HttpResponse
from market_watcher.models import (
    EndpointSnapshot,
    PluginRecord,
    SourceEvidence,
    SourceKind,
    SourceState,
)
from market_watcher.sources.github_search import (
    SEARCH_ENDPOINT,
    GitHubSearchFetcher,
)
from market_watcher.sources.issues import COLLECTION_ISSUES_URL, IssuesFetcher
from market_watcher.sources.market import MARKET_RAW_URL, MarketFetcher

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
    async def test_market_parses_fields_and_injected_observed_at(self) -> None:
        http = FakeHttpClient(
            [
                response(
                    200,
                    fixture("market_api.json"),
                    headers={"Last-Modified": "Sun, 20 Jul 2026 00:00:00 GMT"},
                )
            ]
        )
        result = await MarketFetcher(http, clock=lambda: NOW).fetch()
        self.assertTrue(result.success and result.complete)
        observation = result.observations[0]
        self.assertEqual(observation.observed_at, NOW)
        self.assertEqual(observation.repo_owner, "example")
        self.assertEqual(observation.repo_name, "astrbot_plugin_demo")
        self.assertEqual(observation.stars, 12)

    async def test_incomplete_primary_uses_sparse_raw_fallback(self) -> None:
        http = FakeHttpClient(
            [
                response(200, fixture("market_incomplete.json")),
                response(200, fixture("raw_plugins.json"), headers={"ETag": '"raw"'}),
            ]
        )
        result = await MarketFetcher(http, clock=lambda: NOW).fetch()
        self.assertTrue(result.success and result.complete)
        self.assertTrue(result.from_fallback)
        self.assertEqual(result.endpoint, MARKET_RAW_URL)
        self.assertEqual(result.etag, '"raw"')
        observation = result.observations[0]
        self.assertTrue(observation.sparse)
        self.assertIsNone(observation.version)
        self.assertIsNone(observation.astrbot_version)

    async def test_304_isolated_by_endpoint(self) -> None:
        seed_http = FakeHttpClient(
            [response(503), response(200, fixture("raw_plugins.json"))]
        )
        seed = await MarketFetcher(seed_http, clock=lambda: NOW).fetch()
        previous = source_state(MARKET_RAW_URL, seed.observations, etag='"raw-old"')
        http = FakeHttpClient([response(304), response(304)])
        result = await MarketFetcher(http, clock=lambda: "later").fetch(previous)
        self.assertTrue(result.success and result.not_modified)
        self.assertTrue(result.from_fallback)
        self.assertEqual(result.endpoint, MARKET_RAW_URL)
        self.assertNotIn("If-None-Match", http.calls[0][1])
        self.assertEqual(http.calls[1][1]["If-None-Match"], '"raw-old"')

    async def test_observation_hash_can_track_star_changes(self) -> None:
        first_payload = fixture("market_api.json")
        second_payload = deepcopy(first_payload)
        second_payload["demo"]["stars"] = 999
        first = await MarketFetcher(
            FakeHttpClient([response(200, first_payload)]), clock=lambda: "first"
        ).fetch()
        second = await MarketFetcher(
            FakeHttpClient([response(200, second_payload)]), clock=lambda: "second"
        ).fetch()
        self.assertNotEqual(
            first.observations[0].observation_hash,
            second.observations[0].observation_hash,
        )

    def test_mvp_update_hash_ignores_non_update_fields(self) -> None:
        market_evidence = SourceEvidence(
            source_kind=SourceKind.MARKET,
            source_record_id="demo",
            source_url="https://example.invalid/market",
            observed_at=NOW,
        )
        issue_evidence = SourceEvidence(
            source_kind=SourceKind.COLLECTION_ISSUE,
            source_record_id="1",
            source_url="https://example.invalid/issue",
            observed_at=NOW,
        )
        base = {
            "display_name": "Demo",
            "description": "description",
            "author": "author",
            "version": "1.0.0",
            "repo_url": "https://github.com/example/astrbot_plugin_demo",
            "astrbot_version": ">=4.24",
            "platforms": ("aiocqhttp",),
            "market_status": "listed",
            "issue_state": None,
            "issue_labels": (),
            "archived": False,
        }
        first = PluginRecord(
            canonical_id="github:example/demo",
            name="old-name",
            **base,
            tags=("old",),
            stars=1,
            observed_at="first",
            evidence=(market_evidence, issue_evidence),
        ).compute_content_hash()
        second = PluginRecord(
            canonical_id="github:example/demo",
            name="new-name",
            **base,
            tags=("new",),
            stars=999,
            observed_at="second",
            evidence=(issue_evidence, market_evidence),
        ).compute_content_hash()
        changed = PluginRecord(
            canonical_id="github:example/demo",
            name="new-name",
            **{**base, "version": "2.0.0"},
        ).compute_content_hash()
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    async def test_exception_summary_does_not_persist_secret(self) -> None:
        secret = "Bearer CANARY_TOKEN https://user:pass@example.invalid/path"
        result = await MarketFetcher(
            FakeHttpClient([RuntimeError(secret), RuntimeError(secret)]),
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(result.success)
        self.assertNotIn("CANARY_TOKEN", result.error_summary or "")
        self.assertNotIn("user:pass", result.error_summary or "")


class IssueSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_collection_filters_pr_and_parses_json(self) -> None:
        result = await IssuesFetcher(
            FakeHttpClient([response(200, fixture("collection_issues.json"))]),
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.observations[0].observed_at, NOW)

    async def test_multi_page_snapshot_forces_full_issue_refetch(self) -> None:
        seed = await IssuesFetcher(
            FakeHttpClient([response(200, fixture("collection_issues.json"))]),
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        previous = source_state(
            COLLECTION_ISSUES_URL,
            seed.observations,
            pages_fetched=2,
        )
        http = FakeHttpClient([response(200, fixture("collection_issues.json"))])
        result = await IssuesFetcher(
            http,
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch(previous)
        self.assertTrue(result.complete)
        self.assertNotIn("If-None-Match", http.calls[0][1])

    async def test_safe_multi_page_and_newer_duplicate_wins(self) -> None:
        old = fixture("collection_issues.json")[0]
        new = deepcopy(old)
        new["state"] = "closed"
        new["updated_at"] = "2026-07-21T00:00:00Z"
        page_two_url = COLLECTION_ISSUES_URL.removesuffix("page=1") + "page=2"
        http = FakeHttpClient(
            [
                response(200, [old], headers={"Link": f'<{page_two_url}>; rel="next"'}),
                response(200, [new]),
            ]
        )
        result = await IssuesFetcher(
            http,
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        self.assertTrue(result.complete)
        self.assertEqual(result.pages_fetched, 2)
        self.assertEqual(len(result.observations), 1)
        self.assertEqual(result.observations[0].issue_state, "closed")

    async def test_cross_domain_and_loop_links_are_incomplete(self) -> None:
        issue = fixture("collection_issues.json")[:1]
        links = [
            '<https://evil.invalid/repos/x/y/issues?page=2>; rel="next"',
            f'<{COLLECTION_ISSUES_URL}>; rel="next"',
            '<https://api.github.com/repos/Other/Repo/issues?page=2>; rel="next"',
        ]
        for link in links:
            with self.subTest(link=link):
                result = await IssuesFetcher(
                    FakeHttpClient([response(200, issue, headers={"Link": link})]),
                    source_kind=SourceKind.COLLECTION_ISSUE,
                    clock=lambda: NOW,
                ).fetch()
                self.assertFalse(result.complete)
                self.assertEqual(result.error_code, "unsafe_pagination")

    async def test_all_damaged_issues_are_incomplete(self) -> None:
        result = await IssuesFetcher(
            FakeHttpClient([response(200, fixture("issues_all_damaged.json"))]),
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(result.complete)
        self.assertEqual(result.records_rejected, 2)

    async def test_ambiguous_duplicate_is_incomplete(self) -> None:
        old = fixture("collection_issues.json")[0]
        changed = deepcopy(old)
        changed["state"] = "closed"
        result = await IssuesFetcher(
            FakeHttpClient([response(200, [old, changed])]),
            source_kind=SourceKind.COLLECTION_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(result.complete)

    async def test_legacy_requires_label_or_historical_template(self) -> None:
        invalid = await IssuesFetcher(
            FakeHttpClient([response(200, fixture("legacy_issue_missing_label.json"))]),
            source_kind=SourceKind.LEGACY_PUBLISH_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        valid = await IssuesFetcher(
            FakeHttpClient([response(200, fixture("legacy_issues.json"))]),
            source_kind=SourceKind.LEGACY_PUBLISH_ISSUE,
            clock=lambda: NOW,
        ).fetch()
        self.assertFalse(invalid.complete)
        self.assertTrue(valid.complete)
        self.assertEqual(len(valid.observations), 1)


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
