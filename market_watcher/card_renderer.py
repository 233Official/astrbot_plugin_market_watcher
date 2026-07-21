"""Build bounded, JSON-safe data for the market change image card."""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from enum import Enum
from html.parser import HTMLParser
from typing import Any

from .models import ChangeEvent, ChangeKind, SourceKind

MAX_EVENTS_PER_CARD = 5
INTRO_MAX_LENGTH = 180
NAME_MAX_LENGTH = 72
VERSION_MAX_LENGTH = 36
AUTHOR_MAX_LENGTH = 48
DESCRIPTION_MAX_LENGTH = 260
CHIP_MAX_LENGTH = 32
STATUS_MAX_LENGTH = 40

SOURCE_LABELS = {
    SourceKind.MARKET.value: "AstrBot 市场",
    SourceKind.COLLECTION_ISSUE.value: "Collection Issue",
    SourceKind.LEGACY_PUBLISH_ISSUE.value: "主仓旧 Issue",
    SourceKind.GITHUB_DISCOVERY.value: "GitHub 补充发现",
}

FIELD_LABELS = {
    "display_name": "展示名",
    "description": "描述",
    "author": "作者",
    "version": "版本",
    "repo_url": "仓库地址",
    "astrbot_version": "AstrBot 兼容版本",
    "platforms": "支持平台",
    "tags": "标签",
    "market_status": "市场状态",
    "issue_state": "Issue 状态",
    "issue_labels": "Issue 标签",
    "archived": "归档状态",
}

_CQ_CODE = re.compile(r"(?i)\[CQ:[^\]\r\n]{0,512}\]")
_EMOJI = re.compile(
    "["
    "\U0001f1e6-\U0001f1ff"
    "\U0001f300-\U0001faff"
    "\U00002600-\U000026ff"
    "\U00002700-\U000027bf"
    "\U0001f3fb-\U0001f3ff"
    "\u200d\ufe0e\ufe0f\u20e3"
    "]+"
)


class _TextExtractor(HTMLParser):
    """Extract visible text while discarding executable or metadata elements."""

    _HIDDEN_TAGS = {"script", "style", "template", "noscript", "svg", "math"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.lower() in self._HIDDEN_TAGS:
            self.hidden_depth += 1

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del tag, attrs

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._HIDDEN_TAGS and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)


def build_card_payload(
    events: Iterable[ChangeEvent],
    *,
    intro: str,
    batch_index: int,
    batch_total: int,
    total_items: int,
) -> dict[str, object]:
    """Return a bounded presentation snapshot containing only JSON primitives."""

    event_list = list(events)
    if len(event_list) > MAX_EVENTS_PER_CARD:
        raise ValueError(
            "card payload accepts at most 5 events; paginate events upstream"
        )
    _validate_batch(batch_index, batch_total, total_items)

    safe_intro = _clean_text(intro, INTRO_MAX_LENGTH)
    payload: dict[str, object] = {
        "title": "插件市场变更简报",
        "intro": safe_intro or "本次市场监测到新的插件动态。",
        "batch_index": batch_index,
        "batch_total": batch_total,
        "total_items": total_items,
        "item_count": len(event_list),
        "items": [_event_snapshot(event) for event in event_list],
    }
    return payload


def build_render_request(
    payload: dict[str, object],
) -> tuple[str, dict[str, object], dict[str, object]]:
    """Return the self-contained template, renderer data, and screenshot options."""

    return (
        CARD_TEMPLATE,
        {"card": payload},
        {"full_page": True, "type": "png", "quality": 90},
    )


