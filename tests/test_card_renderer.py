from __future__ import annotations

import json
import re
import unittest

from jinja2.sandbox import SandboxedEnvironment

from market_watcher.card_renderer import (
    CARD_TEMPLATE,
    DESCRIPTION_MAX_LENGTH,
    NAME_MAX_LENGTH,
    build_card_payload,
    build_render_request,
)
from market_watcher.models import (
    ChangeEvent,
    ChangeKind,
    PluginRecord,
    SourceEvidence,
    SourceKind,
)

NOW = "2026-07-21T10:00:00Z"


def make_event(
    kind: ChangeKind = ChangeKind.DISCOVERED,
    *,
    index: int = 1,
    **record_values: object,
) -> ChangeEvent:
    defaults: dict[str, object] = {
        "canonical_id": f"github:owner/plugin-{index}",
        "name": f"plugin-{index}",
        "display_name": f"插件 {index}",
        "description": "一项清晰、可靠的市场插件更新。",
        "author": "AstrBot 社区",
        "version": "1.2.3",
        "platforms": ("aiocqhttp", "telegram"),
        "tags": ("工具", "自动化"),
        "market_status": "已上架",
        "stars": 128,
        "forks": 14,
        "evidence": (
            SourceEvidence(
                SourceKind.MARKET,
                f"plugin-{index}",
                f"https://example.invalid/plugin-{index}",
                NOW,
            ),
            SourceEvidence(
                SourceKind.GITHUB_DISCOVERY,
                f"owner/plugin-{index}",
                f"https://github.com/owner/plugin-{index}",
                NOW,
            ),
        ),
    }
    defaults.update(record_values)
    current = PluginRecord(**defaults)  # type: ignore[arg-type]
    return ChangeEvent(
        event_id=f"event:{kind.value}:{index}",
        kind=kind,
        canonical_id=current.canonical_id,
        current=current,
        previous=None,
        changed_fields=("description", "version") if kind is ChangeKind.UPDATED else (),
        detected_at=NOW,
    )


def payload_for(events: list[ChangeEvent]) -> dict[str, object]:
    return build_card_payload(
        events,
        intro="本次市场新增与更新值得关注。",
        batch_index=1,
        batch_total=2,
        total_items=7,
    )


class CardPayloadTests(unittest.TestCase):
    def test_discovered_snapshot_contains_all_designed_fields(self) -> None:
        payload = payload_for([make_event()])
        item = payload["items"][0]  # type: ignore[index]

        self.assertEqual(item["kind"], "discovered")
        self.assertEqual(item["status_text"], "新发现")
        self.assertEqual(item["name"], "插件 1")
        self.assertEqual(item["version"], "1.2.3")
        self.assertEqual(item["author"], "AstrBot 社区")
        self.assertEqual(item["stars"], 128)
        self.assertEqual(item["forks"], 14)
        self.assertEqual(item["platforms"], ["aiocqhttp", "telegram"])
        self.assertEqual(item["tags"], ["工具", "自动化"])
        self.assertEqual(item["market_status"], "已上架")
        self.assertEqual(item["sources"], ["AstrBot 市场", "GitHub 补充发现"])
        self.assertNotIn("changed_fields", item)

    def test_updated_snapshot_maps_changed_fields(self) -> None:
        item = payload_for([make_event(ChangeKind.UPDATED)])["items"][0]  # type: ignore[index]

        self.assertEqual(item["kind"], "updated")
        self.assertEqual(item["status_text"], "有更新")
        self.assertEqual(item["changed_fields"], ["描述", "版本"])

    def test_minimal_record_omits_empty_optional_fields(self) -> None:
        event = make_event(
            display_name=None,
            description=None,
            author=None,
            version=None,
            platforms=(),
            tags=(),
            market_status=None,
            stars=None,
            forks=None,
            evidence=(),
        )
        item = payload_for([event])["items"][0]  # type: ignore[index]

        self.assertEqual(
            item,
            {"kind": "discovered", "status_text": "新发现", "name": "plugin-1"},
        )
        self.assertNotIn("None", json.dumps(item, ensure_ascii=False))

    def test_long_and_hostile_text_is_sanitized_and_truncated(self) -> None:
        event = make_event(
            display_name="<b>超长插件</b>" + "名" * 100 + "🚀",
            description=(
                "<script>alert('x')</script><p>安全描述</p>"
                "[CQ:at,qq=all]" + "文" * 400 + "🔥"
            ),
            author="<img src=x onerror=alert(1)>作者🙂",
            tags=("<strong>效率</strong>", "[CQ:image,file=x]", "✨视觉"),
        )
        item = payload_for([event])["items"][0]  # type: ignore[index]

        self.assertLessEqual(len(item["name"]), NAME_MAX_LENGTH)
        self.assertLessEqual(len(item["description"]), DESCRIPTION_MAX_LENGTH)
        self.assertTrue(item["name"].endswith("…"))
        self.assertTrue(item["description"].endswith("…"))
        serialized = json.dumps(item, ensure_ascii=False)
        for unsafe in ("<script", "<img", "[CQ:", "🚀", "🔥", "🙂", "✨"):
            self.assertNotIn(unsafe, serialized)
        self.assertIn("安全描述", item["description"])
        self.assertEqual(item["author"], "作者")
        self.assertEqual(item["tags"], ["效率", "视觉"])

    def test_five_events_are_accepted_and_six_are_rejected(self) -> None:
        five = [make_event(index=index) for index in range(1, 6)]
        self.assertEqual(payload_for(five)["item_count"], 5)

        with self.assertRaisesRegex(ValueError, "at most 5 events"):
            payload_for([*five, make_event(index=6)])

    def test_payload_survives_json_round_trip(self) -> None:
        payload = payload_for([make_event(), make_event(ChangeKind.UPDATED, index=2)])
        encoded = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(json.loads(encoded), payload)


