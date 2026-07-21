# 更新日志

本项目遵循语义化版本，并在此记录面向使用者的重要变化。

---

## Unreleased

- 暂无。

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
