# AstrBot 插件市场观察器

`astrbot_plugin_market_watcher` 聚合 AstrBot 插件市场、发布 Issue 与 GitHub 仓库信号，识别新插件和重要变化，并向指定 AstrBot 会话推送可读摘要。

当前正式版本为 **1.0.0**。MVP 已于 2026-07-21 在 AstrBot v4.26.6 与 QQ 官方 WebSocket 群聊完成发布前线上验收，覆盖生命周期、权限、订阅持久化、主动推送、默认来源、AI Provider 路由、GitHub 认证降级恢复和 pending outbox 跨完整重启投递。

规格文档：

- [MVP 产品需求文档（PRD）](docs/PRD.md)
- [MVP 功能规格文档（FSD）](docs/FSD.md)
- [1.0.0 最终设计文档](docs/DESIGN.md)
- [2026-07-21 脱敏线上验收记录](docs/ONLINE_ACCEPTANCE.md)
- [插件开发与发布 Playbook](docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md)：记录包结构、离线验收、故障定位与真实 AstrBot 验收边界。

---

## 目标功能

- 发现新增和实质更新的 AstrBot 插件。
- 合并多个来源中的同一插件，避免重复提醒。
- 获取并缓存 GitHub Star 数等补充指标。
- 可选调用 AstrBot LLM Provider 生成中文变化摘要。
- 按明确目标会话和稳定批次推送通知。

---

## 数据源

- AstrBot 市场 API 或 `plugins.json`：主要事实来源。
- AstrBot 插件 Collection Issues：主要发布入口和候选信号。
- AstrBot 主仓 `plugin-publish` Issues：仅兼容历史与残留信号；官方发布指南已称该入口废弃。
- GitHub 全局 `astrbot_plugin_*` 仓库发现：低频补充发现来源，默认关闭。

详细范围与实现契约见 [产品需求文档](docs/PRD.md) 和 [功能规格文档](docs/FSD.md)。四类来源适配均属于 MVP，但市场/Collection 默认开启，主仓旧 Issues/GitHub 全局发现默认关闭。

---

## 安装方式

在 AstrBot WebUI 的插件管理中使用仓库地址安装：

```text
https://github.com/233Official/astrbot_plugin_market_watcher
```

也可以将仓库克隆到 AstrBot 的插件目录后重载插件。当前版本要求 AstrBot `>=4.24.0,<5`，Python `>=3.10`。

支持平台声明：

- `aiocqhttp`。
- QQ 官方 WebSocket `qq_official`，已验证群聊命令与主动消息。
- QQ 官方频道主动推送、C2C 主动消息和 `qq_official_webhook` 尚未验收，不属于 `1.0.0` 承诺范围。

---

## 命令

插件命令注册名不含唤醒词，AstrBot 会在命令 filter 前按当前 `wake_prefix` 统一处理。下列 `/marketwatch ...` 仅是默认 `/` 前缀示例；若配置 `!` 前缀，可发送 `!marketwatch status`。当 `wake_prefix=[]` 时，群聊通常按 AstrBot 当前规则 @机器人后发送 `marketwatch status`，不要配置会匹配所有消息的空字符串前缀。

