"""Deterministic Chinese fact summaries without AI dependencies."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from .models import ChangeEvent, ChangeKind, SourceKind

MAX_MESSAGE_LENGTH = 3500

SOURCE_NAMES = {
    SourceKind.MARKET: "AstrBot 市场",
    SourceKind.COLLECTION_ISSUE: "Collection Issue",
    SourceKind.LEGACY_PUBLISH_ISSUE: "主仓旧 Issue",
    SourceKind.GITHUB_DISCOVERY: "GitHub 补充发现",
}
FIELD_NAMES = {
    "display_name": "展示名",
    "description": "用途描述",
    "author": "作者",
    "version": "版本",
    "repo_url": "仓库地址",
    "astrbot_version": "AstrBot 兼容版本",
    "platforms": "支持平台",
    "market_status": "市场状态",
    "issue_state": "Issue 状态",
    "issue_labels": "Issue 标签",
    "archived": "仓库归档状态",
}


def chunk_events(events: list[ChangeEvent], max_items: int) -> list[list[ChangeEvent]]:
    size = max(1, max_items)
    return [events[index : index + size] for index in range(0, len(events), size)]


def render_summary(
    events: list[ChangeEvent],
    batch_index: int,
    batch_total: int,
    *,
    total_items: int | None = None,
) -> str:
    lines = [
        "AstrBot 插件市场变化",
        f"第 {batch_index}/{batch_total} 批，共 {total_items or len(events)} 项",
    ]
    for event in events:
        record = event.current
        event_name = "新增" if event.kind is ChangeKind.DISCOVERED else "实质更新"
        title = _safe_text(record.display_name or record.name, 80)
        item_lines = [f"\n- 【{event_name}】{title}"]
        if record.version:
            item_lines.append(f"  版本：{_safe_text(record.version, 40)}")
        if record.description:
            item_lines.append(f"  用途：{_safe_text(record.description, 240)}")
        if record.github_metadata_status != "disabled":
            if record.stars is None:
                item_lines.append("  Star：未知")
            elif record.github_metadata_fetched_at:
                stale = (
                    "，stale"
                    if record.github_metadata_status not in {"fresh", "ok"}
                    else ""
                )
                item_lines.append(
                    "  Star："
                    f"{record.stars}（缓存于 "
                    f"{record.github_metadata_fetched_at}{stale}）"
                )
            else:
                item_lines.append(f"  Star（当前或缓存值）：{record.stars}")
        if event.changed_fields:
            changed = [FIELD_NAMES.get(item, item) for item in event.changed_fields]
            item_lines.append(f"  变化字段：{', '.join(changed)}")
        sources = sorted({SOURCE_NAMES[item.source_kind] for item in record.evidence})
        if sources:
            item_lines.append(f"  来源：{', '.join(sources)}")
        links = []
        for evidence in record.evidence:
            link = _safe_url(evidence.source_url)
            if link and link not in links:
                links.append(link)
        if links:
            item_lines.append(f"  链接：{links[0]}")
        candidate = "\n".join((*lines, *item_lines))
        if len(candidate) > MAX_MESSAGE_LENGTH:
            lines.append("\n（其余内容因长度限制已省略）")
            break
        lines.extend(item_lines)
    rendered = "\n".join(lines)
    if len(rendered) > MAX_MESSAGE_LENGTH:
        return rendered[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return rendered


def _safe_text(value: str, limit: int) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    value = re.sub(r"\s+", " ", value).strip()
    translations = str.maketrans(
        {"@": "＠", "[": "［", "]": "］", "`": "ˋ", "*": "＊", "_": "＿"}
    )
    value = value.translate(translations)
    if value.startswith(("-", "+", ">", "#")):
        value = "·" + value[1:]
    if len(value) > limit:
        value = value[: max(0, limit - 1)].rstrip() + "…"
    return value or "未命名插件"


def _safe_url(value: str) -> str | None:
    value = re.sub(r"[\x00-\x20\x7f]+", "", str(value))[:500]
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    return (
        value.replace("@", "%40")
        .replace("[", "%5B")
        .replace("]", "%5D")
        .replace("`", "%60")
        .replace("*", "%2A")
        .replace("_", "%5F")
    )
