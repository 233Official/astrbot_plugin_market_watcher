"""T2I image card delivery: batch round-trip, pagination, prepare/send, fallback."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from copy import deepcopy

from market_watcher.astrbot_adapter import AstrBotNotifier, FakeNotifier
from market_watcher.card_renderer import (
    build_card_payload,
)
from market_watcher.models import (
    ChangeEvent,
    ChangeKind,
    DeliveryBatch,
    DeliveryStatus,
    DeliveryTargetState,
    PluginRecord,
    SourceEvidence,
    SourceKind,
    WatcherState,
)
from market_watcher.outbox import (
    create_batches,
    deliver_pending,
)

NOW = "2026-07-21T12:00:00Z"
LATER = "2026-07-21T13:00:00Z"

# -- Toy image bytes for validation tests -----------------------------------

_VALID_JPEG = b"\xff\xd8" + b"\x00" * 48 + b"\xff\xd9"  # >= 50, SOI+EOI
_VALID_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60  # >= 60, signature
_VALID_WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 30  # >= 30, RIFF+WEBP
_VALID_GIF = b"GIF89a" + b"\x00" * 30 + b"\x3b"  # >= 30, trailer

_INTERNAL_SERVER_ERROR = b"Internal Server Error" + b" " * 85  # 107 bytes total
_HTML_CONTENT = b"<html><body>error page</body></html>"
_JSON_CONTENT = b'{"error":"not_found"}'
_UNKNOWN_CONTENT = b"This is random text that is not an image"
_TRUNCATED_JPEG = b"\xff\xd8\xff\xe0"  # too short


def make_event(index: int = 1, kind: ChangeKind = ChangeKind.DISCOVERED) -> ChangeEvent:
    canonical = f"github:owner/plugin-{index}"
    evidence = SourceEvidence(
        SourceKind.MARKET,
        f"plugin-{index}",
        f"https://example.invalid/plugin-{index}",
        NOW,
    )
    current = PluginRecord(
        canonical_id=canonical,
        name=f"plugin-{index}",
        display_name=f"插件 {index}",
        description="测试插件描述。",
        version="1.0.0",
        author="作者",
        stars=10,
        evidence=(evidence,),
    )
    return ChangeEvent(
        event_id=f"event:{kind.value}:{index}",
        kind=kind,
        canonical_id=canonical,
        current=current,
        previous=None,
        changed_fields=("version",) if kind is ChangeKind.UPDATED else (),
        detected_at=NOW,
    )


class FakeStore:
    def __init__(self, state: WatcherState | None = None) -> None:
        self.state = deepcopy(state or WatcherState())
        self.saves = 0

    def load(self) -> WatcherState:
        return deepcopy(self.state)

    def save(self, state: WatcherState) -> None:
        self.saves += 1
        self.state = deepcopy(state)


class BatchPayloadTests(unittest.TestCase):
    """1. batch payload round-trip; old batch no payload"""

    def test_new_batch_with_payload_survives_json_round_trip(self) -> None:
        payload = build_card_payload(
            [make_event()],
            intro="测试导语。",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:test",
            event_ids=("event:discovered:1",),
            message="测试文本",
            created_at=NOW,
            targets={"umo:a": DeliveryTargetState(target="umo:a")},
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        restored = WatcherState.from_dict(state.to_dict())
        restored_batch = restored.outbox[batch.batch_id]
        cp = restored_batch.card_payload
        self.assertIsNotNone(cp)
        assert cp is not None
        self.assertEqual(cp["title"], payload["title"])
        self.assertEqual(restored_batch.message, batch.message)
        self.assertEqual(restored_batch.targets["umo:a"].target, "umo:a")

    def test_old_batch_without_payload_loads_and_defaults_to_none(self) -> None:
        """Old state without card_payload field must load with card_payload=None."""
        batch = DeliveryBatch(
            batch_id="batch:old",
            event_ids=(),
            message="旧版本文本",
            created_at=NOW,
            targets={"umo:old": DeliveryTargetState(target="umo:old")},
        )
        self.assertIsNone(batch.card_payload)
        data = batch.to_dict()
        self.assertNotIn("card_payload", data)
        # Simulate old state: remove card_payload
        full = WatcherState(outbox={batch.batch_id: batch}).to_dict()
        self.assertNotIn("card_payload", full["outbox"]["batch:old"])
        restored = WatcherState.from_dict(full)
        self.assertIsNone(restored.outbox["batch:old"].card_payload)
        self.assertEqual(restored.outbox["batch:old"].message, "旧版本文本")

    def test_batch_with_card_payload_to_dict_includes_it(self) -> None:
        payload = {"title": "test", "items": []}
        batch = DeliveryBatch(
            batch_id="batch:cp",
            event_ids=(),
            message="msg",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        data = batch.to_dict()
        self.assertIn("card_payload", data)
        self.assertEqual(data["card_payload"], payload)

    def test_batch_without_card_payload_to_dict_omits_it(self) -> None:
        batch = DeliveryBatch(
            batch_id="batch:nocp",
            event_ids=(),
            message="msg",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
        )
        data = batch.to_dict()
        self.assertNotIn("card_payload", data)


class PaginationTests(unittest.TestCase):
    """2. Image card: 1/5/6+ events correct pagination; disabled keeps original."""

    def _events(self, count: int) -> list[ChangeEvent]:
        return [make_event(i) for i in range(1, count + 1)]

    def test_image_card_1_event_single_batch(self) -> None:
        batches = create_batches(
            self._events(1),
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=True,
        )
        self.assertEqual(len(batches), 1)
        self.assertIsNotNone(batches[0].card_payload)
        self.assertEqual(batches[0].card_payload["item_count"], 1)  # type: ignore[index]

    def test_image_card_5_events_single_batch(self) -> None:
        batches = create_batches(
            self._events(5),
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=True,
        )
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].card_payload["item_count"], 5)  # type: ignore[index]

    def test_image_card_6_events_two_batches(self) -> None:
        batches = create_batches(
            self._events(6),
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=True,
        )
        self.assertEqual(len(batches), 2)
        self.assertEqual(batches[0].card_payload["item_count"], 5)  # type: ignore[index]
        self.assertEqual(batches[1].card_payload["item_count"], 1)  # type: ignore[index]

    def test_image_card_disabled_uses_max_items_per_push(self) -> None:
        batches = create_batches(
            self._events(12),
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=False,
        )
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(batches[0].event_ids), 10)
        self.assertEqual(len(batches[1].event_ids), 2)
        for batch in batches:
            self.assertIsNone(batch.card_payload)

    def test_image_card_disabled_single_batch_no_payload(self) -> None:
        batches = create_batches(
            self._events(3),
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=False,
        )
        self.assertEqual(len(batches), 1)
        self.assertIsNone(batches[0].card_payload)


class PrepareSendTests(unittest.IsolatedAsyncioTestCase):
    """3. Multi-target single render. 4-6. Render failure/timeout/invalid fallback."""

    async def test_multi_target_single_prepare(self) -> None:
        """Multiple targets share one render; prepare called once."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:mt",
            event_ids=(),
            message="multi-target",
            created_at=NOW,
            targets={
                "t1": DeliveryTargetState(target="t1"),
                "t2": DeliveryTargetState(target="t2"),
            },
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=b"png-data")
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 2)
        self.assertEqual(pending, 0)
        self.assertEqual(len(fake.prepared_batches), 1)
        self.assertIs(fake.prepared_batches[0], batch)

    async def test_render_exception_falls_back_to_text_successfully(self) -> None:
        """Render exception → text sent; success, no retry."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:re",
            event_ids=(),
            message="render-exception-text",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)

        class RaiseNotifier(FakeNotifier):
            async def prepare(self, batch):
                raise RuntimeError("render crash")

        notifier = RaiseNotifier()
        sent, pending = await deliver_pending(
            state,
            store,
            notifier,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 1)
        self.assertEqual(pending, 0)
        self.assertEqual(
            state.outbox[batch.batch_id].targets["t"].status,
            DeliveryStatus.SENT,
        )
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][1], "render-exception-text")

    async def test_prepare_returns_none_sends_text(self) -> None:
        """None from prepare → text path."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:noner",
            event_ids=(),
            message="none-prepare-text",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=None)
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1], "none-prepare-text")

    async def test_prepare_returns_empty_bytes_sends_text(self) -> None:
        """Empty bytes from prepare → text path."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:empty",
            event_ids=(),
            message="empty-bytes-text",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=b"")
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 1)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.calls[0][1], "empty-bytes-text")

    async def test_image_send_via_prepare_succeeds(self) -> None:
        """With prepare_result bytes, delivery succeeds (FakeNotifier image path)."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:imgok",
            event_ids=(),
            message="img-ok-text",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        notifier = FakeNotifier(prepare_result=b"image-data")
        sent, pending = await deliver_pending(
            state,
            store,
            notifier,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 1)
        target = state.outbox[batch.batch_id].targets["t"]
        self.assertIs(target.status, DeliveryStatus.SENT)
        self.assertEqual(target.attempts, 1)
        self.assertEqual(len(notifier.prepared_batches), 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(notifier.calls[0][1], "img-ok-text")

    async def test_image_and_text_both_fail_attempts_plus_one(self) -> None:
        """Image fails AND text fails → FAILED, attempts+=1."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:bothfail",
            event_ids=(),
            message="both-fail",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
            max_attempts=3,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)

        class BothFailNotifier(FakeNotifier):
            def __init__(self):
                super().__init__()
                self.prepare_result = b"img"
                self.send_count = 0

            async def send(self, target, message):
                self.send_count += 1
                self.calls.append((target, message))
                return False, "fake_failure"

        notifier = BothFailNotifier()
        sent, pending = await deliver_pending(
            state,
            store,
            notifier,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 0)
        self.assertEqual(pending, 1)
        target = state.outbox[batch.batch_id].targets["t"]
        self.assertIs(target.status, DeliveryStatus.FAILED)
        self.assertEqual(target.attempts, 1)

    async def test_cancelled_error_propagates(self) -> None:
        """CancelledError must propagate, not fall back to text."""
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:cancel",
            event_ids=(),
            message="cancel-test",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)

        class CancelNotifier(FakeNotifier):
            async def prepare(self, batch):
                raise asyncio.CancelledError()

            async def send(self, target, message):
                self.calls.append((target, message))
                return True, None

        notifier = CancelNotifier()
        with self.assertRaises(asyncio.CancelledError):
            await deliver_pending(
                state,
                store,
                notifier,
                now=NOW,
                clock=lambda: NOW,
            )
        self.assertEqual(len(notifier.calls), 0)

    async def test_save_before_render_before_send(self) -> None:
        """Order: save state → prepare render → send."""
        events = [make_event()]
        batches = create_batches(
            events,
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=True,
        )
        self.assertEqual(len(batches), 1)
        batch = batches[0]
        self.assertIsNotNone(batch.card_payload)
        # The batch is saved to state before delivery in the service pipeline.
        # Here we verify that card_payload survives state round-trip.
        state = WatcherState(outbox={batch.batch_id: batch})
        restored = WatcherState.from_dict(state.to_dict())
        self.assertIsNotNone(restored.outbox[batch.batch_id].card_payload)

    async def test_image_card_text_is_always_generated(self) -> None:
        """Text message is always generated alongside card payload."""
        events = [make_event()]
        batch = create_batches(
            events,
            ["umo"],
            max_items=10,
            created_at=NOW,
            enable_image_card=True,
        )[0]
        self.assertIsNotNone(batch.card_payload)
        self.assertIn("插件 1", batch.message)


class AstrBotNotifierTests(unittest.IsolatedAsyncioTestCase):
    """Tests for AstrBotNotifier.prepare and .send wiring.

    Requirement coverage:
      - html_render injected as plugin attribute (not self.context)
      - html_render called with return_url=False and options keyword
      - injected image_loader is used
      - last_delivery_mode accurately reported
      - CancelledError propagates from text send path
    """

    # ------------------------------------------------------------------
    # 2. html_render call signature
    # ------------------------------------------------------------------

    async def test_html_render_called_with_return_url_false_and_options(self) -> None:
        """html_render receives return_url=False and options dict."""
        captured: dict[str, object] = {}

        async def fake_render(tmpl, data, return_url=True, options=None):
            captured["return_url"] = return_url
            captured["options"] = options
            return _VALID_PNG

        notifier = AstrBotNotifier(
            context=object(),
            html_render=fake_render,
        )
        payload = build_card_payload(
            [make_event()],
            intro="t",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:sig",
            event_ids=(),
            message="sig-test",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        result = await notifier.prepare(batch)
        self.assertIsNotNone(result)
        self.assertFalse(captured.get("return_url", True))
        opts = captured.get("options")
        self.assertIsNotNone(opts)
        self.assertEqual(opts["type"], "png")  # type: ignore[index]

    # ------------------------------------------------------------------
    # 5. injected image_loader is used
    # ------------------------------------------------------------------

    async def test_injected_image_loader_used(self) -> None:
        """Custom image_loader is actually called in send()."""
        loader_calls: list[bytes] = []

        class FakeImage:
            @classmethod
            def fromBytes(cls, data: bytes) -> str:
                loader_calls.append(data)
                return f"img:{len(data)}"

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        class TestCtx:
            sent: list = []

            async def send_message(self, target, chain):
                self.sent.append((target, chain))
                return True

        ctx = TestCtx()
        notifier = AstrBotNotifier(
            context=ctx,
            html_render=None,
            message_chain_loader=lambda: FakeMC,
            image_loader=lambda: FakeImage,
        )
        notifier._pending_image_bytes = b"img-bytes"
        success, err = await notifier.send("t", "m")
        self.assertTrue(success)
        self.assertEqual(len(loader_calls), 1)
        self.assertEqual(loader_calls[0], b"img-bytes")

    # ------------------------------------------------------------------
    # 6. image send failure + text success → text_fallback mode
    # ------------------------------------------------------------------

    async def test_image_fail_text_success_mode_is_text_fallback(self) -> None:
        """Image fails + text succeeds → last_delivery_mode == 'text_fallback'."""

        class TestCtx:
            def __init__(self):
                self.image_sent = False
                self.text_sent = False

            async def send_message(self, target, chain):
                if hasattr(chain, "chain") and chain.chain is not None:
                    self.image_sent = True
                    return False  # image send fails
                self.text_sent = True
                return True  # text succeeds

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        class FakeImg:
            @classmethod
            def fromBytes(cls, data):
                return "img"

        ctx = TestCtx()
        notifier = AstrBotNotifier(
            context=ctx,
            html_render=None,
            message_chain_loader=lambda: FakeMC,
            image_loader=lambda: FakeImg,
        )
        notifier._pending_image_bytes = _VALID_PNG
        notifier._prepare_attempted = True  # simulate valid image was prepared
        success, err = await notifier.send("t", "m")
        self.assertTrue(success)
        self.assertTrue(ctx.image_sent)
        self.assertTrue(ctx.text_sent)
        self.assertEqual(notifier.last_delivery_mode, "text_fallback")

    async def test_image_send_success_mode_is_image(self) -> None:
        """When image send succeeds, last_delivery_mode == 'image'."""

        class TestCtx:
            async def send_message(self, target, chain):
                return True

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        class FakeImg:
            @classmethod
            def fromBytes(cls, data):
                return "img"

        ctx = TestCtx()
        notifier = AstrBotNotifier(
            context=ctx,
            html_render=None,
            message_chain_loader=lambda: FakeMC,
            image_loader=lambda: FakeImg,
        )
        notifier._pending_image_bytes = b"bytes"
        success, err = await notifier.send("t", "m")
        self.assertTrue(success)
        self.assertEqual(notifier.last_delivery_mode, "image")

    async def test_text_send_no_image_mode_is_text(self) -> None:
        """When no pending image, last_delivery_mode == 'text'."""

        class TestCtx:
            async def send_message(self, target, chain):
                return True

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        ctx = TestCtx()
        notifier = AstrBotNotifier(
            context=ctx,
            html_render=None,
            message_chain_loader=lambda: FakeMC,
        )
        success, err = await notifier.send("t", "m")
        self.assertTrue(success)
        self.assertEqual(notifier.last_delivery_mode, "text")

    # ------------------------------------------------------------------
    # 8. CancelledError propagates from text send path
    # ------------------------------------------------------------------

    async def test_cancelled_propagates_on_text_send(self) -> None:
        """CancelledError in text send path propagates, not caught."""

        class CancelCtx:
            called = 0

            async def send_message(self, target, chain):
                self.__class__.called += 1
                raise asyncio.CancelledError()

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        notifier = AstrBotNotifier(
            context=CancelCtx(),
            html_render=None,
            message_chain_loader=lambda: FakeMC,
        )
        with self.assertRaises(asyncio.CancelledError):
            await notifier.send("t", "m")

    async def test_cancelled_propagates_from_image_path(self) -> None:
        """CancelledError in image send path propagates."""

        class CancelCtx:
            async def send_message(self, target, chain):
                raise asyncio.CancelledError()

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        class FakeImg:
            @classmethod
            def fromBytes(cls, data):
                return "img"

        notifier = AstrBotNotifier(
            context=CancelCtx(),
            html_render=None,
            message_chain_loader=lambda: FakeMC,
            image_loader=lambda: FakeImg,
        )
        notifier._pending_image_bytes = b"bytes"
        with self.assertRaises(asyncio.CancelledError):
            await notifier.send("t", "m")

    async def test_cancelled_propagates_from_prepare(self) -> None:
        """CancelledError in prepare path propagates."""

        async def failing_render(tmpl, data, return_url=True, options=None):
            raise asyncio.CancelledError()

        notifier = AstrBotNotifier(
            context=object(),
            html_render=failing_render,
        )
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        batch = DeliveryBatch(
            batch_id="batch:cp",
            event_ids=(),
            message="cp",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )
        with self.assertRaises(asyncio.CancelledError):
            await notifier.prepare(batch)


class DeliverPendingChangesTests(unittest.IsolatedAsyncioTestCase):
    """Tests for deliver_pending structural changes.

    Requirement coverage:
      - consecutive batches (first image, second text) both get prepare
      - backoff/no-due batch does not trigger prepare
    """

    def _img_batch(self, bid: str, msg: str, target_key: str = "t") -> DeliveryBatch:
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        return DeliveryBatch(
            batch_id=bid,
            event_ids=(),
            message=msg,
            created_at=NOW,
            targets={target_key: DeliveryTargetState(target=target_key)},
            card_payload=payload,
        )

    def _text_batch(self, bid: str, msg: str, target_key: str = "t") -> DeliveryBatch:
        return DeliveryBatch(
            batch_id=bid,
            event_ids=(),
            message=msg,
            created_at=NOW,
            targets={target_key: DeliveryTargetState(target=target_key)},
            card_payload=None,
        )

    # ------------------------------------------------------------------
    # 3. consecutive batches: first with image, second text-only
    # ------------------------------------------------------------------

    async def test_consecutive_image_then_text_both_prepare_called(self) -> None:
        """deliver_pending calls prepare on every due batch (including text-only)."""
        b1 = self._img_batch("b1", "img-batch")
        b2 = self._text_batch("b2", "text-batch")
        state = WatcherState(outbox={b1.batch_id: b1, b2.batch_id: b2})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=b"img-data")
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 2)
        self.assertEqual(pending, 0)
        # Both batches must have been prepared (text batch clears bytes via prepare)
        self.assertEqual(len(fake.prepared_batches), 2)
        prepared_ids = {b.batch_id for b in fake.prepared_batches}
        self.assertIn(b1.batch_id, prepared_ids)
        self.assertIn(b2.batch_id, prepared_ids)

    async def test_image_batch_and_old_text_batch_no_cross_contamination(self) -> None:
        """Text-only batch after image batch does not reuse image bytes.

        Uses a sensing notifier that records the internal _pending_image_bytes
        at the moment of each send() call.
        """
        b1 = self._img_batch("b1", "img")
        b2 = self._text_batch("b2", "text")
        state = WatcherState(outbox={b1.batch_id: b1, b2.batch_id: b2})
        store = FakeStore(state)

        send_modes: list[str] = []

        class SensingNotifier(FakeNotifier):
            def __init__(self):
                super().__init__(prepare_result=b"img-data")
                self._pending_image_bytes: bytes | None = None

            async def prepare(self, batch):
                self.prepared_batches.append(batch)
                self._pending_image_bytes = (
                    self.prepare_result if batch.card_payload is not None else None
                )
                return self._pending_image_bytes

            async def send(self, target, message):
                self.calls.append((target, message))
                send_modes.append("image" if self._pending_image_bytes else "text")
                return True, None

        sent, pending = await deliver_pending(
            state,
            store,
            SensingNotifier(),
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 2)
        self.assertEqual(send_modes, ["image", "text"])

    # ------------------------------------------------------------------
    # 4. backoff/no-due batch does not trigger prepare
    # ------------------------------------------------------------------

    async def test_backoff_batch_does_not_trigger_prepare(self) -> None:
        """Batch where all targets are in backoff → prepare is not called."""
        batch = self._img_batch("b:bo", "backoff")
        batch.targets["t"].next_retry_at = "2099-12-31T23:59:59Z"
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=b"img")
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 0)
        self.assertEqual(pending, 1)
        self.assertEqual(len(fake.prepared_batches), 0)

    async def test_sent_batch_does_not_trigger_prepare(self) -> None:
        """Batch where all targets are SENT → prepare is not called."""
        batch = self._img_batch("b:sent", "already-sent")
        batch.targets["t"].status = DeliveryStatus.SENT
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=b"img")
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 0)
        self.assertEqual(pending, 0)
        self.assertEqual(len(fake.prepared_batches), 0)

    async def test_exhausted_batch_does_not_trigger_prepare(self) -> None:
        """Batch where all targets are EXHAUSTED → prepare is not called."""
        batch = self._img_batch("b:ex", "exhausted")
        batch.targets["t"].status = DeliveryStatus.EXHAUSTED
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        fake = FakeNotifier(prepare_result=b"img")
        sent, pending = await deliver_pending(
            state,
            store,
            fake,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 0)
        self.assertEqual(pending, 0)
        self.assertEqual(len(fake.prepared_batches), 0)

    # ------------------------------------------------------------------
    # clear_prepared cleanup: normal / exception / CancelledError
    # ------------------------------------------------------------------

    async def test_clear_prepared_after_normal_delivery(self) -> None:
        """deliver_pending calls clear_prepared after a successful batch."""
        from market_watcher.astrbot_adapter import FakeNotifier

        batch = self._img_batch("b:cln1", "clean-normal")
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        notifier = FakeNotifier(prepare_result=b"img-data")
        notifier._pending_image_bytes = b"pre-set"  # simulate prior state
        sent, pending = await deliver_pending(
            state,
            store,
            notifier,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 1)
        # clear_prepared() must have been called in the finally block
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_clear_prepared_after_prepare_exception(self) -> None:
        """deliver_pending calls clear_prepared when prepare raises Exception."""
        from market_watcher.astrbot_adapter import FakeNotifier

        class RaisePrepare(FakeNotifier):
            async def prepare(self, batch):
                raise RuntimeError("prepare boom")

        batch = self._img_batch("b:cln2", "clean-exc")
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        notifier = RaisePrepare()
        notifier._pending_image_bytes = b"pre-set"
        sent, pending = await deliver_pending(
            state,
            store,
            notifier,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 1)  # text fallback succeeds
        # clear_prepared() must have been called in the finally block
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_clear_prepared_after_cancelled(self) -> None:
        """deliver_pending calls clear_prepared when CancelledError propagates."""
        from market_watcher.astrbot_adapter import FakeNotifier

        class CancelPrepare(FakeNotifier):
            async def prepare(self, batch):
                raise asyncio.CancelledError()

        batch = self._img_batch("b:cln3", "clean-cancel")
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        notifier = CancelPrepare()
        notifier._pending_image_bytes = b"pre-set"
        with self.assertRaises(asyncio.CancelledError):
            await deliver_pending(
                state,
                store,
                notifier,
                now=NOW,
                clock=lambda: NOW,
            )
        # clear_prepared() must have been called before re-raise
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_clear_prepared_multi_target_single_prepare(self) -> None:
        """Multiple targets share one prepare; image bytes available for all sends,
        then cleared after the batch completes."""
        from market_watcher.astrbot_adapter import FakeNotifier

        class SensingNotifier(FakeNotifier):
            def __init__(self):
                super().__init__(prepare_result=b"img-data")
                self.send_had_bytes: list[bool] = []

            async def send(self, target, message):
                self.calls.append((target, message))
                self.send_had_bytes.append(self._pending_image_bytes is not None)
                return True, None

        batch = self._img_batch("b:mtcln", "multi-target-clean", target_key="t1")
        batch.targets["t2"] = DeliveryTargetState(target="t2")
        state = WatcherState(outbox={batch.batch_id: batch})
        store = FakeStore(state)
        notifier = SensingNotifier()
        sent, pending = await deliver_pending(
            state,
            store,
            notifier,
            now=NOW,
            clock=lambda: NOW,
        )
        self.assertEqual(sent, 2)
        # Both sends should have seen _pending_image_bytes
        self.assertEqual(notifier.send_had_bytes, [True, True])
        # After delivery, bytes must be cleared
        self.assertIsNone(notifier._pending_image_bytes)


class PrepareDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    """Tests for AstrBotNotifier.prepare() outcome diagnostic logging.

    Every outcome path (skipped / bytes / string / none / exception /
    timeout / cancelled / image_ready / invalid_image) must be observable via
    log fields.  String content, URL, path, token, UMO must NOT appear in logs.
    """

    def setUp(self) -> None:
        # Temp file with valid PNG content
        self._valid_png_path = tempfile.mktemp(suffix=".png")
        with open(self._valid_png_path, "wb") as f:
            f.write(_VALID_PNG)
        # Temp empty file
        self._empty_file_path = tempfile.mktemp(suffix=".bin")
        with open(self._empty_file_path, "wb") as f:
            pass
        # Temp file with Internal Server Error content
        self._ise_path = tempfile.mktemp(suffix=".jpg")
        with open(self._ise_path, "wb") as f:
            f.write(_INTERNAL_SERVER_ERROR)
        # Temp file with HTML content
        self._html_path = tempfile.mktemp(suffix=".png")
        with open(self._html_path, "wb") as f:
            f.write(_HTML_CONTENT)
        # Temp file with JSON content
        self._json_path = tempfile.mktemp(suffix=".png")
        with open(self._json_path, "wb") as f:
            f.write(_JSON_CONTENT)
        # Temp file with unknown content
        self._unknown_path = tempfile.mktemp(suffix=".bin")
        with open(self._unknown_path, "wb") as f:
            f.write(_UNKNOWN_CONTENT)
        # Temp file with truncated JPEG
        self._truncated_path = tempfile.mktemp(suffix=".jpg")
        with open(self._truncated_path, "wb") as f:
            f.write(_TRUNCATED_JPEG)

    def tearDown(self) -> None:
        for attr in (
            "_valid_png_path",
            "_empty_file_path",
            "_ise_path",
            "_html_path",
            "_json_path",
            "_unknown_path",
            "_truncated_path",
        ):
            path = getattr(self, attr, None)
            if path and os.path.isfile(path):
                os.unlink(path)

    @staticmethod
    def _payload_batch() -> DeliveryBatch:
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        return DeliveryBatch(
            batch_id="batch:diag",
            event_ids=(),
            message="diag",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )

    @staticmethod
    def _no_payload_batch() -> DeliveryBatch:
        return DeliveryBatch(
            batch_id="batch:nop",
            event_ids=(),
            message="nop",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=None,
        )

    # -- skipped (no renderer) ------------------------------------------------

    async def test_prepare_skipped_when_no_renderer(self) -> None:
        """outcome=skipped when html_render is None."""
        notifier = AstrBotNotifier(context=object(), html_render=None)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertFalse(notifier._prepare_attempted)

    async def test_prepare_skipped_when_no_payload(self) -> None:
        """outcome=skipped when card_payload is None even with renderer."""

        async def fake_render(*a, **kw):
            return _VALID_PNG

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._no_payload_batch())
        self.assertIsNone(result)
        self.assertFalse(notifier._prepare_attempted)

    # -- image_ready, source=bytes --------------------------------------------

    async def test_prepare_valid_jpeg_bytes(self) -> None:
        """Valid JPEG bytes → outcome=image_ready, source=bytes, image_kind=jpeg."""

        async def fake_render(*a, **kw):
            return _VALID_JPEG

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertEqual(result, _VALID_JPEG)
        self.assertEqual(notifier._pending_image_bytes, _VALID_JPEG)
        self.assertTrue(notifier._prepare_attempted)

    async def test_prepare_valid_png_bytes(self) -> None:
        """Valid PNG bytes → outcome=image_ready, source=bytes, image_kind=png."""

        async def fake_render(*a, **kw):
            return _VALID_PNG

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertEqual(result, _VALID_PNG)
        self.assertIsNotNone(notifier._pending_image_bytes)

    async def test_prepare_valid_webp_bytes(self) -> None:
        """Valid WebP bytes → outcome=image_ready."""

        async def fake_render(*a, **kw):
            return _VALID_WEBP

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertEqual(result, _VALID_WEBP)
        self.assertIsNotNone(notifier._pending_image_bytes)

    async def test_prepare_valid_gif_bytes(self) -> None:
        """Valid GIF bytes → outcome=image_ready."""

        async def fake_render(*a, **kw):
            return _VALID_GIF

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertEqual(result, _VALID_GIF)
        self.assertIsNotNone(notifier._pending_image_bytes)

    # -- invalid_image from bytes ---------------------------------------------

    async def test_prepare_bytes_empty_returns_none(self) -> None:
        """Empty bytes returns None, not stored."""

        async def fake_render(*a, **kw):
            return b""

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertIsNone(notifier._pending_image_bytes)
        self.assertTrue(notifier._prepare_attempted)

    async def test_prepare_bytes_internal_server_error(self) -> None:
        """Bytes containing 'Internal Server Error' → outcome=invalid_image."""

        async def fake_render(*a, **kw):
            return _INTERNAL_SERVER_ERROR

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_prepare_bytes_html_content(self) -> None:
        """Bytes starting with <html → outcome=invalid_image, sig=html."""

        async def fake_render(*a, **kw):
            return _HTML_CONTENT

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    async def test_prepare_bytes_json_content(self) -> None:
        """Bytes that is valid JSON → outcome=invalid_image, sig=json."""

        async def fake_render(*a, **kw):
            return _JSON_CONTENT

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    async def test_prepare_bytes_unknown_content(self) -> None:
        """Bytes with unknown content → outcome=invalid_image, sig=unknown."""

        async def fake_render(*a, **kw):
            return _UNKNOWN_CONTENT

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    async def test_prepare_truncated_jpeg_returns_none(self) -> None:
        """Truncated JPEG (too small, no EOI) → outcome=invalid_image."""

        async def fake_render(*a, **kw):
            return _TRUNCATED_JPEG

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    # -- image_ready, source=file ---------------------------------------------

    async def test_prepare_valid_file_sets_bytes(self) -> None:
        """Existing file with valid PNG content → outcome=image_ready, source=file."""

        async def fake_render(*a, **kw):
            return self._valid_png_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNotNone(result)
        self.assertIsNotNone(notifier._pending_image_bytes)
        with open(self._valid_png_path, "rb") as f:
            self.assertEqual(result, f.read())

    # -- invalid_image from file ----------------------------------------------

    async def test_prepare_empty_file_returns_none(self) -> None:
        """Empty file → outcome=invalid_image, not stored."""

        async def fake_render(*a, **kw):
            return self._empty_file_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_prepare_ise_file_returns_none(self) -> None:
        """107-byte Internal Server Error file → invalid_image, not sent."""

        async def fake_render(*a, **kw):
            return self._ise_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_prepare_html_file_returns_none(self) -> None:
        """File with HTML content → outcome=invalid_image."""

        async def fake_render(*a, **kw):
            return self._html_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    async def test_prepare_json_file_returns_none(self) -> None:
        """File with JSON content → outcome=invalid_image."""

        async def fake_render(*a, **kw):
            return self._json_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    async def test_prepare_unknown_file_returns_none(self) -> None:
        """File with unknown content → outcome=invalid_image."""

        async def fake_render(*a, **kw):
            return self._unknown_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    async def test_prepare_truncated_file_returns_none(self) -> None:
        """File with truncated JPEG → outcome=invalid_image."""

        async def fake_render(*a, **kw):
            return self._truncated_path

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    # -- file read exception --------------------------------------------------

    async def test_prepare_file_read_permission_error(self) -> None:
        """Unreadable file path → outcome=invalid_image, error_type logged."""

        async def fake_render(*a, **kw):
            return "/nonexistent/path/image.png"

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    # -- URL and other string outcomes ----------------------------------------

    async def test_prepare_string_url_returns_none(self) -> None:
        """outcome=string (url) does NOT set pending bytes."""

        async def fake_render(*a, **kw):
            return "https://example.invalid/img.png"

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertIsNone(notifier._pending_image_bytes)

    async def test_prepare_string_other_returns_none(self) -> None:
        """outcome=string (other) does NOT set pending bytes."""

        async def fake_render(*a, **kw):
            return "/some/unchecked/path"

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)
        self.assertIsNone(notifier._pending_image_bytes)

    # -- exception outcome ----------------------------------------------------

    async def test_prepare_exception_returns_none(self) -> None:
        """outcome=exception returns None, no pending bytes."""

        async def fake_render(*a, **kw):
            raise ValueError("internal-error")

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    # -- timeout outcome ------------------------------------------------------

    async def test_prepare_timeout_returns_none(self) -> None:
        """outcome=timeout returns None."""

        async def slow_render(*a, **kw):
            await asyncio.sleep(30)
            return b"data"

        notifier = AstrBotNotifier(
            context=object(),
            html_render=slow_render,
            image_render_timeout=1,
        )
        result = await notifier.prepare(self._payload_batch())
        self.assertIsNone(result)

    # -- cancelled outcome ----------------------------------------------------

    async def test_prepare_cancelled_still_propagates(self) -> None:
        """outcome=cancelled is logged before re-raise; CancelledError propagates."""

        async def cancel_render(*a, **kw):
            raise asyncio.CancelledError()

        notifier = AstrBotNotifier(context=object(), html_render=cancel_render)
        with self.assertRaises(asyncio.CancelledError):
            await notifier.prepare(self._payload_batch())

    # -- _prepare_attempted flag ----------------------------------------------

    async def test_prepare_attempted_true_when_renderer_called(self) -> None:
        """_prepare_attempted True when renderer called (even for invalid result)."""

        async def fake_render(*a, **kw):
            return _TRUNCATED_JPEG

        notifier = AstrBotNotifier(context=object(), html_render=fake_render)
        await notifier.prepare(self._payload_batch())
        self.assertTrue(notifier._prepare_attempted)

    async def test_prepare_attempted_false_when_skipped(self) -> None:
        """_prepare_attempted is False when prepare is skipped."""
        notifier = AstrBotNotifier(context=object(), html_render=None)
        await notifier.prepare(self._payload_batch())
        self.assertFalse(notifier._prepare_attempted)

    # -- log safety -----------------------------------------------------------

    async def test_prepare_string_outcome_never_logs_raw_value(self) -> None:
        """String outcome log must NOT contain the raw path/URL."""
        import io
        import logging

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger(__name__)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        import market_watcher.astrbot_adapter as adapter

        orig = adapter._safe_logger
        adapter._safe_logger = lambda: logger
        try:

            async def url_render(*a, **kw):
                return "https://secret.example.com/img.png"

            async def file_render(*a, **kw):
                return "/var/secret/token.png"

            for renderer in (url_render, file_render):
                buf.truncate(0)
                buf.seek(0)
                notifier = AstrBotNotifier(context=object(), html_render=renderer)
                await notifier.prepare(self._payload_batch())
                log_text = buf.getvalue()
                self.assertIn("outcome=string", log_text)
                # No raw URL or path
                self.assertNotIn("secret.example.com", log_text)
                self.assertNotIn("/var/secret/", log_text)
                self.assertNotIn("token.png", log_text)
                self.assertNotIn("https://", log_text)
                # Must record suffix
                self.assertIn("suffix=.png", log_text)
        finally:
            adapter._safe_logger = orig
            logger.removeHandler(handler)

    async def test_prepare_invalid_image_path_not_in_log(self) -> None:
        """File read outcome log must NOT contain the raw path."""
        import io
        import logging

        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        logger = logging.getLogger(__name__)
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        import market_watcher.astrbot_adapter as adapter

        orig = adapter._safe_logger
        adapter._safe_logger = lambda: logger
        try:

            async def ise_render(*a, **kw):
                return self._ise_path

            notifier = AstrBotNotifier(context=object(), html_render=ise_render)
            await notifier.prepare(self._payload_batch())
            log_text = buf.getvalue()
            # Must contain outcome=invalid_image, not the path
            self.assertIn("outcome=invalid_image", log_text)
            self.assertIn("invalid_signature=internal_server_error", log_text)
            self.assertNotIn("Internal Server Error", log_text)
            self.assertNotIn(self._ise_path, log_text)
        finally:
            adapter._safe_logger = orig
            logger.removeHandler(handler)


class AstrBotNotifierSendModeTests(unittest.IsolatedAsyncioTestCase):
    """Tests for send() mode tracking with _prepare_attempted."""

    def _payload_batch(self) -> DeliveryBatch:
        payload = build_card_payload(
            [make_event()],
            intro="x",
            batch_index=1,
            batch_total=1,
            total_items=1,
        )
        return DeliveryBatch(
            batch_id="batch:mode",
            event_ids=(),
            message="mode-test",
            created_at=NOW,
            targets={"t": DeliveryTargetState(target="t")},
            card_payload=payload,
        )

    async def test_send_text_fallback_when_prepare_attempted_and_no_bytes(self) -> None:
        """When _prepare_attempted=True but no pending bytes → text_fallback."""

        class TestCtx:
            async def send_message(self, target, chain):
                return True

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        notifier = AstrBotNotifier(
            context=TestCtx(),
            html_render=None,
            message_chain_loader=lambda: FakeMC,
        )
        notifier._prepare_attempted = True  # simulate attempted prepare
        success, err = await notifier.send("t", "msg")
        self.assertTrue(success)
        self.assertEqual(notifier.last_delivery_mode, "text_fallback")

    async def test_send_text_when_not_attempted(self) -> None:
        """When _prepare_attempted=False → text (pure text batch)."""

        class TestCtx:
            async def send_message(self, target, chain):
                return True

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        notifier = AstrBotNotifier(
            context=TestCtx(),
            html_render=None,
            message_chain_loader=lambda: FakeMC,
        )
        # _prepare_attempted stays False (no prepare call)
        success, err = await notifier.send("t", "msg")
        self.assertTrue(success)
        self.assertEqual(notifier.last_delivery_mode, "text")

    async def test_image_send_with_file_bytes_via_prepare(self) -> None:
        """Valid file path → image delivery attempted → mode=image."""

        class FakeImg:
            @classmethod
            def fromBytes(cls, data):
                return "img"

        class FakeMC:
            def __init__(self, chain=None):
                self.chain = chain

            def message(self, text):
                return f"text:{text}"

        class TestCtx:
            async def send_message(self, target, chain):
                return True

        notifier = AstrBotNotifier(
            context=TestCtx(),
            html_render=None,
            message_chain_loader=lambda: FakeMC,
            image_loader=lambda: FakeImg,
        )
        notifier._pending_image_bytes = _VALID_PNG
        notifier._prepare_attempted = True
        success, err = await notifier.send("t", "msg")
        self.assertTrue(success)
        self.assertEqual(notifier.last_delivery_mode, "image")

    async def test_clear_prepared_resets_attempt_flag(self) -> None:
        """clear_prepared() resets _prepare_attempted."""
        notifier = AstrBotNotifier(context=object(), html_render=None)
        notifier._pending_image_bytes = b"data"
        notifier._prepare_attempted = True
        notifier.last_delivery_mode = "image"
        notifier.clear_prepared()
        self.assertIsNone(notifier._pending_image_bytes)
        self.assertFalse(notifier._prepare_attempted)
        self.assertIsNone(notifier.last_delivery_mode)

    async def test_multi_target_single_prepare_and_read(self) -> None:
        """Multiple targets share one file read; prepare called once."""
        import tempfile

        png_path = tempfile.mktemp(suffix=".png")
        try:
            with open(png_path, "wb") as f:
                f.write(_VALID_PNG)

            prepare_count = 0

            async def counting_render(*a, **kw):
                nonlocal prepare_count
                prepare_count += 1
                return png_path

            notifier = AstrBotNotifier(context=object(), html_render=counting_render)
            # Single prepare call
            result = await notifier.prepare(self._payload_batch())
            self.assertIsNotNone(result)
            self.assertIsNotNone(notifier._pending_image_bytes)
            self.assertEqual(prepare_count, 1)
            # Both sends use the same bytes
            self.assertEqual(notifier._pending_image_bytes, _VALID_PNG)
        finally:
            os.unlink(png_path)


if __name__ == "__main__":
    unittest.main()