- `/marketwatch status`：只读取本地状态，显示状态健康、插件记录、待投递目标、永久失败目标和最后运行报告。
- `/marketwatch check`：仅管理员可用，执行四来源正常检查、基线/变化检测及配置目标推送，并返回中文运行摘要。
- `/marketwatch test-push`：仅管理员在群聊中使用，向当前会话直接发送一条主动推送诊断消息；不检查来源，不修改订阅、outbox 或其他持久状态。
- `/marketwatch test-ai`：仅管理员在群聊中使用，以固定虚构事实调用真实 AstrBot Provider；显式 Provider 优先，否则解析当前会话默认 Provider。诊断不受 `enable_ai_summary` 开关阻止，也不修改持久状态。
- `/marketwatch test-github`：仅管理员在群聊中使用，独立请求 GitHub `/rate_limit`，验证匿名或 Token 认证及 primary rate limit headers；不消耗生产预算或修改状态。
- `/marketwatch test-outbox-prepare`：仅管理员在群聊中使用，为当前会话幂等持久化一条带长期 hold 的诊断 pending；不会自动投递。
- `/marketwatch test-outbox-status`：仅管理员在群聊中使用，只显示诊断 batch 总数及 pending/failed/sent/exhausted 计数。
- `/marketwatch test-outbox-deliver`：仅管理员在群聊中使用，解除诊断 hold 并调用生产 outbox 投递链路；成功记录保留为 sent 证据。
- `/marketwatch test-outbox-cleanup`：仅管理员在群聊中使用，幂等删除所有诊断 batch，不影响真实 `batch:` 项。
- `/marketwatch subscribe`：仅群管理员在群聊中使用，将当前群官方 UMO 加入持久化订阅。
- `/marketwatch unsubscribe`：仅群管理员在群聊中使用，幂等取消当前群订阅。
- `/marketwatch subscriptions`：仅群管理员在群聊中使用，只显示订阅总数和当前群状态，不列出 UMO。

---

## 配置

核心配置位于 `_conf_schema.json`：

- `enabled`：fixed-delay 自动调度总开关；关闭时不影响管理员手动检查。
- `poll_interval_minutes`：每轮自动检查完成后的等待时间，范围为 5 至 1440 分钟。
- `push_targets`：WebUI 主动推送目标 UMO 列表；每轮与群管理员命令创建的持久化订阅合并、验证、去重和排序。
- `github_token`：可选 GitHub Token，默认空。
- `include_star_count`：是否请求、缓存并在事件摘要中显示 GitHub Star。
- `enable_ai_summary`：可选 AI 导语总开关，默认关闭。
- `llm_provider_id`：AI 导语显式 Provider；为空时，手动检查解析当前会话默认 Provider，自动检查解析首个有效目标的默认 Provider。
- `ai_timeout_seconds`：生产 AI 导语与 `test-ai` 共用的单次 Provider 超时，默认 60 秒、范围 10 至 120 秒；到期取消本次调用并回退事实模板，不重试。
- `source_*`：各数据源开关；高成本或兼容来源默认关闭。

启用自动调度后，初始化先等待 10 秒，再执行首轮检查；每轮完成后才开始计算下一次等待时间。自动与手动检查复用同一互斥锁，忙碌时自动轮次跳过且不排队。

---

## 发布包安装

1. 运行 `python scripts/package_release.py`，在 AstrBot WebUI 上传生成的 ZIP。
2. 按需配置 GitHub Token、AI 开关和 Provider；也可留空 Provider 使用当前会话默认值。
3. 群管理员在测试群按当前唤醒方式执行 `marketwatch subscribe`（默认示例为 `/marketwatch subscribe`）。
4. 按相同唤醒方式执行 `marketwatch check`，再执行 `marketwatch status` 核对来源、预算、AI 与投递状态。

每个来源首次成功采集只建立静默基线，不会把历史插件作为新增批量推送。

---

## QQ 官方 WebSocket 验收

