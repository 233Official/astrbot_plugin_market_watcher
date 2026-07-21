from __future__ import annotations

import asyncio
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from market_watcher.ai import (
    AI_SYSTEM_PROMPT,
    AiIntroService,
    AiProviderMissing,
    AiRoleError,
    AiTimeout,
    build_prompt,
)
from market_watcher.astrbot_adapter import AiCallFailed, AstrBotAiClient
from market_watcher.merge import merge_sources
from market_watcher.models import (
    ChangeEvent,
    ChangeKind,
    FetchResult,
    PluginRecord,
    SourceEvidence,
    SourceKind,
    SourceObservation,
    SourceState,
    WatcherState,
)
from market_watcher.outbox import create_batches
from market_watcher.service import MarketWatcherService
from market_watcher.summary import MAX_MESSAGE_LENGTH, render_summary
from tests.integration.test_astrbot_contract import load_astrbot_contract

NOW = "2026-07-20T12:00:00Z"


def event(version: str = "2.0.0") -> ChangeEvent:
    evidence = SourceEvidence(
        SourceKind.MARKET,
        "demo",
        "https://example.invalid/plugins/demo",
        NOW,
    )
    current = PluginRecord(
        "github:owner/demo",
        "demo",
        display_name="Demo",
        description="公开用途",
        version=version,
        stars=12,
        evidence=(evidence,),
    )
    return ChangeEvent(
        event_id="event:updated:stable",
        kind=ChangeKind.UPDATED,
        canonical_id=current.canonical_id,
        current=current,
        previous=deepcopy(current),
        changed_fields=("version",),
        detected_at=NOW,
    )