def _event_snapshot(event: ChangeEvent) -> dict[str, object]:
    record = event.current
    kind = _kind_value(event.kind)
    name = _first_text(
        getattr(record, "display_name", None),
        getattr(record, "name", None),
        getattr(event, "canonical_id", None),
        limit=NAME_MAX_LENGTH,
    )
    item: dict[str, object] = {
        "kind": kind,
        "status_text": "新发现" if kind == ChangeKind.DISCOVERED.value else "有更新",
        "name": name or "未命名插件",
    }

    _add_text(item, "version", getattr(record, "version", None), VERSION_MAX_LENGTH)
    _add_text(item, "author", getattr(record, "author", None), AUTHOR_MAX_LENGTH)
    _add_text(
        item,
        "description",
        getattr(record, "description", None),
        DESCRIPTION_MAX_LENGTH,
    )
    _add_count(item, "stars", getattr(record, "stars", None))
    _add_count(item, "forks", getattr(record, "forks", None))
    _add_text(
        item,
        "market_status",
        getattr(record, "market_status", None),
        STATUS_MAX_LENGTH,
    )

    platforms = _clean_list(getattr(record, "platforms", ()), CHIP_MAX_LENGTH, 6)
    tags = _clean_list(getattr(record, "tags", ()), CHIP_MAX_LENGTH, 6)
    changed_fields = _changed_fields(getattr(event, "changed_fields", ()))
    sources = _sources(record)
    if platforms:
        item["platforms"] = platforms
    if tags:
        item["tags"] = tags
    if changed_fields:
        item["changed_fields"] = changed_fields
    if sources:
        item["sources"] = sources
    return item


def _kind_value(value: object) -> str:
    if value is ChangeKind.DISCOVERED or value == ChangeKind.DISCOVERED.value:
        return ChangeKind.DISCOVERED.value
    return ChangeKind.UPDATED.value


def _validate_batch(batch_index: int, batch_total: int, total_items: int) -> None:
    if type(batch_index) is not int or batch_index < 1:
        raise ValueError("batch_index must be a positive integer")
    if type(batch_total) is not int or batch_total < 1:
        raise ValueError("batch_total must be a positive integer")
    if batch_index > batch_total:
        raise ValueError("batch_index must not exceed batch_total")
    if type(total_items) is not int or total_items < 0:
        raise ValueError("total_items must be a non-negative integer")


def _add_text(
    target: dict[str, object],
    key: str,
    value: object,
    limit: int,
) -> None:
    cleaned = _clean_text(value, limit)
    if cleaned:
        target[key] = cleaned


def _add_count(target: dict[str, object], key: str, value: object) -> None:
    if type(value) is int and value >= 0:
        target[key] = value


def _first_text(*values: object, limit: int) -> str | None:
    for value in values:
        cleaned = _clean_text(value, limit)
        if cleaned:
            return cleaned
    return None


def _clean_list(values: object, limit: int, max_items: int) -> list[str]:
    if isinstance(values, (str, bytes)) or values is None:
        candidates: Iterable[object] = (values,) if values is not None else ()
    else:
        try:
            candidates = iter(values)  # type: ignore[arg-type]
        except TypeError:
            candidates = (values,)
    result: list[str] = []
    for value in candidates:
        cleaned = _clean_text(value, limit)
        if cleaned and cleaned not in result:
            result.append(cleaned)
        if len(result) == max_items:
            break
    return result


def _changed_fields(values: object) -> list[str]:
    if isinstance(values, str):
        candidates: Iterable[object] = (values,)
    else:
        try:
            candidates = iter(values)  # type: ignore[arg-type]
        except TypeError:
            candidates = ()
    result: list[str] = []
    for value in candidates:
        raw = value.value if isinstance(value, Enum) else value
        key = _clean_text(raw, CHIP_MAX_LENGTH)
        if not key:
            continue
        label = FIELD_LABELS.get(key, key.replace("_", " "))
        label = _clean_text(label, CHIP_MAX_LENGTH)
        if label and label not in result:
            result.append(label)
        if len(result) == 8:
            break
    return result


def _sources(record: object) -> list[str]:
    evidence = list(getattr(record, "evidence", ()) or ())
    field_sources = getattr(record, "field_sources", {}) or {}
    if isinstance(field_sources, dict):
        evidence.extend(field_sources.values())

    labels: set[str] = set()
    for item in evidence:
        source_kind = getattr(item, "source_kind", None)
        raw = source_kind.value if isinstance(source_kind, Enum) else source_kind
        value = _clean_text(raw, CHIP_MAX_LENGTH)
        if not value:
            continue
        labels.add(SOURCE_LABELS.get(value, value.replace("_", " ")))
    order = {label: index for index, label in enumerate(SOURCE_LABELS.values())}
    return sorted(labels, key=lambda label: (order.get(label, len(order)), label))[:4]