class CardTemplateTests(unittest.TestCase):
    def test_render_request_contract(self) -> None:
        payload = payload_for([make_event()])
        template, data, options = build_render_request(payload)

        self.assertIs(template, CARD_TEMPLATE)
        self.assertEqual(data, {"card": payload})
        self.assertEqual(
            options,
            {"full_page": True, "type": "png", "quality": 90},
        )

    def test_template_is_self_contained_and_matches_visual_contract(self) -> None:
        lowered = CARD_TEMPLATE.lower()

        self.assertIn("width: 760px", lowered)
        self.assertIn("#e8e5de", lowered)
        self.assertIn("#ffffff", lowered)
        self.assertIn("#3346a8", lowered)
        self.assertIn("#23845b", lowered)
        self.assertIn("#b97818", lowered)
        for forbidden in (
            "<script",
            "<link",
            "<img",
            "@import",
            "http://",
            "https://",
            "url(",
            "gradient(",
            "|safe",
        ):
            self.assertNotIn(forbidden, lowered)

    def test_every_printed_jinja_expression_is_escaped(self) -> None:
        expressions = re.findall(r"{{(.*?)}}", CARD_TEMPLATE, flags=re.DOTALL)

        self.assertGreater(len(expressions), 10)
        for expression in expressions:
            with self.subTest(expression=expression.strip()):
                self.assertRegex(expression, r"\|\s*e\s*$")

    def test_real_sandbox_renderer_contract_for_supported_card_shapes(self) -> None:
        minimal = make_event(
            index=3,
            display_name=None,
            description=None,
            author=None,
            version=None,
            platforms=(),
            tags=(),
            market_status=None,
            stars=None,
            forks=None,
            evidence=(),
        )
        cases = (
            ("discovered", [make_event()], ("新发现", "插件 1")),
            (
                "updated",
                [make_event(ChangeKind.UPDATED, index=2)],
                ("有更新", "插件 2", "变化字段", "描述", "版本"),
            ),
            ("minimal", [minimal], ("新发现", "plugin-3")),
            (
                "five events",
                [make_event(index=index) for index in range(1, 6)],
                ("本批 5 项", "插件 1", "插件 2", "插件 3", "插件 4", "插件 5"),
            ),
        )

        for name, events, expected_texts in cases:
            with self.subTest(case=name):
                payload = payload_for(events)
                template, data, _options = build_render_request(payload)
                rendered = SandboxedEnvironment().from_string(template).render(data)

                self.assertTrue(rendered.lstrip().startswith("<!doctype html>"))
                self.assertIn("</html>", rendered)
                self.assertEqual(rendered.count('<article class="event '), len(events))
                for text in expected_texts:
                    self.assertIn(text, rendered)


if __name__ == "__main__":
    unittest.main()
