"""Pure local status rendering and safe state diagnostics."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import RuntimeConfig
from .models import DeliveryStatus, SourceKind, SourceState, WatcherState

_STATE_BYTE_FIELDS = (
    "sources",
    "plugins",
    "github",
    "outbox",
    "id_aliases",
    "image_delivery_windows",
)
_STATE_COUNT_FIELDS = (
    "source_observations",
    "snapshots",
    "snapshot_observations",
    "plugins",
    "github_repos",
    "outbox_batches",
    "pending_targets",
    "aliases",
    "image_windows",
)
_SAFE_ERROR_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


@dataclass(frozen=True, slots=True)
class StateDiagnostics:
    """Safe, non-content state size and count diagnostics."""

    state_file_bytes: int | None
    field_bytes: dict[str, int] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    available: bool = True


def compact_json_bytes(value: Any) -> int:
    """Return compact UTF-8 JSON size without exposing the serialized value."""
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def collect_state_diagnostics(
    state: WatcherState, state_path: Path | None = None
) -> StateDiagnostics:
    """Collect safe state metrics with one in-memory ``to_dict`` traversal.

    The returned byte sizes are calculated per top-level serialized field.  No
    serialized content is returned, logged, or rendered by this module.
    """
    disk_bytes = _state_file_bytes(state_path)
    try:
        data = state.to_dict()
        field_bytes = {
            key: compact_json_bytes(value) for key, value in sorted(data.items())
        }
        counts = {
            "source_observations": sum(
                len(source.observations) for source in state.sources.values()
            ),
            "snapshots": sum(
                len(source.snapshots) for source in state.sources.values()
            ),
            "snapshot_observations": sum(
                len(snapshot.observations)
                for source in state.sources.values()
                for snapshot in source.snapshots.values()
            ),
            "plugins": len(state.plugins),
            "github_repos": len(state.github.repos),
            "outbox_batches": len(state.outbox),
            "pending_targets": sum(
                target.status in {DeliveryStatus.PENDING, DeliveryStatus.FAILED}
                for batch in state.outbox.values()
                for target in batch.targets.values()
            ),
            "aliases": len(state.id_aliases),
            "image_windows": len(state.image_delivery_windows),
        }
    except Exception:
        return StateDiagnostics(
            state_file_bytes=disk_bytes,
            available=False,
        )
    return StateDiagnostics(
        state_file_bytes=disk_bytes,
        field_bytes=field_bytes,
        counts=counts,
    )


def unavailable_state_diagnostics(state_path: Path | None = None) -> StateDiagnostics:
    """Return a non-throwing fallback when state metrics cannot be collected."""
    return StateDiagnostics(
        state_file_bytes=_state_file_bytes(state_path),
        available=False,
    )


def format_source_states(sources: dict[str, SourceState]) -> str:
    """Render per-source status using only controlled state fields."""
    entries: list[str] = []
    for key, source in sorted(sources.items(), key=lambda item: str(item[0])):
        source_key = _source_key(key)
        if source_key is None:
            continue
        entries.append(
            f"{source_key}:complete={'yes' if source.complete else 'no'},"
            f"failed={'no' if source.complete else 'yes'},"
            f"error_code={_safe_error_code(source.error_code)},"
            f"last_success_at={source.last_success_at or 'none'},"
            f"observations={len(source.observations)},"
            f"snapshots={len(source.snapshots)}"
        )
    return "; ".join(entries) or "暂无"


def format_source_errors(sources: dict[str, SourceState]) -> str:
    """Render only ``SourceKind`` keys and safe ``error_code`` values."""
    entries: list[str] = []
    for key, source in sorted(sources.items(), key=lambda item: str(item[0])):
        source_key = _source_key(key)
        if source_key is None:
            continue
        entries.append(f"{source_key}:{_safe_error_code(source.error_code)}")
    return ", ".join(entries)


def _source_key(value: object) -> str | None:
    try:
        return SourceKind(value).value
    except (TypeError, ValueError):
        return None


def _safe_error_code(value: object) -> str:
    if value is None:
        return "none"
    if type(value) is str and _SAFE_ERROR_CODE.fullmatch(value):
        return value
    return "invalid"


def _state_file_bytes(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        if not path.exists():
            return 0
        return path.stat().st_size
    except FileNotFoundError:
        return 0
    except OSError:
        return None


def _format_state_field_bytes(diagnostics: StateDiagnostics) -> str:
    if not diagnostics.available or not diagnostics.field_bytes:
        return "unavailable"
    return ", ".join(
        f"{key}={diagnostics.field_bytes.get(key, 'unavailable')}"
        for key in _STATE_BYTE_FIELDS
    )


def _format_state_counts(diagnostics: StateDiagnostics) -> str:
    if not diagnostics.available or not diagnostics.counts:
        return "unavailable"
    return ", ".join(
        f"{key}={diagnostics.counts.get(key, 'unavailable')}"
        for key in _STATE_COUNT_FIELDS
    )


def format_status(
    *,
    runtime: RuntimeConfig,
    enabled_sources: set[SourceKind],
    scheduler_state: str,
    scheduler_last_attempt: str | None,
    scheduler_last_success: str | None,
    scheduler_error: str,
    drain_scheduler_state: str,
    drain_last_attempt: str | None,
    drain_last_success: str | None,
    drain_error: str,
    service_busy: bool,
    health: str,
    schema_version: str,
    plugin_count: int,
    source_states: str,
    github_remaining: str,
    github_reset: str,
    github_cache_count: int,
    configured_target_count: int,
    subscription_count: int,
    effective_target_count: int,
    pending: int,
    exhausted: int,
    throttled: int,
    next_drain: str,
    last_text: str,
    plugin_version: str = "unknown",
    state_diagnostics: StateDiagnostics | None = None,
) -> str:
    """Render status without exposing configured target or token values."""
    source_flags = {
        SourceKind.MARKET: "market",
        SourceKind.GITHUB_DISCOVERY: "github_discovery",
    }
    switches = ", ".join(
        f"{label}={'enabled' if kind in enabled_sources else 'disabled'}"
        for kind, label in source_flags.items()
    )
    image_card = (
        f"图片卡片：{'enabled' if runtime.enable_image_card else 'disabled'}"
        f"（超时：{runtime.image_render_timeout_seconds}s"
        f"，单图事件数：{runtime.image_card_max_items}"
        f"，每窗口上限：{runtime.max_images_per_delivery}"
        f"，发送间隔：{runtime.image_send_interval_seconds}s）"
    )
    drain_status = (
        f"出站箱调度：{drain_scheduler_state}"
        f"（上次尝试：{drain_last_attempt or '无'}，"
        f"上次成功：{drain_last_success or '无'}，"
        f"错误：{drain_error}）"
    )
    diagnostics = state_diagnostics or StateDiagnostics(
        state_file_bytes=None,
        available=False,
    )
    state_file_bytes = (
        str(diagnostics.state_file_bytes)
        if diagnostics.state_file_bytes is not None
        else "unknown"
    )
    return "\n".join(
        (
            "AstrBot 插件市场观察器",
            f"- 插件版本：{plugin_version}",
            f"- 配置启用：{'enabled' if runtime.enabled else 'disabled'}",
            f"- 自动检查调度：{scheduler_state}",
            f"- {drain_status}",
            f"- 服务忙碌：{'yes' if service_busy else 'no'}",
            f"- 轮询间隔：{runtime.poll_interval_minutes} 分钟",
            f"- 检查调度上次尝试：{scheduler_last_attempt or '无'}",
            f"- 检查调度上次成功：{scheduler_last_success or '无'}",
            f"- 检查调度错误：{scheduler_error}",
            f"- 状态健康：{health}",
            f"- 状态 schema_version：{schema_version}",
            f"- 插件记录：{plugin_count}",
            f"- 来源开关：{switches}",
            f"- 来源状态：{source_states}",
            f"- 状态文件磁盘 bytes：{state_file_bytes}",
            f"- 状态字段 JSON bytes：{_format_state_field_bytes(diagnostics)}",
            f"- 状态计数：{_format_state_counts(diagnostics)}",
            f"- GitHub Token：{'已配置' if runtime.github_token else '未配置'}",
            f"- 配置目标数：{configured_target_count}",
            f"- 群订阅数：{subscription_count}",
            f"- 本轮有效目标数：{effective_target_count}",
            f"- GitHub 剩余/重置：{github_remaining}/{github_reset}",
            f"- GitHub 缓存：{github_cache_count}",
            f"- {image_card}",
            f"- 待投递目标：{pending}",
            f"- 窗口节流目标：{throttled}",
            f"- 下次出站箱投递：{next_drain}",
            f"- 永久失败目标：{exhausted}",
            last_text,
        )
    )