class FakeAiClient:
    def __init__(self, outcome="生态出现一项变化") -> None:
        self.outcome = outcome
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class AiBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_empty_provider_and_no_events_do_not_call_client(self):
        client = FakeAiClient()
        service = AiIntroService(client)
        self.assertEqual(
            (await service.generate([event()], enabled=False, provider_id="p")).status,
            "disabled",
        )
        empty = await service.generate([event()], enabled=True, provider_id="")
        self.assertEqual(
            (empty.status, empty.error_code), ("skipped", "ai_provider_not_found")
        )
        no_events = await service.generate([], enabled=True, provider_id="p")
        self.assertEqual(
            (no_events.status, no_events.error_code), ("skipped", "ai_no_events")
        )
        self.assertEqual(client.calls, [])

    async def test_provider_timeout_role_empty_exception_and_success(self):
        cases = (
            (AiProviderMissing(), "ai_provider_not_found"),
            (AiTimeout(), "ai_timeout"),
            (AiRoleError(), "ai_role_error"),
            (RuntimeError("secret detail"), "ai_exception"),
            ("", "ai_empty_output"),
            ("x" * 121, "ai_output_too_long"),
        )
        for outcome, code in cases:
            with self.subTest(code=code):
                result = await AiIntroService(FakeAiClient(outcome)).generate(
                    [event()], enabled=True, provider_id="provider"
                )
                self.assertEqual((result.status, result.error_code), ("fallback", code))

        result = await AiIntroService(
            FakeAiClient("@all **变化**\n[CQ:at,qq=1]")
        ).generate([event()], enabled=True, provider_id="provider")
        self.assertEqual(result.status, "success")
        self.assertNotIn("@", result.intro)
        self.assertNotIn("[CQ:", result.intro)
        self.assertNotIn("\n", result.intro)

    async def test_cancelled_error_propagates(self):
        with self.assertRaises(asyncio.CancelledError):
            await AiIntroService(FakeAiClient(asyncio.CancelledError())).generate(
                [event()], enabled=True, provider_id="provider"
            )

    def test_prompt_contains_only_bounded_public_facts(self):
        item = event()
        item.current.description = (
            "公开用途 github_pat_CANARY_TOKEN ghp_CANARYTOKEN12345678 "
            "sk-CANARYTOKEN123456 aiocqhttp:GroupMessage:CANARY_UMO"
        )
        item.current.evidence[
            0
        ].source_url = (
            "https://example.invalid/plugins/demo?token=github_pat_CANARY_TOKEN"
        )
        prompt = build_prompt([item] * 20)
        self.assertLessEqual(len(prompt), 6000)
        self.assertEqual(prompt.count("事件="), 10)
        self.assertIn("来源类别=market", prompt)
        self.assertIn("https://example.invalid/plugins/demo", prompt)
        for canary in (
            "github_pat_CANARY_TOKEN",
            "ghp_CANARYTOKEN12345678",
            "sk-CANARYTOKEN123456",
            "aiocqhttp:GroupMessage:CANARY_UMO",
            "raw_excerpt_canary",
        ):
            self.assertNotIn(canary, prompt)
        self.assertIn("不得评价安全性", AI_SYSTEM_PROMPT)

    async def test_astrbot_adapter_call_shape_and_failures(self):
        class Context:
            def __init__(self, outcome):
                self.outcome = outcome
                self.calls = []

            async def llm_generate(self, **kwargs):
                self.calls.append(kwargs)
                if isinstance(self.outcome, BaseException):
                    raise self.outcome
                if self.outcome == "sleep":
                    await asyncio.sleep(1)
                return self.outcome

            async def get_current_chat_provider_id(self, origin):
                return "default-provider"

        context = Context(SimpleNamespace(role="assistant", completion_text="ok"))
        value = await AstrBotAiClient(context).generate(
            provider_id="provider", prompt="facts", system_prompt="rules"
        )
        self.assertEqual(value, "ok")
        self.assertEqual(
            context.calls,
            [
                {
                    "chat_provider_id": "provider",
                    "prompt": "facts",
                    "system_prompt": "rules",
                }
            ],
        )
        self.assertEqual(
            await AstrBotAiClient(context).resolve_provider_id("secret-origin"),
            "default-provider",
        )

        role_err = Context(SimpleNamespace(role="err", completion_text="detail"))
        with self.assertRaises(AiRoleError):
            await AstrBotAiClient(role_err).generate(
                provider_id="p", prompt="x", system_prompt="y"
            )

        ProviderNotFoundError = type("ProviderNotFoundError", (Exception,), {})
        with self.assertRaises(AiProviderMissing):
            await AstrBotAiClient(Context(ProviderNotFoundError())).generate(
                provider_id="p", prompt="x", system_prompt="y"
            )

        ProviderNotFound = type("ProviderNotFound", (Exception,), {})
        with self.assertRaises(AiCallFailed):
            await AstrBotAiClient(Context(ProviderNotFound())).generate(
                provider_id="p", prompt="x", system_prompt="y"
            )

        self.assertEqual(AstrBotAiClient(context).timeout_seconds, 60)

        class CancelAwareContext:
            def __init__(self):
                self.cancelled = False

            async def llm_generate(self, **kwargs):
                del kwargs
                try:
                    await asyncio.sleep(1)
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise

        cancel_aware = CancelAwareContext()
        with self.assertRaises(AiTimeout):
            await AstrBotAiClient(cancel_aware, timeout_seconds=0.001).generate(
                provider_id="p", prompt="x", system_prompt="y"
            )
        self.assertTrue(cancel_aware.cancelled)

        fallback = await AiIntroService(
            AstrBotAiClient(Context("sleep"), timeout_seconds=0.001)
        ).generate([event()], enabled=True, provider_id="p")
        self.assertEqual(
            (fallback.status, fallback.error_code), ("fallback", "ai_timeout")
        )

        cancelled = Context(asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            await AstrBotAiClient(cancelled).generate(
                provider_id="p", prompt="x", system_prompt="y"
            )

    def test_ai_text_does_not_change_batch_identity_or_facts(self):
        plain = create_batches([event()], ["umo"], max_items=10, created_at=NOW)[0]
        first = create_batches(
            [event()], ["umo"], max_items=10, created_at=NOW, intro="导语一"
        )[0]
        second = create_batches(
            [event()], ["umo"], max_items=10, created_at=NOW, intro="完全不同导语"
        )[0]
        self.assertEqual(
            {plain.batch_id, first.batch_id, second.batch_id}, {plain.batch_id}
        )
        for batch in (plain, first, second):
            self.assertIn("【实质更新】Demo", batch.message)
            self.assertIn("版本：2.0.0", batch.message)

    def test_overflowing_intro_is_dropped_without_truncating_facts(self):
        events = []
        for index in range(200):
            item = event(str(index))
            item.event_id = f"event:updated:{index}"
            item.canonical_id = f"github:owner/demo-{index}"
            item.current.canonical_id = item.canonical_id
            item.current.name = f"demo-{index}"
            item.current.display_name = f"Demo {index}"
            item.current.description = None
            events.append(item)
        facts = render_summary(events, 1, 1, total_items=len(events))
        intro = "导" * 120
        self.assertLessEqual(len(facts), MAX_MESSAGE_LENGTH)
        self.assertGreater(len(intro) + 2 + len(facts), MAX_MESSAGE_LENGTH)
        batch = create_batches(
            events, ["umo"], max_items=200, created_at=NOW, intro=intro
        )[0]
        self.assertEqual(batch.message, facts)
        self.assertLessEqual(len(batch.message), MAX_MESSAGE_LENGTH)

    def test_integration_loader_skips_only_when_top_level_package_is_absent(self):
        with (
            patch(
                "tests.integration.test_astrbot_contract.importlib.util.find_spec",
                return_value=None,
            ),
            self.assertRaises(unittest.SkipTest),
        ):
            load_astrbot_contract()

        root_cause = ImportError("broken astrbot.api.event")
        with (
            patch(
                "tests.integration.test_astrbot_contract.importlib.util.find_spec",
                return_value=object(),
            ),
            patch(
                "tests.integration.test_astrbot_contract.importlib.import_module",
                side_effect=root_cause,
            ),
            self.assertRaisesRegex(ImportError, "broken astrbot.api.event"),
        ):
            load_astrbot_contract()


class MemoryStore:
    def __init__(self, state):
        self.state = deepcopy(state)

    def load(self):
        return deepcopy(self.state)

    def save(self, state):
        self.state = deepcopy(state)


class OneFetcher:
    def __init__(self, result):
        self.result = result

    async def fetch(self, previous=None):
        return self.result


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    async def send(self, target, message):
        self.calls.append((target, message))
        return True, None

    def clear_prepared(self):
        pass


def observation(version: str) -> SourceObservation:
    return SourceObservation(
        source_kind=SourceKind.MARKET,
        source_record_id="demo",
        source_url="https://example.invalid/plugins/demo",
        observed_at=NOW,
        fetched_from="https://example.invalid/api",
        canonical_id="github:owner/demo",
        repo_url="https://github.com/owner/demo",
        repo_owner="owner",
        repo_name="demo",
        name="demo",
        display_name="Demo",
        description="公开用途",
        version=version,
        observation_hash=f"hash:{version}",
    )


class AiOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_failure_still_sends_deterministic_facts_and_logs_are_safe(self):
        old = observation("1.0.0")
        current = observation("2.0.0")
        old_source = SourceState(
            baseline_established=True,
            complete=True,
            observations={old.source_record_id: old},
        )
        state = WatcherState(
            sources={SourceKind.MARKET.value: old_source},
            plugins=merge_sources({SourceKind.MARKET.value: old_source}),
        )
        notifier = RecordingNotifier()
        observations = []
        tick = iter(index / 1000 for index in range(1000))
        service = MarketWatcherService(
            store=MemoryStore(state),
            fetchers={
                SourceKind.MARKET: OneFetcher(
                    FetchResult(
                        SourceKind.MARKET,
                        True,
                        True,
                        observations=[current],
                        endpoint="https://example.invalid/api",
                        pages_fetched=1,
                    )
                )
            },
            notifier=notifier,
            ai_intro=AiIntroService(FakeAiClient(RuntimeError("CANARY_SECRET"))),
            clock=lambda: NOW,
            monotonic=lambda: next(tick),
            observer=observations.append,
        )
        report = await service.check(
            enabled_sources={SourceKind.MARKET},
            push_targets=["umo:CANARY_TARGET"],
            max_items_per_push=10,
            enable_ai_summary=True,
            llm_provider_id="provider-secret",
        )
        self.assertEqual(
            (report.ai_status, report.ai_error_code), ("fallback", "ai_exception")
        )
        self.assertEqual(len(notifier.calls), 1)
        message = notifier.calls[0][1]
        self.assertTrue(message.startswith("AstrBot 插件市场变化"))
        self.assertIn("版本：2.0.0", message)
        self.assertEqual(
            set(report.phase_durations_ms),
            {"fetch", "merge", "detect", "github", "ai", "save", "deliver", "overall"},
        )
        serialized_logs = repr(observations)
        self.assertNotIn("CANARY_SECRET", serialized_logs)
        self.assertNotIn("CANARY_TARGET", serialized_logs)
        self.assertNotIn("provider-secret", serialized_logs)
        self.assertTrue(
            all(
                set(item)
                == {
                    "run_id",
                    "phase",
                    "duration_ms",
                    "events",
                    "sources_succeeded",
                    "sources_failed",
                    "error_code",
                }
                for item in observations
            )
        )

    async def test_finished_at_and_overall_follow_operational_save(self):
        sequence = []

        class OrderedStore:
            def __init__(self, state):
                self.state = deepcopy(state)

            def load(self):
                sequence.append(("load", None))
                return deepcopy(self.state)

            def save(self, state):
                sequence.append(("save", state.last_run.get("finished_at")))
                self.state = deepcopy(state)

        class OrderedNotifier:
            async def send(self, target, message):
                sequence.append(("deliver", target))
                return True, None

            def clear_prepared(self):
                pass

        wall_index = 0

        def wall_clock():
            nonlocal wall_index
            wall_index += 1
            value = f"2026-07-20T12:00:{wall_index:02d}Z"
            sequence.append(("clock", value))
            return value

        monotonic_index = 0

        def monotonic():
            nonlocal monotonic_index
            monotonic_index += 1
            return monotonic_index / 100

        pending = create_batches(
            [event()], ["umo:target"], max_items=10, created_at=NOW
        )[0]
        state = WatcherState(outbox={pending.batch_id: pending})
        store = OrderedStore(state)
        report = await MarketWatcherService(
            store=store,
            fetchers={},
            notifier=OrderedNotifier(),
            clock=wall_clock,
            monotonic=monotonic,
        ).check(enabled_sources=set(), push_targets=[], max_items_per_push=10)

        finished_position = sequence.index(("clock", report.finished_at))
        save_positions = [
            index for index, item in enumerate(sequence) if item[0] == "save"
        ]
        deliver_position = next(
            index for index, item in enumerate(sequence) if item[0] == "deliver"
        )
        self.assertLess(deliver_position, finished_position)
        self.assertLess(save_positions[-2], finished_position)
        self.assertLess(finished_position, save_positions[-1])
        self.assertEqual(store.state.last_run["finished_at"], report.finished_at)
        phase_sum = sum(
            value
            for name, value in report.phase_durations_ms.items()
            if name != "overall"
        )
        self.assertGreaterEqual(report.phase_durations_ms["overall"], phase_sum)


if __name__ == "__main__":
    unittest.main()
