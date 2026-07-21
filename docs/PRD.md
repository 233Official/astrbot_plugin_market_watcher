# AstrBot 插件市场观察器产品需求文档

## 文档信息

- 产品：`astrbot_plugin_market_watcher`
- 版本：1.0
- 状态：已验收的 1.0.0 需求基线
- 维护方：233Official
- 更新时间：2026-07-21

---

## 背景

AstrBot 插件生态的信息分散在市场清单、发布 Issue、Collection 流程和 GitHub 仓库中。维护者与用户难以及时判断哪些插件刚发布、版本发生变化、仓库迁移、描述调整或热度上升，也容易因多个入口重复出现同一插件而收到重复消息。

本插件拟在 AstrBot 内提供低干扰、可解释、可降级的市场变化观察能力。系统必须先建立可靠基线，再报告后续变化；不得把抓取失败、来源差异或 AI 推测包装成确定事实。

---

## 文档生命周期

- 本 PRD 与 FSD 只服务 MVP；MVP 已作为 `1.0.0` 基线通过验收。
- 最终实现结构见 [设计文档](DESIGN.md)，真实环境结论见 [线上验收记录](ONLINE_ACCEPTANCE.md)。
- 1.0 后的新功能、缺陷和技术债通过 GitHub Issues 跟踪。
- Issue 引起设计变化时同步更新设计文档，不持续扩张 MVP PRD/FSD。

---

## 目标

- 聚合四类来源并转换为统一插件记录。
- 识别新增和实质更新事件。
- 通过规范化仓库地址和稳定标识对跨来源记录去重。
- 为变化生成确定性摘要，并可选使用 AI 改善可读性。
- 向明确配置的 AstrBot 会话进行节流、分批和可追踪的推送。
- 在 GitHub 限流、单源故障或 LLM 不可用时保持核心流程可用。

---

## 非目标

- 不提供插件自动安装、自动升级、代码执行或安全背书。
- 不替代 AstrBot 官方市场审核、发布流程或仓库治理。
- 不对插件质量、可信度或恶意性给出未经验证的结论。
- 不在首个可用版本中实现实时秒级监控、复杂 WebUI 或多租户服务。
- 不抓取私人仓库、私人 Issue 或用户未授权的数据。
- 不在 MVP 中生成移除/下架、仓库迁移或 Star 独立变化事件；这些作为 1.0 后 Issue 候选。
- 不在 MVP 中实现固定每日时刻或 cron 推送。

---

## 用户故事

- 作为 AstrBot 管理员，我希望每次轮询发现变化后收到新增和更新插件摘要，以便了解生态变化。
- 作为插件开发者，我希望同一发布在市场和 Issue 中出现时只提醒一次，并保留来源证据。
- 作为低配额用户，我希望不配置 Token 也能使用主要市场来源，并在 GitHub API 限流时得到明确降级提示。
- 作为隐私敏感用户，我希望 Token 不出现在日志和推送中，且不配置目标时绝不主动发送。
- 作为维护者，我希望能手动执行正常检查、查看本轮摘要、上次成功时间和错误摘要。

---

## 数据源

### 市场 API 或 plugins.json

- 定位：主要事实来源。
- 内容：插件名、展示名、描述、版本、作者、仓库地址、兼容版本及市场状态等可用字段。
- 策略：支持 ETag、`Last-Modified` 或内容哈希缓存；解析失败时保留上次成功快照，不将空结果视为全量删除。

---

### AstrBot 主仓 plugin-publish Issues

- 定位：兼容历史与残留信号，不作为新发布的主要权威来源。
- 原因：官方发布指南已说明主仓发布入口废弃，但历史 Issue、尚未迁移的自动化或残留提交仍可能提供线索。
- 策略：与 Collection Issues 并行采集，但默认关闭或降低优先级；记录 Issue 状态和链接，不单独据此认定已进入官方市场。

---

### AstrBot 插件 Collection Issues

- 定位：主要发布入口和候选来源，与市场数据共同构成主来源。
- 内容：提交仓库、作者声明、审核状态、讨论和关闭原因等公开信息。
- 策略：识别开放、关闭、重新打开和关键标签变化；只有满足明确状态规则时才转换为新增或实质更新候选事件。

---

### GitHub astrbot_plugin_* 全局发现

- 定位：补充发现来源，不代表官方收录或审核通过。
- 范围：公开仓库名或主题符合 `astrbot_plugin_*` 约定的候选仓库。
- 策略：低频运行，默认关闭；按更新时间分页，限制最大页数，对 fork、归档、镜像和明显无关仓库设置过滤规则。