- 在 AstrBot WebUI 配置 `qq_official` WebSocket adapter 时，配置的适配器 `id` 是 UMO 第一段；例如实例 ID 为 `qqws` 时，群 UMO 形如 `qqws:GROUP_MESSAGE:<group_openid>`。
- 可先按当前唤醒方式执行 canonical 命令 `marketwatch test-push`，直接验证当前群主动消息链路。本次 `!` 前缀实例可发送 `!marketwatch test-push`；`!` 只是当前配置示例，不是插件固定要求。
- AI 两阶段验收先将 `llm_provider_id` 留空并执行 `!marketwatch test-ai`，验证当前会话默认 Provider；再在 WebUI 选择显式 Provider、重载插件并重复执行。成功回复包含“真实 Provider 调用成功”和安全导语；失败回复包含脱敏错误类别及纯事实模板。`!` 同样只是本次实例示例。
- 真实线上验收显示 10 秒和 30 秒均返回 `ai_timeout`，将超时调整为 60 秒后真实 Provider 调用成功，因此当前默认值为 60 秒。
- 如能在不影响其他业务的情况下安全选择不可用 Provider，可额外验证降级；否则以离线异常、超时和不合规输出测试作为故障证据。
- 推荐直接在 QQ 官方 WebSocket 测试群按当前唤醒方式执行 `marketwatch subscribe` 捕获 AstrBot 提供的准确 UMO，不建议手工猜测 `group_openid`。默认 `/` 前缀下即 `/marketwatch subscribe`，自定义前缀需相应替换。
- 随后按相同唤醒方式执行 `marketwatch subscriptions` 确认当前群已订阅，并在可产生测试变化的受控条件下执行 `marketwatch check` 验证主动推送。
- 重启或重载 AstrBot 后，再次执行 `marketwatch subscriptions` 和 `marketwatch check`，确认订阅持久化与主动推送仍正常。
- 当前优先验收群聊。C2C 主动发送可后续单独验证；频道 cron 主动推送不在承诺范围，`qq_official_webhook` 尚未验收且不声明支持。

### Pending outbox 跨重启验收

以下均为 canonical 命令名，需按当前 AstrBot 唤醒方式发送，不要照抄固定前缀：

1. 执行 `marketwatch test-outbox-prepare`。
2. 执行 `marketwatch test-outbox-status`，确认 `count=1`、`pending=1`。
3. **完整重启 AstrBot**，不是仅重载插件；重启后再次执行 status，确认仍为 `pending=1`。
4. 执行 `marketwatch test-outbox-deliver`，确认群收到 Market Watcher 出站箱跨重启诊断消息。
5. 再次执行 status，确认 `sent=1`、`pending=0`。
6. 执行 `marketwatch test-outbox-cleanup`，最后执行 status，确认 `count=0`。

prepare 使用长期 hold，必须显式 deliver 或 cleanup。deliver 与生产检查一致，除诊断项外也可能同时处理其他已经到期的真实 pending；prepare 会使普通 status 的总 pending 临时增加 1，cleanup 后恢复。

---

## GitHub API 诊断验收

- `marketwatch test-github` 只请求固定 `https://api.github.com/rate_limit`，输出认证模式、HTTP 状态、安全分类和 primary rate limit 的 Limit、Remaining、Reset；不会显示 Token、响应正文或 Authorization header。
- 第一阶段清空 `github_token`，重载后按当前唤醒方式执行命令；本次 `!` 前缀实例示例为 `!marketwatch test-github`，预期认证模式为“匿名”。
- 第二阶段仅在 WebUI 配置有效的最小权限 Fine-grained Token，重载后重复命令，预期认证模式为“已配置 Token”且分类为 `ok`。
- 第三阶段可临时填写明显无效的占位 Token，重载后重复命令，预期 HTTP 401、分类 `auth_failed`、错误类别 `github_auth_failed`。测试完成后立即清空无效 Token，不要把真实 Token 写入聊天、日志、文档或仓库。
- `/rate_limit` 成功只证明该认证与限流查询路径可用，不代表所有仓库、搜索或 Issue 端点权限。不得通过真实请求耗尽限额；429 和 403 限流分类使用离线模拟验证。

---

## 安全说明

- 仓库不包含任何凭据，`github_token` 默认值为空。
- GitHub Token 应使用最小权限 Fine-grained Token，不得写入日志、Issue、截图或版本库。
- 当前 AstrBot 插件配置 schema 未提供通用密码输入控件，WebUI 可能以普通字符串框展示 Token；请限制 AstrBot 配置文件权限，并只在确有配额需要时配置。
- 推送目标默认空，插件不得在用户未配置目标时主动发送消息。
- 群订阅只保存 AstrBot 提供的 `event.unified_msg_origin`，不解析群号；完整 UMO 不进入普通日志、status 或订阅列表输出。
- GitHub Token 仅注入到 exact `https://api.github.com` 请求，不发送到市场 API、raw GitHub 或其他主机。
- GitHub 请求共享每轮预算（有 Token 20、无 Token 5）和最大并发 2；仓库元数据 TTL 分别为 6 小时和 24 小时。
- LLM 仅接收受限的公开规范化事实，不接收 Token、UMO、配置、raw excerpt 或完整响应；导语失败不会阻塞 outbox 和事实推送。

