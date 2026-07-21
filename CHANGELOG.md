# 更新日志

本项目遵循语义化版本，并在此记录面向使用者的重要变化。

---

## Unreleased

### Added

- （预留）

---

## 1.1.1 - 2026-07-22

### Fixed

- 修复 Release Tag `v` 前缀版本校验：metadata/main.py/pyproject 三处版本源的 Git tag 比较统一使用 `tag.removeprefix("v")`。
- 修复 Release notes 提取：CHANGELOG 使用 `## 1.1.1 - date` 而非 `## v1.1.1 - date`，提取正则匹配无前缀版本 heading。
- 修复 ZIP 顶层目录 entry 非确定性：使用显式 `ZipInfo(date_time=FIXED_TIMESTAMP)` + Unix 目录 metadata，消除跨构建时间漂移，通过 Python 3.11 三间隔构建 raw SHA-256 回归验证。

---

## 1.1.0 - 2026-07-22

### Added

- 增加图片卡片（T2I）市场简报：`card_renderer` 视觉模块，outbox 批次携带可选 `card_payload`，notifier 两阶段 prepare/send 驱动 HTML→图片渲染，渲染异常或超时自动回退纯文本。
- 增加 AstrBot T2I 服务路径型返回支持：`html_render(return_url=False)` 返回本地文件路径时，`prepare()` 以 `asyncio.to_thread` 安全读取、magic 校验（JPEG/PNG/WebP/GIF）、大小验证（上限 20 MiB），无效内容安全拒绝仅记录 `invalid_signature`。
- 增加 json-safe outbox payload 与跨重启可靠性：`DeliveryBatch.card_payload` 序列化至磁盘 state，重启后恢复重试。
- 增加图片卡片分页：启用时每批最多 5 条事件，多目标复用单次渲染结果。
- 增加图片失败同次 attempt 文本降级：图片发送失败时同一 attempt 自动降级原文本；图片与文本均失败才增加 attempt 计数。
- 新增配置 `enable_image_card`（默认 `true`）和 `image_render_timeout_seconds`（默认 `8`，范围 3–20）。
- `marketwatch test-push` 在图片卡片启用时报实际发送模式（`image` / `text_fallback` / `text`）。
- 增加 renderer 诊断日志：初始化时采样 `plugin_callable`、`context_callable`、`api_callable`、MRO owner 和 `notifier_callable`，仅观测不使用模块 fallback。
- `AstrBotNotifier.prepare()` 增加结构化结果观测日志，覆盖 `image_ready` / `string` / `invalid_image` / `exception` / `timeout` / `cancelled` 路径，不含原始值或凭据。
- `CancelledError` 传播，确保不因渲染超时或取消误发文本回退。
- 增加标准 `scripts/package_plugin.py`（支持 `--dev-version`、`--test-label`、`--flat`）、`.vscode/launch.json`、`.github/workflows/release.yml`；CI/CD 与发布结构整改。
- 增加 Jinja2 `SandboxedEnvironment` 真实模板合约测试，覆盖全部卡片形状；`card_renderer.CARD_TEMPLATE` 所有 Jinja 表达式均用 `|e` 转义。
- `AstrBotNotifier` 增加 `_prepare_attempted` 非持久化标志，准确区分 `text_fallback`（图片卡启用且 prepare 尝试过）与 `text`（纯文本 batch/图片关闭）。

### Changed

- 正式版本从 `1.0.0` 升级至 `1.1.0`（新功能向后兼容）。
- `DeliveryBatch` 增加可选 `card_payload` 字段，`to_dict`/`from_dict` 兼容旧 state 无 payload 场景。
- `Notifier` 协议增加 `prepare` 方法，`AstrBotNotifier` 注入 `html_render` 和超时。
- `RuntimeConfig` 增加图片卡片配置项，`format_status` 显示图片卡片状态。
- `AstrBotNotifier.send()` 的 `text_fallback` 判定从 `_pending_image_bytes is not None` 改为 `_prepare_attempted`，修复图片卡启用但 renderer 返回无效时语义。

### Fixed

- 修复 Jinja2 `card.items` 冲突：模板中冲突的保留字访问改为安全显式 `card.get("items", [])`；增加 `SandboxedEnvironment` 合约测试防止回归。
- `html_render` 注入来源修正为 `getattr(self, "html_render", None)` 而非 `self.context`。
- `prepare()` 调用 `html_render` 使用 `return_url=False, options=options` 关键字参数确保返回 bytes。
- `deliver_pending()` 图片串批修复：每存在到期目标的 batch 均触发 `prepare()` 清除前一图片 bytes。

---

## 1.0.0 - 2026-07-21

### Added

- 交付市场 API/`plugins.json`、Collection Issues、主仓历史发布 Issues 与 GitHub 全局发现四来源适配、规范化、跨来源合并、静默基线以及新增/实质更新检测。
- 增加 schema v1 原子状态、可靠 outbox、逐目标有限重试、跨重启恢复、群订阅持久化、fixed-delay 调度和管理员状态/检查/诊断命令。
- 增加 GitHub 匿名或 Token 模式、共享预算、并发限制、ETag/TTL 缓存、Star 补充和 primary/secondary rate-limit 安全分类。
- 增加可选 AI 单段导语、默认或显式 Provider 路由、60 秒默认超时、事实模板降级和安全输出清洗。
- 增加确定性 WebUI ZIP、SHA-256 sidecar、解包后包上下文导入自检、最终设计文档和脱敏线上验收记录。

### Changed

- MVP 规格正式冻结为 `1.0.0` 基线；后续功能、缺陷和技术债改由 GitHub Issues 跟踪，并同步维护设计文档。
- 支持平台声明为 `aiocqhttp` 与 QQ 官方 WebSocket `qq_official`；QQ 官方频道主动推送和 Webhook 不在本版本承诺范围。
- canonical 命令只注册 `marketwatch`，唤醒前缀及 @/回复规则统一交由 AstrBot 宿主管理。

### Fixed

- 修复 AstrBot 包上下文加载时 sibling 顶层导入失败，并保留离线顶层 `main` 导入契约。
- 针对 AstrBot v4.26.6 停用后再启用的 stale handler binding 增加严格限定的重绑定兼容层。
- 修复无效 GitHub Token 后持续认证失败的问题：401 时本轮禁用 Token 并降级匿名预算，后续配置恢复后可重新启用。
- 确保 AI 超时、Provider 缺失、异常或不合规输出不阻塞状态保存、outbox 和纯事实推送。

### Security

- GitHub Token 仅注入 exact `https://api.github.com`，诊断与日志不输出 Token、Authorization、响应正文或完整 UMO。
- LLM 仅接收受限公开规范化事实；外部文本按不可信输入处理，并中和 CQ、mention 和 Markdown 控制字符。
- 发布验证扫描疑似凭据并拒绝环境文件、缓存、测试、脚本、符号链接和路径穿越成员进入 ZIP。

---

## 0.1.0 - 2026-07-20

- 建立 AstrBot Star 插件生命周期与命令骨架。
- 增加安全默认配置、领域接口、静态测试、README 与 PRD。
- 明确初始化版本不执行真实市场采集或主动推送。