两个 Issues 入口需要并行建模，以保留迁移期和历史事实；但由于官方发布指南称主仓 `plugin-publish` 入口已废弃，产品排序、可信度和默认开关均以 Collection/市场为主，主仓入口只承担兼容历史与残留信号的职责。

---

## 数据模型

统一插件记录至少包含：

- `canonical_id`：由规范化 GitHub `owner/repo` 优先生成的稳定标识。
- `name`、`display_name`、`description`、`author`、`version`。
- `repo_url`、`repo_owner`、`repo_name`、默认分支及仓库状态。
- `source_kind`、`source_url`、来源记录 ID 和来源优先级。
- `astrbot_version`、支持平台及市场状态。
- `stars`、`forks`、`archived`、`updated_at` 等可选 GitHub 指标。
- `observed_at`、`content_hash`、首次与最近出现时间。
- `raw`：受大小限制的原始字段，便于排障；不得存储 Token 或响应头中的敏感信息。

变化事件至少包含稳定事件 ID、事件类型、当前记录、上一记录、变化字段、证据来源和检测时间；推送状态由独立的待投递批次记录。

---

## 去重规则

1. 首选规范化后的 GitHub `owner/repo`，统一大小写、移除 `.git`、查询参数、片段和尾部斜杠。
2. 后续获得可规范化仓库地址时可将来源 fallback ID 合并到 GitHub canonical ID 并保留别名；MVP 不生成仓库迁移事件。
3. 仓库缺失时，使用来源 ID 作为临时键，不仅凭插件展示名合并。
4. 同一轮中市场记录优先于 Collection，Collection 优先于主仓兼容 Issue，全局发现最低。
5. 字段冲突保留来源证据和优先级，不覆盖为无来源的“最终真相”。
6. fork、镜像或重名仓库不自动合并；需要可解释规则或人工覆盖映射。

---

## 变化检测

- 首次运行只建立基线，默认不推送全部历史插件。
- 新增：稳定标识首次出现在可信来源中。
- 更新：版本、描述、兼容范围、支持平台、仓库地址或市场状态发生实质变化。
- Issue 状态：开放、关闭、重新打开、关键标签变化作为候选事件，不等同于市场发布。
- Star 数采集、缓存并显示在新增/更新摘要中；Star 变化本身不触发事件。
- 对列表排序、空白、大小写和无语义格式变化先规范化，再计算稳定哈希。
- 同一插件短时间多次变化合并为一个窗口摘要，避免消息风暴。
- 移除/下架、仓库迁移和 Star 独立变化事件不属于 MVP，留作 1.0 后 Issue 候选。

---

## Star 获取与缓存

- 仅对规范化后的 GitHub 仓库查询 Star，优先使用批量或条件请求能力。
- 缓存记录包含数量、获取时间、ETag、失败次数和下次允许请求时间。
- 默认缓存至少数小时；全局发现与 Star 刷新使用独立预算。
- 404、归档、迁移、权限拒绝和限流分别记录，不能全部解释为 Star 为零。
- 未配置 Token 时降低频率和每轮数量；接近限额时优先保障主要来源元数据，暂停非关键 Star 刷新。

---

## AI 摘要

- AI 摘要为可选增强，默认关闭；确定性变化列表始终是事实基础。
- 输入仅包含本轮必要的公开字段、变化前后值和来源链接，限制字符数和条目数。
- Prompt 要求区分“官方市场状态”“Issue 候选”“GitHub 补充发现”，禁止推断审核结论或安全性。
- 输出需经过长度限制和基本清洗；模型失败、超时或返回空内容时直接使用模板摘要。
- 生产 AI 导语与管理员 `test-ai` 共用 `ai_timeout_seconds`，默认 60 秒、允许 10 至 120 秒；到期取消单次 Provider 调用且不重试，继续使用事实模板。
- 真实线上验收中 10 秒和 30 秒均返回 `ai_timeout`，60 秒真实 Provider 调用成功，因此默认值调整为 60 秒。
- 不向模型发送 GitHub Token、AstrBot 配置、私人会话信息或完整原始响应。

---

## 推送策略

- 仅向 WebUI `push_targets` 或管理员群订阅明确产生的 UMO 推送；两者均为空时不主动推送。
- 首次基线、无变化、仅缓存刷新和单源临时错误默认不推送普通用户消息。
- 单次最多发送配置数量，超出部分给出计数和后续批次提示。
- 每次轮询对新事件即时批量推送；MVP 不实现固定每日时刻或 cron。
- 推送使用可跨重启恢复的 pending outbox 和有限重试，提供 at-least-once 语义；无法保证 exactly-once。
- 支持平台声明包含 `aiocqhttp` 与 QQ 官方 WebSocket `qq_official`；当前 QQ 验收优先群聊，C2C 留待后续验证，频道 cron 主动推送与 `qq_official_webhook` 不在承诺范围。
- UMO 由 AstrBot 提供并原样保存、投递；业务层不按平台改写实例 ID、消息类型或 session ID。

