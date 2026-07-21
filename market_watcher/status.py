"""Pure local status rendering for the AstrBot command adapter."""

from __future__ import annotations

from .config import RuntimeConfig
from .models import SourceKind


def format_status(
    *,
    runtime: RuntimeConfig,
    enabled_sources: set[SourceKind],
    scheduler_state: str,
    scheduler_last_attempt: str | None,
    scheduler_last_success: str | None,
    scheduler_error: str,
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
    last_text: str,
) -> str:
    """Render status without exposing configured target or token values."""
    source_flags = {
        SourceKind.MARKET: "market",
        SourceKind.COLLECTION_ISSUE: "collection_issue",
        SourceKind.LEGACY_PUBLISH_ISSUE: "legacy_publish_issue",
        SourceKind.GITHUB_DISCOVERY: "github_discovery",
    }
    switches = ", ".join(
        f"{label}={'enabled' if kind in enabled_sources else 'disabled'}"
        for kind, label in source_flags.items()
    )
    return "\n".join(
        (
            "AstrBot 插件市场观察器",
            f"- 配置启用：{'enabled' if runtime.enabled else 'disabled'}",
            f"- 自动调度：{scheduler_state}",
            f"- 服务忙碌：{'yes' if service_busy else 'no'}",
            f"- 轮询间隔：{runtime.poll_interval_minutes} 分钟",
            f"- 调度上次尝试：{scheduler_last_attempt or '无'}",
            f"- 调度上次成功：{scheduler_last_success or '无'}",
            f"- 调度错误：{scheduler_error}",
            f"- 状态健康：{health}",
            f"- 状态 schema_version：{schema_version}",
            f"- 插件记录：{plugin_count}",
            f"- 来源开关：{switches}",
            f"- 来源状态：{source_states}",
            f"- GitHub Token：{'已配置' if runtime.github_token else '未配置'}",
            f"- 配置目标数：{configured_target_count}",
            f"- 群订阅数：{subscription_count}",
            f"- 本轮有效目标数：{effective_target_count}",
            f"- GitHub 剩余/重置：{github_remaining}/{github_reset}",
            f"- GitHub 缓存：{github_cache_count}",
            f"- 待投递目标：{pending}",
            f"- 永久失败目标：{exhausted}",
            last_text,
        )
    )
