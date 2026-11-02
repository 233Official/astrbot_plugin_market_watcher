# 配置参考

本文逐项说明 `_conf_schema.json` 暴露的当前配置，以及 `market_watcher/config.py` 和 `main.py` 的默认回退行为。

---

## 总览

| 配置键 | 类型 | Schema 默认值 | 有效范围或取值 | 敏感 |
| --- | --- | --- | --- | --- |
| `enabled` | `bool` | `false` | `true` / `false` | 否 |
| `poll_interval_minutes` | `int` | `30` | `5`–`1440` | 否 |
| `push_targets` | `list` | `[]` | UMO 字符串列表 | 可能包含会话标识 |
| `github_token` | `string` | `""` | 可选 Fine-grained Token | 是 |
| `llm_provider_id` | `string` | `""` | AstrBot Provider ID | 可能暴露内部命名 |
| `source_market_api` | `bool` | `true` | `true` / `false` | 否 |
| `source_github_discovery` | `bool` | `false` | `true` / `false` | 否 |
| `include_star_count` | `bool` | `true` | `true` / `false` | 否 |
| `enable_ai_summary` | `bool` | `false` | `true` / `false` | 否 |
| `ai_timeout_seconds` | `int` | `60` | `10`–`120` | 否 |
| `request_timeout_seconds` | `int` | `15` | `5`–`60` | 否 |
| `max_items_per_push` | `int` | `10` | `1`–`50` | 否 |
| `enable_image_card` | `bool` | `true` | `true` / `false` | 否 |
| `image_render_timeout_seconds` | `int` | `8` | `3`–`20` | 否 |

---

## 调度与推送

### `enabled`

- `false`：不启动 fixed-delay 自动调度，管理员仍可执行 `marketwatch check`。
- `true`：插件初始化后启动自动调度。
- 非布尔值按 `false` 回退。

### `poll_interval_minutes`

- 每轮自动检查完成后，再等待指定分钟数，不是固定时刻调度。
- 有效范围为 `5`–`1440`；缺失、类型错误或越界时按 `30` 回退。

### `push_targets`

- 填写 AstrBot unified message origin（UMO）列表。
- 第一段必须是 WebUI 中配置的 adapter 实例 ID，不是固定平台名。
- 示例格式只能使用占位值：`qqws:GROUP_MESSAGE:<group_openid>`。
- 推荐在目标群执行 `marketwatch subscribe`，直接保存 AstrBot 提供的准确 UMO，避免手工猜测。
- 运行时只接受 list；元素必须是非空字符串且不超过 512 字符，随后去空白、去重并排序。
- 配置目标会与持久化群订阅合并。两者都为空时，不创建变化推送批次。
- UMO 可能包含会话标识，不要写入公开 Issue、截图或日志正文。

---

## GitHub 与网络

### `github_token`

- 可选，仅用于提高 GitHub API 限额；默认空字符串表示匿名请求。
- 应使用满足读取公开信息所需的最小权限 Fine-grained Token。
- 字符串会去除首尾空白；非字符串按空值回退。
- 当前 AstrBot 插件 schema 未提供通用 password 输入类型，WebUI 可能以普通字符串框展示；请限制配置文件和管理界面权限。
- 不要把 Token 放入聊天、日志、截图、Issue、命令示例或版本库。

### `include_star_count`

- `true` 时请求、缓存并在事件摘要中包含 GitHub Star 数。
- `false` 时跳过该补充指标，可减少 GitHub 请求。
- 非布尔值按 `true` 回退。

### `request_timeout_seconds`

- 控制单次 HTTP 请求超时，默认 `15` 秒。
- 有效范围为 `5`–`60`；缺失、类型错误或越界时按 `15` 回退。
- 调高会延长故障等待时间，调低可能增加慢网络下的采集失败。

---

## AI 导语

### `enable_ai_summary`

- 默认 `false`。
- 启用后只为事实列表生成一段短导语；失败、超时或输出不合规时自动回退纯事实模板。
- 非布尔值按 `false` 回退。
- 该开关不阻止管理员使用 `marketwatch test-ai` 诊断真实 Provider。

### `llm_provider_id`

- 可选，可在 AstrBot WebUI 的 Provider 选择器中指定。
- 留空时，手动 `check` 使用当前会话默认 Provider；自动检查使用首个有效推送目标的默认 Provider。
- 字符串会去除首尾空白；非字符串按空值回退。
- Provider ID 可能体现内部部署命名，公开反馈前应脱敏。

### `ai_timeout_seconds`