---

## 配置需求

- `enabled`：总开关，默认关闭。
- `poll_interval_minutes`：轮询间隔，需设置合理下限。
- `push_targets`：UMO 列表，默认空。
- `github_token`：可选，默认空，最小权限且永不记录明文。
- `llm_provider_id` 与 `enable_ai_summary`：摘要模型与开关。
- `ai_timeout_seconds`：AI Provider 单次调用超时，默认 60 秒，范围 10 至 120 秒。
- 四个 `source_*` 开关：市场、Collection 默认开；废弃主仓入口和全局发现默认关。
- `include_star_count`、请求超时、每轮和每次推送上限。
- MVP 不增加当前 schema 之外的用户配置；请求预算、Star TTL 和重试上限先作为内部常量。
- 群管理员可在群聊中订阅或取消当前官方 UMO；运行时将 WebUI 目标与群订阅合并，历史 outbox target snapshot 不随订阅变化。
- `llm_provider_id` 为空时，手动检查使用当前会话 origin，自动检查使用首个有效目标解析 AstrBot 默认 Provider；无 origin 或解析失败时降级纯事实模板。

---

## 命令需求

- `marketwatch status`：显示启用状态、任务状态、上次成功时间、来源状态、缓存规模和脱敏错误摘要。
- `marketwatch check`：管理员手动触发一次受互斥锁保护的正常检查与推送，并向调用者返回本轮摘要；若 AstrBot API 无法稳定表达管理员权限，则作为实现阻断项，不得自行绕过。
- `marketwatch test-push`：管理员仅可在群聊中向当前 `event.unified_msg_origin` 发送一条主动推送诊断消息；不得执行来源检查或修改插件、订阅、outbox 持久状态，失败仅返回脱敏错误类别。
- `marketwatch test-ai`：管理员仅可在群聊中使用固定虚构事实验证真实 Provider。显式 `llm_provider_id` 非空时优先使用，否则通过当前 `event.unified_msg_origin` 解析默认 Provider；该诊断不受 `enable_ai_summary` 开关阻止。
- AI 诊断成功时只返回经过现有输出校验的安全导语；Provider 缺失、超时、异常或输出不合规时明确返回脱敏错误类别和纯事实回退，不泄露 UMO、内部 prompt、Token 或供应商错误。
- AI 诊断不执行四来源检查，不创建或持久化市场事件，也不读写插件状态、订阅或 outbox。
- `marketwatch test-github`：管理员仅可在群聊中独立请求 GitHub `/rate_limit`，输出匿名或已配置 Token 模式、HTTP 状态、安全分类及 primary rate limit 摘要；不得输出 Token、Authorization、响应正文或 UMO。
- GitHub 诊断使用独立短生命周期 HTTP client，不复用生产 `GitHubGateway`，因此不消耗其 5/20 请求预算、不更新 `state.github.rate_limit`、不执行来源检查。`/rate_limit` 成功不证明其他端点权限。
- `marketwatch test-outbox-prepare/status/deliver/cleanup`：四个无参 canonical 管理员群聊命令，用于安全证明 pending outbox 可跨完整 AstrBot 重启恢复。prepare 仅持久化带长期 hold 的诊断 batch；status 只输出状态计数；deliver 解除 hold 后复用生产投递；cleanup 只删除诊断前缀。
- 诊断 batch 使用目标 SHA-256 派生的稳定 ID，同一群重复 prepare 幂等；不得输出 ID、UMO、消息、时间或异常原文。成功投递保留 sent 证据，失败沿用生产 attempts/backoff/exhausted 规则。
- 上述为不含唤醒词的 canonical 命令名；AstrBot 在命令 filter 前统一处理当前 `wake_prefix`，插件不自行解析或剥离前缀。
- MVP 不实现 `preview`、`sources`、`baseline` 等扩展命令；后续需求通过 Issue 跟踪。

---

## 异常与降级

- 单一来源失败不阻塞其他来源；结果标注为不完整。
- 市场全量响应异常时不执行删除检测。
- GitHub 限流时读取缓存、跳过低优先级请求，并计算重试时间。
- LLM 不可用时使用确定性模板摘要。
- 推送失败时保留待发送事件和有限重试状态；达到上限后进入冷却。
- 状态文件损坏时保留备份并进入只读或重建提示，不静默覆盖。
- 轮询任务异常需被记录；插件终止时正确处理 `CancelledError` 并等待任务结束。

