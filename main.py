from __future__ import annotations

from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

if __package__:
    from .market_watcher.ai import AI_DIAGNOSTIC_FACTS, AiIntroService
    from .market_watcher.astrbot_adapter import AstrBotAiClient, AstrBotNotifier
    from .market_watcher.astrbot_handler_compat import normalize_plugin_handler_bindings
    from .market_watcher.config import RuntimeConfig, parse_runtime_config
    from .market_watcher.github import GitHubGateway, GitHubMetadataService
    from .market_watcher.github_diagnostic import (
        format_github_diagnostic,
        run_github_diagnostic,
    )
    from .market_watcher.http import (
        AioHttpClient,
        GitHubAuthHttpClient,
        HttpClient,
        RetryingHttpClient,
    )
    from .market_watcher.models import RunReport, SourceKind
    from .market_watcher.outbox import (
        count_exhausted,
        count_pending,
        merge_targets,
        validate_targets,
    )
    from .market_watcher.scheduler import FixedDelayScheduler
    from .market_watcher.service import MarketWatcherService
    from .market_watcher.sources.github_search import GitHubSearchFetcher
    from .market_watcher.sources.issues import IssuesFetcher
    from .market_watcher.sources.market import MarketFetcher
    from .market_watcher.state import JsonStateStore, StateError
    from .market_watcher.status import format_status
    from .market_watcher.subscriptions import subscription_status, update_subscription
else:
    from market_watcher.ai import AI_DIAGNOSTIC_FACTS, AiIntroService
    from market_watcher.astrbot_adapter import AstrBotAiClient, AstrBotNotifier
    from market_watcher.astrbot_handler_compat import normalize_plugin_handler_bindings
    from market_watcher.config import RuntimeConfig, parse_runtime_config
    from market_watcher.github import GitHubGateway, GitHubMetadataService
    from market_watcher.github_diagnostic import (
        format_github_diagnostic,
        run_github_diagnostic,
    )
    from market_watcher.http import (
        AioHttpClient,
        GitHubAuthHttpClient,
        HttpClient,
        RetryingHttpClient,
    )
    from market_watcher.models import RunReport, SourceKind
    from market_watcher.outbox import (
        count_exhausted,
        count_pending,
        merge_targets,
        validate_targets,
    )
    from market_watcher.scheduler import FixedDelayScheduler
    from market_watcher.service import MarketWatcherService
    from market_watcher.sources.github_search import GitHubSearchFetcher
    from market_watcher.sources.issues import IssuesFetcher
    from market_watcher.sources.market import MarketFetcher
    from market_watcher.state import JsonStateStore, StateError
    from market_watcher.status import format_status
    from market_watcher.subscriptions import subscription_status, update_subscription


