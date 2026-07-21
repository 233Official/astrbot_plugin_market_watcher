# Market Watcher 1.0.0 最终设计

本文描述 `astrbot_plugin_market_watcher` 1.0.0 的最终实现结构与长期不变量。PRD 和 FSD 保留需求形成过程与验收契约，本文不重复粘贴历史规格。

---

## 1.0 范围与非目标

1.0 提供四来源采集、规范化合并、新增与实质更新检测、可靠推送、群订阅、GitHub 元数据增强、可选 AI 导语、自动调度和管理员诊断。

- 首次成功采集只建立静默基线，不批量推送历史记录。
- 事实模板始终权威；Star 和 AI 只作增强，不改变事件判定。
- 不执行插件仓库代码，不下载 Release 资产，不评价安全性、质量或官方认证状态。
- 不承诺 QQ 官方频道 cron 主动推送、C2C 主动消息或 Webhook。
- 不把 1.0 变成通用工作流、审核、安装或自动升级系统。

---

## 架构模块与依赖方向

入口 `main.py` 只负责 AstrBot 生命周期、配置装配、命令和 adapter wiring。`market_watcher/service.py` 编排业务阶段，领域模块不反向依赖 AstrBot。

- `sources/` 与 `http.py`：外部事实采集和受限网络边界。
- `normalize.py`、`merge.py`、`detect.py`：纯领域转换与变化判定。
- `models.py`、`state.py`：严格 schema 与持久化。
- `github.py`：GitHub 预算、限流和缓存增强。
- `ai.py`：best-effort 导语与安全清洗。
- `outbox.py`、`summary.py`：确定性消息、目标快照和投递状态机。
- `scheduler.py`：fixed-delay 调度。
- `astrbot_adapter.py`、`astrbot_handler_compat.py`：唯一宿主耦合边界。

依赖方向保持为入口/adapter → service → 领域与端口；领域代码不得导入 AstrBot。

---

## 四来源与变化流水线

四来源分别是市场 API 或官方 `plugins.json` fallback、Collection Issues、主仓历史 `plugin-publish` Issues 和 GitHub 全局仓库发现。默认启用市场与 Collection，另外两项默认关闭。

采集结果先 normalize 为 `SourceObservation`，以规范化 GitHub 仓库优先形成 canonical ID。merge 按市场 > Collection > 主仓旧 Issues > GitHub discovery 的字段优先级生成 `PluginRecord`，同时保留字段来源与证据。

change detection 只比较白名单中的实质字段并生成 `discovered` 或 `updated`。来源失败、不完整响应和未建立基线的来源不得制造删除或伪变化；Star 波动不生成事件。

---

## State schema 与原子持久化

`state.json` 使用严格 schema v1，保存来源快照、合并插件、GitHub cache/rate-limit、outbox、订阅和最后运行报告。未知结构、类型错误或更高 schema 版本会被拒绝。

保存流程先写同目录临时文件、`fsync`、严格回读，再维护有效 `state.json.bak`，最后用原子替换更新主文件并同步目录。主文件损坏时读取备份；主文件和备份均无效时报告损坏，不静默覆盖证据。运行状态只写入 `StarTools.get_data_dir()` 提供的数据目录。

---

## Outbox 状态机与跨重启

每个 batch 固化事件 ID、事实消息和目标快照。每个目标独立经历 `pending`、`failed`、`sent` 或 `exhausted`。

- 创建 batch 后先保存 state，再尝试发送，保证 save-before-send。
- 失败按 1、2、4 秒递增并封顶 300 秒计算下次重试。
- 达到最大尝试次数后进入 `exhausted`，不再自动重试。
- 每次目标状态改变后立即保存，因此进程或宿主重启后可继续处理。
- 投递语义为逐目标 at-least-once；下游重复可见性优先于丢失通知。
- 新配置目标和订阅只影响新 batch，历史 batch 的目标快照不可变。

诊断 prepare 使用长期 hold，只有显式 deliver 或 cleanup 才解除；deliver 复用生产投递链路。

---

## Scheduler 与互斥锁

自动调度使用 fixed-delay：初始化等待 10 秒，单轮完成后再等待配置间隔。调度任务可取消，插件 terminate 时先停止任务再关闭 HTTP。

自动检查、手动检查、订阅写入和 outbox 诊断共享 service lock。检查发现锁已占用时返回 busy 并跳过，不排队；订阅和诊断则在锁内完成严格读改写，避免覆盖状态。

---

## GitHub auth、budget、cache 与 rate limit

GitHub Token 可选，只注入 exact `https://api.github.com`。每轮共享预算为 Token 模式 20 次、匿名模式 5 次，并发上限 2。元数据缓存使用 ETag，Token 模式 TTL 为 6 小时，匿名模式为 24 小时。

