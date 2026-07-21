from __future__ import annotations

import unittest
from copy import deepcopy

from market_watcher.ai import AiIntroService
from market_watcher.merge import merge_sources
from market_watcher.models import (
    FetchResult,
    SourceKind,
    SourceObservation,
    SourceState,
    WatcherState,
)
from market_watcher.outbox import create_batches, merge_targets
from market_watcher.service import MarketWatcherService
from market_watcher.subscriptions import subscription_status, update_subscription
from tests.test_m4 import event

NOW = "2026-07-20T12:00:00Z"


class MemoryStore:
    def __init__(self, state=None, *, fail_save=False):
        self.state = deepcopy(state or WatcherState())
        self.fail_save = fail_save

    def load(self):
        return deepcopy(self.state)

    def save(self, state):
        if self.fail_save:
            raise OSError("save failed")
        self.state = deepcopy(state)


class Event:
    def __init__(self, origin="adapter:GroupMessage:secret", *, private=False):
        self.unified_msg_origin = origin
        self.private = private

    def is_private_chat(self):
        return self.private


class SubscriptionTests(unittest.TestCase):
    def test_state_subscriptions_are_strict_sorted_and_deduplicated(self):
        state = WatcherState(subscriptions=[" z ", "a", "z"])
        self.assertEqual(state.subscriptions, ["a", "z"])
        self.assertEqual(WatcherState.from_dict(state.to_dict()), state)

        for invalid in ("not-a-list", [1], [""], ["x" * 513]):
            with self.subTest(invalid=invalid):
                data = WatcherState().to_dict()
                data["subscriptions"] = invalid
                with self.assertRaises(ValueError):
                    WatcherState.from_dict(data)

    def test_subscribe_duplicate_unsubscribe_private_and_status(self):
        store = MemoryStore()
        group = Event()
        self.assertEqual(
            update_subscription(store, group, subscribe=True), "subscribed"
        )
        self.assertEqual(
            update_subscription(store, group, subscribe=True),
            "already_subscribed",
        )
        self.assertEqual(subscription_status(store, group), (1, True))
        self.assertEqual(
            update_subscription(store, group, subscribe=False), "unsubscribed"
        )
        self.assertEqual(
            update_subscription(store, group, subscribe=False),
            "already_unsubscribed",
        )
        self.assertEqual(
            update_subscription(store, Event(private=True), subscribe=True),
            "private_chat",
        )
        self.assertEqual(store.state.subscriptions, [])

    def test_subscription_save_failure_does_not_mutate_stored_state(self):
        original = WatcherState(plugins={})
        store = MemoryStore(original, fail_save=True)
        with self.assertRaises(OSError):
            update_subscription(store, Event(), subscribe=True)
        self.assertEqual(store.state, original)

    def test_qq_official_group_umo_is_saved_unchanged(self):
        origin = "qqws:GROUP_MESSAGE:group_openid"
        store = MemoryStore()
        self.assertEqual(
            update_subscription(store, Event(origin), subscribe=True), "subscribed"
        )
        self.assertEqual(store.state.subscriptions, [origin])
        self.assertEqual(subscription_status(store, Event(origin)), (1, True))

    def test_target_merge_is_stable_and_historical_batch_is_immutable(self):
        old = create_batches([event()], ["old-target"], max_items=10, created_at=NOW)[0]
        before = deepcopy(old.targets)
        targets, invalid = merge_targets(
            ["config-b", "config-a", "config-a"],
            ["subscription", "config-b"],
        )
        self.assertEqual(targets, ["config-a", "config-b", "subscription"])
        self.assertEqual(invalid, 0)
        create_batches([event()], targets, max_items=10, created_at=NOW)
        self.assertEqual(old.targets, before)


class ResolvingAiClient:
    def __init__(self, resolved="resolved-provider", *, resolve_error=None):
        self.resolved = resolved
        self.resolve_error = resolve_error
        self.resolve_calls = []
        self.generate_calls = []

    async def resolve_provider_id(self, origin):
        self.resolve_calls.append(origin)
        if self.resolve_error:
            raise self.resolve_error
        return self.resolved

    async def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        return "导语"


class ProviderResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_provider_wins_and_manual_origin_resolves_default(self):
        explicit_client = ResolvingAiClient()
        explicit = await AiIntroService(explicit_client).generate(
            [event()],
            enabled=True,
            provider_id="explicit-provider",
            provider_origin="secret-origin",
        )
        self.assertEqual(explicit.status, "success")
        self.assertEqual(explicit_client.resolve_calls, [])
        self.assertEqual(
            explicit_client.generate_calls[0]["provider_id"], "explicit-provider"
        )

        default_client = ResolvingAiClient("default-provider")
        resolved = await AiIntroService(default_client).generate(
            [event()],
            enabled=True,
            provider_id="",
            provider_origin="manual-origin",
        )
        self.assertEqual(resolved.status, "success")
        self.assertEqual(default_client.resolve_calls, ["manual-origin"])
        self.assertEqual(
            default_client.generate_calls[0]["provider_id"], "default-provider"
        )

    async def test_no_origin_empty_resolution_and_exception_fallback(self):
        no_origin = await AiIntroService(ResolvingAiClient()).generate(
            [event()], enabled=True, provider_id="", provider_origin=None
        )
        self.assertEqual(
            (no_origin.status, no_origin.error_code),
            ("skipped", "ai_provider_not_found"),
        )
        for client in (
            ResolvingAiClient(""),
            ResolvingAiClient(resolve_error=RuntimeError("secret")),
        ):
            result = await AiIntroService(client).generate(
                [event()],
                enabled=True,
                provider_id="",
                provider_origin="origin",
            )
            self.assertEqual(
                (result.status, result.error_code),
                ("fallback", "ai_provider_not_found"),
            )

    async def test_automatic_run_uses_first_effective_target_as_origin(self):
        old = _observation("1.0.0")
        current = _observation("2.0.0")
        source = SourceState(
            baseline_established=True,
            complete=True,
            observations={old.source_record_id: old},
        )
        state = WatcherState(
            subscriptions=["subscription-origin"],
            sources={SourceKind.MARKET.value: source},
            plugins=merge_sources({SourceKind.MARKET.value: source}),
        )
        client = ResolvingAiClient()
        service = MarketWatcherService(
            store=MemoryStore(state),
            fetchers={SourceKind.MARKET: OneFetcher(current)},
            notifier=Notifier(),
            ai_intro=AiIntroService(client),
            clock=lambda: NOW,
        )
        await service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["z-config", "a-config"],
            max_items_per_push=10,
            enable_ai_summary=True,
            llm_provider_id="",
        )
        self.assertEqual(client.resolve_calls, ["a-config"])

        manual_client = ResolvingAiClient()
        manual_service = MarketWatcherService(
            store=MemoryStore(state),
            fetchers={SourceKind.MARKET: OneFetcher(current)},
            notifier=Notifier(),
            ai_intro=AiIntroService(manual_client),
            clock=lambda: NOW,
        )
        await manual_service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["a-config"],
            max_items_per_push=10,
            enable_ai_summary=True,
            llm_provider_id="",
            provider_origin="manual-origin",
        )
        self.assertEqual(manual_client.resolve_calls, ["manual-origin"])


class OneFetcher:
    def __init__(self, current):
        self.current = current

    async def fetch(self, previous=None):
        return FetchResult(
            SourceKind.MARKET,
            True,
            True,
            observations=[self.current],
            endpoint="https://example.invalid/api",
            pages_fetched=1,
        )


class Notifier:
    async def send(self, target, message):
        return True, None

    def clear_prepared(self):
        pass


def _observation(version):
    return SourceObservation(
        source_kind=SourceKind.MARKET,
        source_record_id="demo",
        source_url="https://example.invalid/demo",
        observed_at=NOW,
        fetched_from="https://example.invalid/api",
        canonical_id="github:owner/demo",
        repo_url="https://github.com/owner/demo",
        repo_owner="owner",
        repo_name="demo",
        name="demo",
        version=version,
        observation_hash=f"hash:{version}",
    )


if __name__ == "__main__":
    unittest.main()
