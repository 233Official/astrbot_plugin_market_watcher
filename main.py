from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_market_watcher",
    "233Official",
    "聚合 AstrBot 插件市场与 GitHub 发布信号的监控插件",
    "0.1.0",
)
class MarketWatcherPlugin(Star):
    """Provide the initialization skeleton for market monitoring."""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._poller_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

    async def initialize(self) -> None:
        """Start the stoppable placeholder polling task when enabled."""
        if not self.config.get("enabled", False):
            logger.info("[MarketWatcher] 插件当前未启用，未启动轮询任务")
            return

        self._stop_event.clear()
        self._poller_task = asyncio.create_task(
            self._polling_loop(), name="market_watcher_poller"
        )
        self._poller_task.add_done_callback(self._on_poller_done)
        logger.info("[MarketWatcher] 初始化完成；当前仅运行占位轮询，不访问网络")

    def _on_poller_done(self, task: asyncio.Task[None]) -> None:
        """Consume terminal task exceptions so they are logged deterministically."""
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            logger.error("[MarketWatcher] 轮询任务异常退出: %r", exception)

    async def _polling_loop(self) -> None:
        """Wait between future checks without performing network requests."""
        interval_minutes = max(1, int(self.config.get("poll_interval_minutes", 30)))
        try:
            while not self._stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=interval_minutes * 60
                    )
                except asyncio.TimeoutError:
                    logger.debug(
                        "[MarketWatcher] 占位轮询到期；初始化阶段不执行网络采集"
                    )
        except asyncio.CancelledError:
            logger.debug("[MarketWatcher] 轮询任务收到取消请求")
            raise

    async def terminate(self) -> None:
        """Stop and await the background task safely."""
        self._stop_event.set()
        task = self._poller_task
        self._poller_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        logger.info("[MarketWatcher] 插件已停止")

    @filter.command_group("marketwatch")
    def marketwatch(self) -> None:
        """AstrBot 插件市场观察器命令组。"""

    @marketwatch.command("status")
    async def status(self, event: AstrMessageEvent):
        """Show the current skeleton runtime status."""
        enabled = bool(self.config.get("enabled", False))
        running = self._poller_task is not None and not self._poller_task.done()
        interval = max(1, int(self.config.get("poll_interval_minutes", 30)))
        yield event.plain_result(
            "AstrBot 插件市场观察器\n"
            f"- 配置启用：{'是' if enabled else '否'}\n"
            f"- 占位轮询任务：{'运行中' if running else '未运行'}\n"
            f"- 轮询间隔：{interval} 分钟\n"
            "- 开发状态：初始化版本，尚未接入真实数据源"
        )

    @marketwatch.command("check")
    async def check(self, event: AstrMessageEvent):
        """Report that collection is intentionally unavailable for now."""
        yield event.plain_result(
            "Market Watcher 当前处于初始化阶段，尚未实现完整采集；"
            "本命令不会发起网络请求。"
        )