请求按 Collection、旧 Issues、事件相关元数据、普通元数据、GitHub Search 的顺序分配价值。401 会在本轮禁用 Token 并收缩到匿名预算；403 根据 headers 与安全截断的响应信号区分权限不足和限流；429 或 secondary rate-limit 会阻止本轮后续 GitHub 请求。已有 cache 在失败时保留为 stale，而不是清空事实。

---

## AI best-effort 与安全输出

AI 只生成一段可选中文导语，不生成、修改或删除事实列表。显式 `llm_provider_id` 优先；为空时，手动检查按当前会话 UMO 解析默认 Provider，自动检查按首个有效目标解析。

单次 Provider 默认超时 60 秒，不自动重试。Provider 缺失、超时、异常、错误角色、空输出或超过 120 字均回退纯事实模板，不阻塞 save 或 outbox。

Prompt 只包含受限的公开规范化字段，限制事件数、字符数和 URL；不包含 Token、UMO、配置、raw excerpt 或完整响应。输出会移除控制字符并中和 CQ、mention 与 Markdown 控制字符。

---

## 平台与主动消息边界

`aiocqhttp` 与 QQ 官方 WebSocket `qq_official` 是 metadata 声明的平台。业务层只传递 AstrBot 提供的 UMO 给 `Context.send_message`，不解析群号、不重写 UMO、不实现平台专用重发。

1.0 已真实验证 QQ 官方 WebSocket 群聊。QQ 官方频道主动推送依赖额外消息上下文，Webhook 尚未验收，二者均不作承诺；C2C 主动消息也留待后续独立验收。

---

## Canonical 命令与唤醒规则

插件只注册 canonical 命令组 `marketwatch` 及子命令，不注册 `/`、`!` 或其他前缀。`wake_prefix`、@机器人、回复机器人和消息唤醒判断全部由 AstrBot 宿主管理，插件不得重复剥离或解析唤醒词。

---

## 管理员诊断与副作用边界

- `status` 只读本地状态，不要求管理员权限。
- `check` 执行真实来源检查、状态保存、新 batch 创建和投递。
- `test-push` 只向当前群直接发送诊断消息，不修改 state 或 outbox。
- `test-ai` 使用固定虚构事实调用生产 AI 路径，不读写 state。
- `test-github` 使用独立 client 请求 `/rate_limit`，不消耗生产预算、不持久化响应。
- `test-outbox-prepare/status/deliver/cleanup` 只操作带专用前缀的诊断 batch；deliver 可能同时处理其他已到期真实 pending。
- `subscribe/unsubscribe/subscriptions` 只保存或汇总当前群 UMO，不向用户列出完整 UMO。

除 `status` 外，上述业务与诊断命令均受 AstrBot 管理员权限保护；群限定命令拒绝私聊。

---

## AstrBot v4.26.6 handler rebinding shim

AstrBot v4.26.6 停用插件后可能保留已绑定 handler，再启用时形成旧实例与新实例的重复 partial。兼容层只在 `initialize()` 开始运行，并仅处理当前插件精确模块、原始函数 identity、参数、keywords 和实例类型均可证明安全的 binding。

它不修改命令签名，不触碰其他插件 registry，`terminate()` 也不清理 registry。任何条件不明确时保持原状并依赖完整重启恢复。

当最低支持的 AstrBot 版本已包含上游 handler 生命周期修复，且两轮停用启用回归在该最低版本通过后，应删除 shim、对应 wiring 和专用测试。

---

## 安全、隐私与日志

- 默认 Token 为空、推送目标为空、自动调度关闭。
- 外部响应有大小限制，所有外部文本均视为不可信输入。
- 日志只记录阶段、计数、耗时和稳定错误类别，不记录 Token、Authorization、响应正文或完整 UMO。
- 状态会保存用户明确配置或订阅的 UMO，因此宿主数据目录必须限制文件访问权限。
- Star 不是安全或质量证明，发现结果也不表示官方认证。

---

## 发布打包与后续演进

`package_release.py` 生成单一版本化顶层目录、固定时间戳和权限的确定性 ZIP，并写 SHA-256 sidecar。包包含运行文件、README、CHANGELOG、LICENSE、PRD、FSD、DESIGN、ONLINE_ACCEPTANCE 和 Playbook，不包含 tests、scripts、缓存、环境文件或 `pyproject.toml`。

`verify_release.py` 检查三个版本源、必需文件、README 文档入口、安全默认值、链接、凭据模式、包上下文导入和离线测试。

1.0 之后的功能、缺陷和技术债通过 GitHub Issues 跟踪。任何影响本文件所述架构、不变量、平台边界、安全或隐私模型的变更，必须与代码和测试同步更新本文；新的真实验收范围同步更新 `ONLINE_ACCEPTANCE.md`。
