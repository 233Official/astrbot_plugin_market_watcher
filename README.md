# AstrBot 插件市场观察器

为 AstrBot 聚合插件市场与 GitHub 发布信号，识别新插件和重要变化，并把中文摘要可靠推送到指定会话。

[![Version](https://img.shields.io/badge/version-1.0.0-blue)](./CHANGELOG.md) [![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.24.0%2C%3C5-6f42c1)](https://github.com/AstrBotDevs/AstrBot)  [![Python](https://img.shields.io/badge/Python-%3E%3D3.10-3776ab)](https://www.python.org/)  [![License](https://img.shields.io/badge/license-AGPL--3.0-green)](./LICENSE)

当前支持市场 API / `plugins.json`、AstrBot 插件 Collection Issues，并可选补充旧发布 Issue、GitHub 全局发现、Star 数和 AI 导语。最快的使用路径是通过仓库地址安装，在目标群订阅，然后执行一次检查。遇到问题请提交 [GitHub Issue](https://github.com/233Official/astrbot_plugin_market_watcher/issues)。

---

## 功能亮点

- 聚合多个来源并合并同一插件，减少重复通知。
- 按来源建立静默基线，只报告后续新增或实质变化，不把历史记录一次性刷屏。
- 支持手动检查与 fixed-delay 自动检查；自动调度默认关闭。
- 使用持久化订阅和 outbox 投递，保留待重试与永久失败状态。
- 可选采集 GitHub Star，并以共享预算、缓存和限流降级控制请求成本。
- 可选调用 AstrBot LLM Provider 生成短导语；失败时自动回退到纯事实摘要。
- 可选图片卡片（T2I）投递：自动将变化摘要渲染为图片卡片，渲染超时或异常时自动降级纯文本。

---

## 5 分钟快速开始

### 1. 安装

在 AstrBot WebUI 的插件管理中使用以下仓库地址安装：

```text
https://github.com/233Official/astrbot_plugin_market_watcher
```

也可将仓库克隆到 AstrBot 插件目录后重载插件。当前没有在本文档中声明 AstrBot 市场安装渠道。要求 AstrBot `>=4.24.0,<5`、Python `>=3.10`。

### 2. 最小配置

保持默认来源即可。若要自动检查，将 `enabled` 设为 `true`；首次体验可继续保持 `false`，使用管理员手动检查。`github_token`、`llm_provider_id` 均可留空。

### 3. 首次使用

命令注册名不含唤醒词；以下使用默认 `/` 前缀。若前缀为 `!`，可发送 `!marketwatch status`；若 `wake_prefix=[]`，群聊通常需要先 `@机器人` 再发送命令。

```text
/marketwatch subscribe
/marketwatch test-push
/marketwatch check
/marketwatch status
```

这些操作需要 AstrBot 管理员权限，且订阅与推送测试只能在群聊中使用。

### 4. 可观察结果

- `subscribe` 回复当前群已订阅。
- `test-push` 在当前群产生一条主动推送测试消息；图片卡片启用时报告实际发送模式（`image` / `text_fallback` / `text`）。
- 首次成功 `check` 为各来源建立静默基线，并返回中文运行摘要；没有历史插件批量推送是预期行为。
- `status` 显示来源、调度器、目标、待投递项和最后运行报告。

---

## 支持矩阵

| 平台或场景 | 状态 | 范围 |
| --- | --- | --- |
| AstrBot v4.26.6 + `qq_official` WebSocket 群聊 | 已验证 | 命令、订阅持久化、主动推送、重启后 outbox 投递 |
| `aiocqhttp` | 待验证 | metadata 已声明且有离线契约覆盖，本次未做真实平台回归 |
| `qq_official` C2C 主动消息 | 待验证 | 不从群聊结果外推 |
| QQ 官方频道 cron 主动推送 | 不支持 | 当前版本不作承诺 |
| `qq_official_webhook` | 不支持 | 未验收，且未列入支持平台 |

线上证据和边界见[线上验收记录](./docs/ONLINE_ACCEPTANCE.md)。其他 AstrBot 版本、adapter、账号类型或部署方式均不因单次成功而自动视为已验证。

---

## 高频命令

| 场景 | 命令 | 权限 | 结果 |
| --- | --- | --- | --- |
| 查看状态 | `/marketwatch status` | 所有用户 | 返回本地状态与最后运行报告 |
| 立即检查 | `/marketwatch check` | AstrBot 管理员 | 采集来源、检测变化并处理目标推送 |
| 订阅当前群 | `/marketwatch subscribe` | AstrBot 管理员；仅群聊 | 持久化当前会话 UMO |
| 取消当前群 | `/marketwatch unsubscribe` | AstrBot 管理员；仅群聊 | 幂等移除当前群订阅 |
| 测试主动推送 | `/marketwatch test-push` | AstrBot 管理员；仅群聊 | 向当前会话发送诊断消息 |

完整命令、限制和诊断用途见[命令参考](./docs/COMMANDS.md)。

---

## 高频配置

| 配置键 | 默认值 | 用途与风险 |
| --- | --- | --- |
| `enabled` | `false` | 是否启动自动调度；关闭不影响手动 `check` |
| `poll_interval_minutes` | `30` | 每轮完成后的等待分钟数，范围 `5`–`1440` |
| `push_targets` | `[]` | WebUI 配置的 UMO；留空时仅使用群订阅目标 |
| `github_token` | `""` | 可选敏感项，仅用于提高 GitHub API 限额 |
| `include_star_count` | `true` | 开启时额外消耗 GitHub 请求预算；关闭可降低预算使用 |
| `enable_ai_summary` | `false` | 仅增加短导语，失败不阻塞事实推送 |
| `llm_provider_id` | `""` | 留空时按手动或自动检查场景解析默认 Provider |
| `enable_image_card` | `true` | 开启后变化摘要优先渲染为图片卡片再推送；渲染失败自动降级纯文本 |
| `image_render_timeout_seconds` | `8` | 单次图片渲染超时上限，范围 3–20 秒 |

全部配置、边界、默认回退和来源开关见[配置参考](./docs/CONFIGURATION.md)。

---

## 数据源与默认行为

- 默认启用市场 API / `plugins.json` 与 AstrBot 插件 Collection Issues。
- AstrBot 主仓旧 `plugin-publish` Issues 已废弃，仅作兼容，默认关闭。
- GitHub `astrbot_plugin_*` 全局发现成本与误报率较高，默认关闭。
- 首次成功采集只建立静默基线；后续新增和实质更新才形成变化事件。
- 配置目标与群订阅会合并、清理、去重并排序后用于推送。

---

## 安全与隐私

- `github_token` 默认空；如需配置，请使用最小权限 Fine-grained Token，并限制 AstrBot 配置文件访问权限。
- 不要在聊天、日志、截图、Issue 或版本库中提交 Token、完整 UMO、真实群号、用户标识或内部地址。
- `push_targets` 默认空，插件不会在没有有效配置目标或群订阅时主动发送变化通知。
- AI 只接收受限的公开规范化事实，不应接收 Token、UMO、原始响应或完整配置。
- 提交 Issue 前请先脱敏；诊断命令只用于受控管理员测试。

---

## 文档索引

- [完整命令参考](./docs/COMMANDS.md)
- [完整配置参考](./docs/CONFIGURATION.md)
- [设计与安全边界](./docs/DESIGN.md)
- [脱敏线上验收记录](./docs/ONLINE_ACCEPTANCE.md)
- [插件开发与发布 Playbook](./docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md)
- [贡献指南](./CONTRIBUTING.md)
- [版本变更](./CHANGELOG.md)

---

## FAQ 与反馈

### 为什么第一次检查没有推送历史插件？

首次成功采集会建立静默基线；只有之后检测到的新增或实质变化才会通知。

### 为什么自动检查没有运行？

`enabled` 默认是 `false`。启用后插件采用 fixed-delay 调度；也可由管理员执行 `/marketwatch check`。

### 命令为什么没有响应？

确认使用当前 AstrBot 唤醒方式。插件注册的是 `marketwatch`，不是带固定 `/` 或 `!` 的命令名；多数命令还要求管理员权限或群聊环境。

缺陷、使用问题和功能建议请提交 [GitHub Issues](https://github.com/233Official/astrbot_plugin_market_watcher/issues)，并附脱敏后的版本、平台、命令与错误类别。

---

## License

本项目使用 [GNU Affero General Public License v3.0](./LICENSE)。