---

## GitHub API 限流与 Token 安全

- 支持无 Token 运行，但降低请求预算；配置 Token 后读取速率限制响应头。
- Token 使用 Fine-grained PAT 和最小只读权限，不要求仓库写权限。
- 请求日志、异常、状态文件、推送和 AI 输入中必须清洗 Authorization、Token 及可能含凭据的 URL。
- 不在仓库、测试 fixture、默认配置或示例中放置真实 Token。
- 对 401 与 403 区分认证失败、权限不足和限流；认证失败后暂停使用 Token，避免持续重试。
- 尊重 `Retry-After` 和重置时间，加入抖动退避并限制并发。

---

## 隐私与安全

- 只处理公开插件生态数据和用户明确配置的会话标识。
- 状态持久化放在 AstrBot 插件数据目录，使用原子写入和最小文件权限。
- 原始响应设置大小上限，不执行仓库中的代码，不自动下载 Release 资产。
- 所有外部文本视为不可信输入；渲染前转义，避免日志注入、消息注入和 Prompt Injection 影响事实层。
- 不把“发现”描述为“官方认证”，不把 Star 数描述为安全或质量保证。

---

## 可观测性

- 结构化记录每轮开始/结束、耗时、各源记录数、缓存命中、变化数和推送结果。
- 记录上次成功、上次尝试、连续失败、限流重置时间和脱敏错误类别。
- 提供任务是否运行、状态版本和待推送数量，不输出 Token 或完整私人 UMO 列表。
- 为 fetch、normalize、deduplicate、detect、summarize、push 分阶段计时，便于定位瓶颈。
- MVP 已作为 `1.0.0` 基线于 2026-07-21 通过真实 AstrBot、公开 API、测试群 UMO 和受控 LLM Provider 验收。后续需求通过 GitHub Issues 跟踪，并在影响设计不变量时同步更新设计文档。

---

## 验收标准

- 插件可在受支持 AstrBot 版本加载、禁用、启用、重载和卸载，不遗留后台任务。
- 未配置 Token 和推送目标时仍可安全运行，且不会主动推送。
- 首次成功采集只建立基线；第二次相同输入不产生事件。
- 四类来源的 fixture 可标准化，跨来源同仓库只形成一个 canonical 插件。
- 市场或 Collection 的新增与版本变化能生成带来源证据的事件。
- 单源失败、空响应、GitHub 限流和 LLM 失败均按降级规则处理。
- 日志、状态、测试输出与消息中不出现 Token 明文。
- 静态测试无需安装 AstrBot；集成阶段另提供 AstrBot 环境测试。
- 真实 outbox 验收按 prepare → status pending=1 → 完整重启 AstrBot → status pending=1 → deliver → 群收到诊断消息 → status sent=1/pending=0 → cleanup → status count=0 执行。

---

## 里程碑

### M0：初始化骨架

- 生命周期、占位任务、命令、配置、接口、静态测试和文档。
- 不执行真实网络请求。

---

### M1：状态与主要来源

- 原子状态存储、市场/Collection fetcher、标准化、基线和 fixture 测试。

---

### M2：变化与推送

- 去重、变化检测、手动检查、批量模板摘要、目标推送和幂等记录。

---

### M3：GitHub 增强与调度

- 兼容主仓 Issue、全局发现、Star 缓存、限流预算和 fixed-delay 调度。

---

### M4：AI 与稳定性

- 可选 AI 摘要、可观测性、故障注入、AstrBot 集成测试和发布验收。

---

## 风险

- 官方市场端点或 Issue 模板变化导致解析失效。
- 多来源延迟不同，可能出现暂时冲突或重复事件。
- GitHub 无 Token 配额较低，全局发现容易消耗预算。
- 仓库重命名、转移、fork 和镜像可能造成错误合并。
- 删除判断不谨慎会把临时故障误报为下架。
- AI 可能夸大或混淆来源状态，必须保留确定性事实摘要。
- AstrBot 主版本升级可能改变插件生命周期、配置 schema 或推送 API。
- AstrBot v4.26.6 已确认停用后保留已绑定 handler、重新启用再次 partial 的生命周期缺陷；当前插件只在初始化阶段修复可安全证明的自身绑定，未知形状保持不变。缺陷待上报，最低支持版本包含上游修复后删除兼容层。

---

## 实现前待验证

- 验证市场 API、官方 `plugins.json` fallback、Collection 仓库与 Issue 标签的真实端点和字段；默认实现决策见 FSD。
- 验证 AstrBot 当前版本的管理员命令校验、UMO 主动消息和 LLM Provider API；无法稳定表达管理员权限时不得绕过。