def _clean_text(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
        text = " ".join(parser.parts)
    except (ValueError, AssertionError):
        text = re.sub(r"<[^>]*>", " ", text)
    text = _CQ_CODE.sub(" ", text)
    text = _EMOJI.sub("", text)
    text = "".join(
        character
        for character in text
        if not unicodedata.category(character).startswith("C")
    )
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text or None


CARD_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ card.title|e }}</title>
  <style>
    * { box-sizing: border-box; }
    html, body { margin: 0; width: 760px; background: #e8e5de; }
    body {
      color: #22263a;
      font-family: "Avenir Next", "PingFang SC", "Microsoft YaHei", sans-serif;
      -webkit-font-smoothing: antialiased;
    }
    .canvas {
      width: 760px;
      padding: 30px;
      background: #e8e5de;
    }
    .sheet {
      overflow: hidden;
      border: 1px solid #d8d5cf;
      border-radius: 24px;
      background: #ffffff;
      box-shadow: 0 18px 46px rgba(34, 38, 58, 0.13);
    }
    .header {
      position: relative;
      padding: 30px 32px 26px;
      border-bottom: 1px solid #e6e7ee;
    }
    .header::before {
      position: absolute;
      top: 0;
      left: 32px;
      width: 84px;
      height: 5px;
      background: #3346a8;
      content: "";
    }
    .eyebrow {
      margin: 0 0 10px;
      color: #3346a8;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0.18em;
    }
    h1 {
      margin: 0;
      color: #202b6b;
      font-family: "Iowan Old Style", "Songti SC", serif;
      font-size: 34px;
      line-height: 1.18;
      letter-spacing: -0.035em;
    }
    .intro {
      max-width: 570px;
      margin: 14px 0 0;
      color: #616579;
      font-size: 15px;
      line-height: 1.75;
    }
    .batch {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-top: 22px;
      padding-top: 16px;
      border-top: 1px solid #ececf1;
      color: #777a8b;
      font-size: 12px;
      font-weight: 700;
    }
    .batch strong { color: #2d3678; font-size: 13px; }
    .events { padding: 10px 18px 18px; }
    .event {
      position: relative;
      margin-top: 10px;
      padding: 21px 22px 20px 25px;
      border: 1px solid #e5e6ec;
      border-radius: 17px;
      background: #ffffff;
    }
    .event::before {
      position: absolute;
      top: 18px;
      bottom: 18px;
      left: 0;
      width: 4px;
      border-radius: 0 4px 4px 0;
      content: "";
    }
    .event--discovered::before { background: #23845b; }
    .event--updated::before { background: #b97818; }
    .event-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
    }
    .title-block { min-width: 0; }
    .status {
      display: inline-block;
      margin-bottom: 8px;
      padding: 4px 9px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
    }
    .event--discovered .status { color: #176a47; background: #e8f5ee; }
    .event--updated .status { color: #8a570c; background: #fff2d9; }
    h2 {
      margin: 0;
      color: #242942;
      font-size: 20px;
      line-height: 1.35;
      letter-spacing: -0.02em;
    }
    .byline { margin-top: 6px; color: #747789; font-size: 12px; }
    .version {
      flex: 0 0 auto;
      padding: 7px 10px;
      border: 1px solid #dfe1ea;
      border-radius: 9px;
      color: #414b8e;
      background: #f7f7fb;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      font-weight: 700;
    }
    .description {
      margin: 13px 0 0;
      color: #55596d;
      font-size: 13px;
      line-height: 1.7;
    }
    .facts {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 16px;
      margin-top: 14px;
      color: #686c7f;
      font-size: 12px;
    }
    .fact strong { margin-left: 4px; color: #2d324e; }
    .rows { margin-top: 14px; border-top: 1px solid #eff0f4; }
    .row {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      padding-top: 11px;
    }
    .row-label {
      flex: 0 0 62px;
      padding-top: 4px;
      color: #8a8d9c;
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
    }
    .chips { display: flex; flex-wrap: wrap; gap: 6px; }
    .chip {
      padding: 4px 8px;
      border: 1px solid #e1e3eb;
      border-radius: 7px;
      color: #50546a;
      background: #f7f7f9;
      font-size: 11px;
      line-height: 1.35;
    }
    .chip--changed { color: #7a520f; border-color: #ead9b8; background: #fff8e9; }
    .chip--source { color: #34417f; border-color: #d9dded; background: #f3f5fb; }
    .footer {
      display: flex;
      justify-content: space-between;
      padding: 0 32px 25px;
      color: #9294a1;
      font-size: 10px;
      letter-spacing: 0.06em;
    }
  </style>
</head>
<body>
  <main class="canvas">
    <section class="sheet">
      <header class="header">
        <p class="eyebrow">MARKET WATCHER</p>
        <h1>{{ card.title|e }}</h1>
        <p class="intro">{{ card.intro|e }}</p>
        <div class="batch">
          <strong>第 {{ card.batch_index|e }} / {{ card.batch_total|e }} 批</strong>
          <span>本批 {{ card.item_count|e }} 项 · 全部 {{ card.total_items|e }} 项</span>
        </div>
      </header>
      <div class="events">
        {% for item in card.get("items", []) %}
          {% if item.kind == "discovered" %}
            <article class="event event--discovered">
          {% else %}
            <article class="event event--updated">
          {% endif %}
            <div class="event-head">
              <div class="title-block">
                <span class="status">{{ item.status_text|e }}</span>
                <h2>{{ item.name|e }}</h2>
                {% if item.get("author") %}
                  <div class="byline">作者 · {{ item.get("author")|e }}</div>
                {% endif %}
              </div>
              {% if item.get("version") %}
                <span class="version">{{ item.get("version")|e }}</span>
              {% endif %}
            </div>
            {% if item.get("description") %}
              <p class="description">{{ item.get("description")|e }}</p>
            {% endif %}
            {% if item.stars is defined or item.forks is defined or item.get("market_status") %}
              <div class="facts">
                {% if item.stars is defined %}
                  <span class="fact">STARS <strong>{{ item.stars|e }}</strong></span>
                {% endif %}
                {% if item.forks is defined %}
                  <span class="fact">FORKS <strong>{{ item.forks|e }}</strong></span>
                {% endif %}
                {% if item.get("market_status") %}
                  <span class="fact">市场 <strong>{{ item.get("market_status")|e }}</strong></span>
                {% endif %}
              </div>
            {% endif %}
            {% if item.get("platforms") or item.get("tags") or item.get("changed_fields") or item.get("sources") %}
              <div class="rows">
                {% if item.get("platforms") or item.get("tags") %}
                  <div class="row">
                    <div class="row-label">平台 / 标签</div>
                    <div class="chips">
                      {% for platform in item.get("platforms", []) %}
                        <span class="chip">{{ platform|e }}</span>
                      {% endfor %}
                      {% for tag in item.get("tags", []) %}
                        <span class="chip">{{ tag|e }}</span>
                      {% endfor %}
                    </div>
                  </div>
                {% endif %}
                {% if item.get("changed_fields") %}
                  <div class="row">
                    <div class="row-label">变化字段</div>
                    <div class="chips">
                      {% for field in item.get("changed_fields", []) %}
                        <span class="chip chip--changed">{{ field|e }}</span>
                      {% endfor %}
                    </div>
                  </div>
                {% endif %}
                {% if item.get("sources") %}
                  <div class="row">
                    <div class="row-label">来源</div>
                    <div class="chips">
                      {% for source in item.get("sources", []) %}
                        <span class="chip chip--source">{{ source|e }}</span>
                      {% endfor %}
                    </div>
                  </div>
                {% endif %}
              </div>
            {% endif %}
          </article>
        {% endfor %}
      </div>
      <footer class="footer">
        <span>ASTRBOT PLUGIN ECOSYSTEM</span>
        <span>自动监测 · 事实快照</span>
      </footer>
    </section>
  </main>
</body>
</html>
"""


__all__ = ["CARD_TEMPLATE", "build_card_payload", "build_render_request"]
