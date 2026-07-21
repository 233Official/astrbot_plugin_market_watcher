# 命令参考

本文列出 `main.py` 当前注册的全部 `marketwatch` 子命令及其实际权限边界。

---

## 唤醒方式

插件注册的命令组名称是 `marketwatch`，不包含唤醒词。AstrBot 会按当前 `wake_prefix` 处理消息：

- 默认 `/` 前缀示例：`/marketwatch status`。
- `!` 前缀示例：`!marketwatch status`。
- `wake_prefix=[]` 时，群聊通常需要先 `@机器人`，再发送 `marketwatch status`。

不要在插件配置中使用会匹配所有消息的空字符串前缀。下文统一使用默认 `/` 作为展示示例。

---

## 日常命令

| 命令 | 权限 | 会话限制 | 实际行为 |
| --- | --- | --- | --- |
| `/marketwatch status` | 所有用户 | 无额外限制 | 只读取本地状态，显示来源、调度、GitHub 预算、目标、订阅、pending、exhausted 和最后运行报告 |
| `/marketwatch check` | AstrBot 管理员 | 无额外限制 | 立即执行已启用来源的检查、基线或变化检测、outbox 处理，并返回中文运行摘要 |
| `/marketwatch subscribe` | AstrBot 管理员 | 仅群聊 | 将当前 `event.unified_msg_origin` 幂等加入持久化订阅 |
| `/marketwatch unsubscribe` | AstrBot 管理员 | 仅群聊 | 幂等移除当前群的持久化订阅 |
| `/marketwatch subscriptions` | AstrBot 管理员 | 仅群聊 | 仅显示订阅总数和当前群是否已订阅，不列出 UMO |

`check` 与自动检查复用同一服务锁。变化推送目标由 `push_targets` 与持久化群订阅合并后产生。

---

## 诊断命令

| 命令 | 权限 | 会话限制 | 实际行为与状态影响 |
| --- | --- | --- | --- |
| `/marketwatch test-push` | AstrBot 管理员 | 仅群聊 | 直接向当前会话发送测试消息；不采集来源，不修改订阅或 outbox |
| `/marketwatch test-ai` | AstrBot 管理员 | 仅群聊 | 用固定虚构事实调用真实 Provider；不受 `enable_ai_summary` 阻止，不修改持久状态 |
| `/marketwatch test-github` | AstrBot 管理员 | 仅群聊 | 请求 GitHub `/rate_limit`，验证匿名或 Token 认证和 primary rate limit；不使用生产检查预算 |
| `/marketwatch test-outbox-prepare` | AstrBot 管理员 | 仅群聊 | 幂等创建一条长期 hold 的诊断 pending；不会自动投递 |
| `/marketwatch test-outbox-status` | AstrBot 管理员 | 仅群聊 | 显示诊断 batch 的 `count`、`pending`、`failed`、`sent`、`exhausted` 计数 |
| `/marketwatch test-outbox-deliver` | AstrBot 管理员 | 仅群聊 | 解除诊断 hold 并调用生产 outbox 投递；其他已到期真实 pending 也可能同时处理 |
| `/marketwatch test-outbox-cleanup` | AstrBot 管理员 | 仅群聊 | 幂等删除全部诊断 batch，不影响真实 `batch:` 项 |

---

## 关键结果说明

### `status`

- 插件未初始化时返回“市场观察器尚未完成初始化”。
- 状态文件不可用时以安全类别显示健康异常，不输出敏感目标明细。
- `pending` 与 `exhausted` 分开统计：前者仍可能重试，后者已达到最大尝试次数。

### `check`

- 第一次成功采集某来源时只建立静默基线。
- 后续新增或实质更新才生成事件和推送批次。
- AI 导语失败、超时或不合规时回退纯事实模板，不阻塞事实投递。

### 订阅命令

- 私聊执行会明确拒绝，不会写入订阅。
- 重复订阅和重复取消均为幂等操作。
- `subscriptions` 不公开任何完整 UMO。

---

## 诊断使用建议

- 主动消息链路优先使用 `test-push`，它不会修改生产 outbox。
- Provider 排查使用 `test-ai`；显式 `llm_provider_id` 优先，否则解析当前会话默认 Provider。
- GitHub 认证排查使用 `test-github`；不要把 Token、Authorization header 或响应正文贴入 Issue。
- outbox 跨重启测试必须以 `prepare` 开始，并最终执行 `deliver` 或 `cleanup`，避免长期保留诊断 pending。
- `test-outbox-deliver` 会进入生产投递链路，执行前应确认没有不希望同时处理的到期真实 pending。

完整线上验收顺序见[线上验收记录](./ONLINE_ACCEPTANCE.md)，开发与故障定位边界见[插件开发与发布 Playbook](./PLUGIN_DEVELOPMENT_PLAYBOOK.md)。

---

## 权限与安全

- 代码仅为 `status` 保留无管理员权限访问；其余全部子命令使用 `PermissionType.ADMIN`。
- “AstrBot 管理员”是 AstrBot 权限判断结果，不应自行外推为任意平台的群管理员身份。
- 诊断回复只应包含安全错误类别和计数，不应包含 Token、完整 UMO、响应正文或内部配置。
- 在公开反馈中提供命令名、AstrBot 版本、adapter 类型与脱敏错误类别即可。