---

## AstrBot v4.26.6 启停兼容

- AstrBot v4.26.6 在插件停用后保留已绑定 handler，重新启用时再次执行 `functools.partial`，可能形成旧实例与新实例同时绑定，表现为命令触发 `TypeError` 或仍访问已终止实例。
- 插件在 `initialize()` 最开始仅对自身精确模块名下、可证明安全的 handler binding 做规范化，使其只绑定当前实例一次；`terminate()` 不修改 AstrBot registry，也不触碰其他插件。
- 若兼容层无法确认 handler root、参数或 keywords 安全，则保持不变。遇到未覆盖的启停故障时，应急方式是完整重载 AstrBot；仍未恢复时重新安装最新验收包。
- 该兼容缺陷待上报；当最低支持的 AstrBot 版本已包含上游修复后，应删除此内部兼容层。

---

## 1.0.0 状态

已完成：

- Star 插件注册、`initialize` / `terminate` 生命周期。
- M1 稳定领域模型、GitHub URL 规范化与 schema v1 原子状态存储。
- 市场 API/raw fallback、Collection Issues、主仓旧 Issues 和 GitHub Search 四来源适配器。
- 可注入的 HTTP 边界、响应大小限制及无需安装 AstrBot、无需访问网络的 fixture 测试。
- M2 跨来源优先级合并、按来源静默基线、新增/实质更新检测和确定性中文摘要。
- 可靠 outbox、稳定事件/批次 ID、逐目标 at-least-once 重试及管理员手动检查推送。
- M3 GitHub 仓库元数据/Star 严格缓存、ETag、预算、限流降级和 fixed-delay 自动调度。
- M4 可选 AI 单段导语、安全 prompt/输出清洗、稳定 batch ID、阶段耗时与脱敏结构化日志。
- 默认离线测试、可选真实 AstrBot 集成契约和只读发布验收脚本。

- 2026-07-21 已完成安装/加载、两轮停用启用、重载、完整重启、卸载重装和非管理员权限验收。
- 已验证自定义唤醒词、群订阅及持久化、主动推送、默认来源检查和 pending outbox 跨完整重启投递清理。
- 已验证 LLM 默认/显式 Provider、10/30 秒超时降级与 60 秒成功。
- 已验证 GitHub 匿名、有效 Token、401 降级与恢复。真实 403/429 配额耗尽未主动制造，使用离线模拟和响应 headers 验证分类与降级。
- 完整脱敏结论见 [线上验收记录](docs/ONLINE_ACCEPTANCE.md)。

---

## 发布后维护

- 新功能、缺陷和技术债通过 GitHub Issues 跟踪，不继续扩张历史 PRD/FSD。
- 涉及架构、不变量、平台边界、安全或隐私模型的变更同步更新 [设计文档](docs/DESIGN.md)。
- 新增真实平台或宿主版本验收时同步更新 [线上验收记录](docs/ONLINE_ACCEPTANCE.md)。

---

## 开发验证

可执行以下离线验证：

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py market_watcher tests
python -m ruff check .
python -m ruff format --check .
git diff --check
python scripts/verify_release.py
python scripts/package_release.py
```

未安装 AstrBot 时，`tests/integration/` 会跳过。可通过安装 AstrBot，或在 `PYTHONPATH` 中提供真实 AstrBot 源码来运行集成契约；测试不硬编码相邻仓库路径。

---

## 许可证

本项目使用 [GNU Affero General Public License v3.0](LICENSE)。