@register(
    "astrbot_plugin_market_watcher",
    "233Official",
    "聚合 AstrBot 插件市场与 GitHub 发布信号的监控插件",
    "1.0.0",
)
class MarketWatcherPlugin(Star):
    """M3 market checks with manual commands and fixed-delay scheduling."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._http: HttpClient | None = None
        self._store: JsonStateStore | None = None
        self._notifier: AstrBotNotifier | None = None
        self._ai_client: AstrBotAiClient | None = None
        self._ai_intro: AiIntroService | None = None
        self._service: MarketWatcherService | None = None
        self._scheduler: FixedDelayScheduler | None = None
        self._runtime_config: RuntimeConfig | None = None

    async def initialize(self) -> None:
        normalized_handlers = normalize_plugin_handler_bindings(self)
        if normalized_handlers > 0:
            logger.info(
                "[MarketWatcher] normalized %s stale AstrBot handler bindings",
                normalized_handlers,
            )
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_market_watcher"))
        self._store = JsonStateStore(data_dir / "state.json")
        runtime = self._runtime()
        token = runtime.github_token
        headers = {"User-Agent": "astrbot-plugin-market-watcher"}
        base_http = AioHttpClient(
            timeout_seconds=runtime.request_timeout_seconds,
            default_headers=headers,
        )
        auth_http = GitHubAuthHttpClient(base_http, token)
        retrying_http = RetryingHttpClient(auth_http)
        github_gateway = GitHubGateway(retrying_http, auth_http)
        self._http = github_gateway
        fetchers = {
            SourceKind.MARKET: MarketFetcher(self._http),
            SourceKind.COLLECTION_ISSUE: IssuesFetcher(
                self._http,
                source_kind=SourceKind.COLLECTION_ISSUE,
            ),
            SourceKind.LEGACY_PUBLISH_ISSUE: IssuesFetcher(
                self._http,
                source_kind=SourceKind.LEGACY_PUBLISH_ISSUE,
            ),
            SourceKind.GITHUB_DISCOVERY: GitHubSearchFetcher(self._http),
        }
        self._notifier = AstrBotNotifier(self.context)
        self._ai_client = AstrBotAiClient(
            self.context, timeout_seconds=runtime.ai_timeout_seconds
        )
        self._ai_intro = AiIntroService(self._ai_client)
        self._service = MarketWatcherService(
            store=self._store,
            fetchers=fetchers,
            notifier=self._notifier,
            github_gateway=github_gateway,
            github_metadata=GitHubMetadataService(
                self._http, github_gateway, clock=lambda: self._service_clock()
            ),
            ai_intro=self._ai_intro,
            observer=self._observe_run,
        )
        self._scheduler = FixedDelayScheduler(
            self._automatic_check,
            lambda: self._runtime().poll_interval_minutes * 60,
            on_error=lambda code: logger.error(
                "[MarketWatcher] scheduler error: %s", code
            ),
        )
        if runtime.enabled:
            self._scheduler.start()
        logger.info("[MarketWatcher] M3 市场检查与 fixed-delay 调度已就绪")

    async def terminate(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
            self._scheduler = None
        if self._http is not None:
            await self._http.close()
            self._http = None
        self._service = None
        self._notifier = None
        self._ai_intro = None
        self._ai_client = None
        logger.info("[MarketWatcher] 插件已停止")

    @filter.command_group("marketwatch")
    def marketwatch(self) -> None:
        """AstrBot 插件市场观察器命令组。"""

    @marketwatch.command("status")
    async def status(self, event: AstrMessageEvent):
        if self._store is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        runtime = self._runtime()
        try:
            state = self._store.load()
            health = "正常"
            schema_version = str(state.schema_version)
            plugin_count = len(state.plugins)
            pending = count_pending(state)
            exhausted = count_exhausted(state)
            source_states = (
                ", ".join(
                    f"{key}:"
                    f"{'ok' if value.complete else value.error_code or 'incomplete'}"
                    for key, value in sorted(state.sources.items())
                )
                or "暂无"
            )
            github_remaining = state.github.rate_limit.remaining
            github_reset = state.github.rate_limit.reset_at or "未知"
            github_cache_count = len(state.github.repos)
            subscription_count = len(state.subscriptions)
            configured_targets, _ = validate_targets(runtime.push_targets)
            effective_targets, _ = merge_targets(
                runtime.push_targets, state.subscriptions
            )
            stored_report = state.last_run
        except StateError as exc:
            health = type(exc).__name__
            schema_version = "不可用"
            plugin_count = 0
            pending = 0
            exhausted = 0
            source_states = "不可用"
            github_remaining = None
            github_reset = "未知"
            github_cache_count = 0
            subscription_count = 0
            configured_targets = []
            effective_targets = []
            stored_report = {}
        last = self._service.last_report if self._service else None
        if last is None and stored_report:
            try:
                last = RunReport(**stored_report)
            except TypeError:
                last = None
        last_text = last.to_chinese() if last else "暂无运行报告"
        scheduler = self._scheduler.status if self._scheduler else None
        scheduler_text = (
            "disabled"
            if not runtime.enabled
            else "running"
            if self._scheduler is not None
            and self._scheduler.task is not None
            and not self._scheduler.task.done()
            else "stopped"
        )
        scheduler_error = (
            scheduler.last_error_code
            if scheduler and scheduler.last_error_code
            else "无"
        )
        github_remaining_text = (
            str(github_remaining) if github_remaining is not None else "未知"
        )
        yield event.plain_result(
            format_status(
                runtime=runtime,
                enabled_sources=self._enabled_sources(),
                scheduler_state=scheduler_text,
                scheduler_last_attempt=scheduler.last_attempt_at if scheduler else None,
                scheduler_last_success=scheduler.last_success_at if scheduler else None,
                scheduler_error=scheduler_error,
                service_busy=bool(self._service and self._service.lock.locked()),
                health=health,
                schema_version=schema_version,
                plugin_count=plugin_count,
                source_states=source_states,
                github_remaining=github_remaining_text,
                github_reset=github_reset,
                github_cache_count=github_cache_count,
                configured_target_count=len(configured_targets),
                subscription_count=subscription_count,
                effective_target_count=len(effective_targets),
                pending=pending,
                exhausted=exhausted,
                last_text=last_text,
            )
        )

    @marketwatch.command("check")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def check(self, event: AstrMessageEvent):
        if self._service is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        runtime = self._runtime()
        report = await self._service.check(
            enabled_sources=self._enabled_sources(),
            push_targets=runtime.push_targets,
            max_items_per_push=runtime.max_items_per_push,
            include_star_count=runtime.include_star_count,
            enable_ai_summary=runtime.enable_ai_summary,
            llm_provider_id=runtime.llm_provider_id,
            provider_origin=event.unified_msg_origin,
        )
        yield event.plain_result(report.to_chinese())

    @marketwatch.command("test-push")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_push(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("主动推送测试仅可在群聊中使用。")
            return
        if self._notifier is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            success, error_code = await self._notifier.send(
                event.unified_msg_origin,
                "【Market Watcher】主动推送测试：如果你看到此消息，"
                "当前会话的主动消息链路可用。",
            )
        except Exception:
            success, error_code = False, "delivery_exception"
        if success:
            yield event.plain_result("Market Watcher 主动推送测试已发送。")
            return
        safe_error = (
            error_code
            if error_code
            in {"astrbot_send_exception", "astrbot_send_false", "delivery_exception"}
            else "delivery_failed"
        )
        yield event.plain_result(
            f"Market Watcher 主动推送测试失败（错误类别：{safe_error}）。"
        )

    @marketwatch.command("test-outbox-prepare")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_outbox_prepare(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("出站箱跨重启诊断仅可在群聊中使用。")
            return
        if self._service is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            counts = await self._service.prepare_outbox_diagnostic(
                event.unified_msg_origin
            )
        except (StateError, OSError, ValueError):
            yield event.plain_result(
                "出站箱诊断准备失败（错误类别：outbox_diagnostic_state_error）。"
            )
            return
        yield event.plain_result(
            "出站箱跨重启诊断已准备并进入长期 hold；"
            "必须显式执行 deliver 或 cleanup。\n"
            f"{_format_outbox_diagnostic_counts(counts)}"
        )

    @marketwatch.command("test-outbox-status")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_outbox_status(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("出站箱跨重启诊断仅可在群聊中使用。")
            return
        if self._service is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            counts = await self._service.outbox_diagnostic_status()
        except (StateError, OSError, ValueError):
            yield event.plain_result(
                "出站箱诊断状态不可用（错误类别：outbox_diagnostic_state_error）。"
            )
            return
        yield event.plain_result(_format_outbox_diagnostic_counts(counts))

    @marketwatch.command("test-outbox-deliver")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_outbox_deliver(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("出站箱跨重启诊断仅可在群聊中使用。")
            return
        if self._service is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            counts = await self._service.deliver_outbox_diagnostic()
        except (StateError, OSError, ValueError):
            yield event.plain_result(
                "出站箱诊断投递失败（错误类别：outbox_diagnostic_state_error）。"
            )
            return
        yield event.plain_result(
            "已解除诊断 hold 并调用生产 outbox 投递链路；"
            "其他已到期真实 pending 也可能同时处理。\n"
            f"{_format_outbox_diagnostic_counts(counts)}"
        )

    @marketwatch.command("test-outbox-cleanup")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_outbox_cleanup(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("出站箱跨重启诊断仅可在群聊中使用。")
            return
        if self._service is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            counts = await self._service.cleanup_outbox_diagnostic()
        except (StateError, OSError, ValueError):
            yield event.plain_result(
                "出站箱诊断清理失败（错误类别：outbox_diagnostic_state_error）。"
            )
            return
        yield event.plain_result(
            f"出站箱诊断记录已清理。\n{_format_outbox_diagnostic_counts(counts)}"
        )

    @marketwatch.command("test-ai")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_ai(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("AI Provider 测试仅可在群聊中使用。")
            return
        if self._ai_intro is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        runtime = self._runtime()
        result = await self._ai_intro.diagnose(
            provider_id=runtime.llm_provider_id,
            provider_origin=event.unified_msg_origin,
        )
        if result.status == "success" and result.intro:
            yield event.plain_result(
                f"真实 Provider 调用成功。安全导语：{result.intro}"
            )
            return
        error_code = result.error_code or "ai_fallback"
        yield event.plain_result(
            f"真实 Provider 调用未成功（错误类别：{error_code}），"
            f"已回退纯事实模板：{AI_DIAGNOSTIC_FACTS}"
        )

    @marketwatch.command("test-github")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def test_github(self, event: AstrMessageEvent):
        if event.is_private_chat():
            yield event.plain_result("GitHub API 测试仅可在群聊中使用。")
            return
        runtime = self._runtime()
        result = await run_github_diagnostic(
            token=runtime.github_token,
            timeout_seconds=runtime.request_timeout_seconds,
        )
        yield event.plain_result(format_github_diagnostic(result))

    @marketwatch.command("subscribe")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def subscribe(self, event: AstrMessageEvent):
        if self._store is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            result = await self._change_subscription(event, subscribe=True)
        except (StateError, OSError, ValueError):
            yield event.plain_result("群订阅状态不可用或保存失败，请检查状态文件。")
            return
        messages = {
            "private_chat": "群订阅命令仅可在群聊中使用。",
            "invalid_origin": "无法识别当前群会话，请稍后重试。",
            "already_subscribed": "当前群已订阅市场变化通知。",
            "subscribed": "当前群已成功订阅市场变化通知。",
        }
        yield event.plain_result(messages[result])

    @marketwatch.command("unsubscribe")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def unsubscribe(self, event: AstrMessageEvent):
        if self._store is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        try:
            result = await self._change_subscription(event, subscribe=False)
        except (StateError, OSError, ValueError):
            yield event.plain_result("群订阅状态不可用或保存失败，请检查状态文件。")
            return
        messages = {
            "private_chat": "群订阅命令仅可在群聊中使用。",
            "invalid_origin": "无法识别当前群会话，请稍后重试。",
            "already_unsubscribed": "当前群尚未订阅市场变化通知。",
            "unsubscribed": "当前群已取消市场变化订阅。",
        }
        yield event.plain_result(messages[result])

    @marketwatch.command("subscriptions")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def subscriptions(self, event: AstrMessageEvent):
        if self._store is None:
            yield event.plain_result("市场观察器尚未完成初始化。")
            return
        if event.is_private_chat():
            yield event.plain_result("群订阅命令仅可在群聊中使用。")
            return
        try:
            total, current = await self._subscription_snapshot(event)
        except (StateError, OSError, ValueError):
            yield event.plain_result("群订阅状态不可用，请检查状态文件。")
            return
        current_text = "已订阅" if current else "未订阅"
        yield event.plain_result(f"群订阅总数：{total}\n当前群：{current_text}")

    async def _change_subscription(
        self, event: AstrMessageEvent, *, subscribe: bool
    ) -> str:
        if self._store is None:
            raise StateError("store is not initialized")
        if self._service is None:
            return update_subscription(self._store, event, subscribe=subscribe)
        async with self._service.lock:
            return update_subscription(self._store, event, subscribe=subscribe)

    async def _subscription_snapshot(
        self, event: AstrMessageEvent
    ) -> tuple[int, bool | None]:
        if self._store is None:
            raise StateError("store is not initialized")
        if self._service is None:
            return subscription_status(self._store, event)
        async with self._service.lock:
            return subscription_status(self._store, event)

    def _enabled_sources(self) -> set[SourceKind]:
        mapping = {
            "source_market_api": SourceKind.MARKET,
            "source_collection_issues": SourceKind.COLLECTION_ISSUE,
            "source_plugin_publish_issues": SourceKind.LEGACY_PUBLISH_ISSUE,
            "source_github_discovery": SourceKind.GITHUB_DISCOVERY,
        }
        return {
            kind
            for key, kind in mapping.items()
            if type(self.config.get(key, False)) is bool and self.config.get(key, False)
        }

    async def _automatic_check(self) -> RunReport:
        if self._service is None:
            return RunReport(
                status="state_error",
                started_at=self._service_clock(),
                error_code="service_not_initialized",
            )
        runtime = self._runtime()
        return await self._service.check(
            enabled_sources=self._enabled_sources(),
            push_targets=runtime.push_targets,
            max_items_per_push=runtime.max_items_per_push,
            include_star_count=runtime.include_star_count,
            enable_ai_summary=runtime.enable_ai_summary,
            llm_provider_id=runtime.llm_provider_id,
        )

    def _runtime(self) -> RuntimeConfig:
        self._runtime_config = parse_runtime_config(self.config)
        return self._runtime_config

    @staticmethod
    def _observe_run(payload: dict) -> None:
        logger.info(
            "[MarketWatcher] run=%s phase=%s duration_ms=%s events=%s "
            "sources_ok=%s sources_failed=%s error_code=%s",
            payload.get("run_id"),
            payload.get("phase"),
            payload.get("duration_ms"),
            payload.get("events"),
            payload.get("sources_succeeded"),
            payload.get("sources_failed"),
            payload.get("error_code"),
        )

    @staticmethod
    def _service_clock() -> str:
        if __package__:
            from .market_watcher.normalize import utc_now
        else:
            from market_watcher.normalize import utc_now

        return utc_now()


def _format_outbox_diagnostic_counts(counts: dict[str, int]) -> str:
    return (
        "Market Watcher 出站箱跨重启诊断\n"
        f"- count={counts['count']}\n"
        f"- pending={counts['pending']}\n"
        f"- failed={counts['failed']}\n"
        f"- sent={counts['sent']}\n"
        f"- exhausted={counts['exhausted']}"
    )