- 生产 AI 导语和 `test-ai` 共用的单次 Provider 超时，默认 `60` 秒。
- 有效范围为 `10`–`120`；缺失、类型错误或越界时按 `60` 回退。
- 超时后取消本次调用并回退事实模板，不重试，也不阻塞 outbox 投递。

---

## 数据源开关

| 配置键 | 默认状态 | 说明与风险 |
| --- | --- | --- |
| `source_market_api` | 开启 | 使用 AstrBot Cloud 唯一市场事实来源；失败保留快照，无旧目录回退 |
| `source_github_discovery` | 关闭 | 搜索 `astrbot_plugin_*` 仓库，成本和误报率较高 |

这些键由 `main.py` 直接读取：只有实际布尔值 `true` 才启用；键缺失或类型不是 `bool` 时按关闭处理。正常通过 WebUI 保存时，应由 schema 提供上表默认值。

旧发布流程的开关已删除，旧配置遗留值不再启用采集。GitHub 元数据只补充已有仓库，不扩大市场清单；独立搜索仍由自己的开关控制。

### #4 迁移与停用

`enabled=false` 仅停自动检查，不停出站箱 drain 或手动检查。保持线上插件停用；未来部署前备份 `state.json` 和 `.bak` 并审查隔离队列。

schema v1/v2 升级到 v3 后，旧市场首次成功完整 Cloud 结果静默重建；失败不消耗迁移，重启幂等。含 pending/failed/exhausted 的旧生产批次隔离保留，不自动重放，不清空全部 outbox。管理员通过 `migration` 查看安全计数，`migration_release` / `migration_discard` 仅处置隔离批次，保留 sent 状态，不影响新队列和诊断批次；release 不重置 exhausted 或重试计数。

---

## 消息大小

### `max_items_per_push`

- 每个推送分片最多包含的变化条目数，默认 `10`。
- 有效范围为 `1`–`50`；缺失、类型错误或越界时按 `10` 回退。
- 调高可能产生更长消息；插件仍会按内部消息长度边界组织事实摘要。
- 当 `enable_image_card` 为 `true` 时，分页容量由 `image_card_max_items` 控制，纯文本模式仍使用本项。

---

## 图片卡片

### `enable_image_card`

- 默认 `true`。
- 开启后优先尝试将变化摘要渲染为 HTML 图片卡片再推送；渲染异常、超时或返回空/非法结果时，立即回退纯文本发送。
- 关闭时维持纯文本行为，`max_items_per_push` 恢复原有控制。
- 非布尔值按 `true` 回退。

### `image_render_timeout_seconds`

- 单次 HTML→图片渲染超时上限，默认 `8` 秒。
- 有效范围为 `3`–`20`；缺失、类型错误或越界时按 `8` 回退。
- 超时或取消时不会触发文本回退内的再次渲染，直接使用原始 `message` 文本发送。

### `image_card_max_items`

- 图片页最大事件数，默认 `15`，允许 `5`、`10`、`15`；其他值按 `15` 回退。
- 均衡分页保留全部事件，例如 16 项为 8+8，50 项为 13+13+12+12；页码在重试和重启后保持不变。

### `max_images_per_delivery`

- 每目标每个 60 秒窗口的图片投递尝试上限，默认 `2`，范围 `1`–`10`。
- 真正进入发送才占配额；成功、失败及文本回退均计入。超额保持 pending，由独立 drain 在后续窗口续传，不消耗重试次数。

### `image_send_interval_seconds`

- 同目标相邻图片投递尝试的最小间隔，默认 `2` 秒，范围 `0`–`30`。
- 窗口和最后尝试时间持久化，手动检查、自动检查、独立 drain 和重启共享同一账本。

---

## 推荐配置

首次使用可采用以下最小思路，不需要填写任何凭据：

```json
{
  "enabled": false,
  "push_targets": [],
  "github_token": "",
  "enable_ai_summary": false
}
```

然后在目标群由 AstrBot 管理员执行 `marketwatch subscribe` 和 `marketwatch check`。确认手动路径与主动推送可用后，再考虑启用 `enabled`、配置 Token 或 AI 导语。

---

## 安全检查清单

- 保持 `github_token` 为空，除非匿名额度确实不足。
- 不在公开材料中展示完整 `push_targets`、Provider ID 或状态文件。
- 不手工拼接或传播包含真实会话标识的 UMO 示例。
- 修改数据源和轮询间隔后观察 `marketwatch status`，确认失败来源与预算符合预期。
- AI 不是事实判断的唯一来源；关闭 AI 不影响核心检测与推送。
