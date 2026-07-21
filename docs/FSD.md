# AstrBot 插件市场观察器 MVP 功能规格文档

## 文档信息

- 产品：`astrbot_plugin_market_watcher`
- 文档版本：1.0
- 状态：已验收的 1.0.0 实现基线
- 对应 PRD：[`docs/PRD.md`](PRD.md)
- 适用版本：1.0.0 基线
- 维护方：233Official
- 更新时间：2026-07-21

本文把 MVP 需求落实为可实现、可测试的功能契约。MVP 已作为 `1.0.0` 基线通过验收，最终结构见 [设计文档](DESIGN.md)，真实环境结论见 [线上验收记录](ONLINE_ACCEPTANCE.md)；此后的功能、缺陷与技术债通过 GitHub Issues 跟踪，设计变化同步到设计文档，不继续扩张本 FSD。

---

## 适用范围与 MVP 定义

MVP 必须完成一条可在 AstrBot 中长期运行的闭环：采集四类公开来源，规范化并合并插件记录，按来源静默建立基线，检测“新增”和“实质更新”，生成事实摘要，可选生成 AI 导语，将事件可靠地推送到明确配置的 UMO，并持久化运行状态与待投递批次。

四类来源适配器及其 fixture 均属于 MVP：

- AstrBot 市场 API 或 `plugins.json`。
- AstrBot 插件 Collection Issues。
- AstrBot 主仓旧 `plugin-publish` Issues。
- GitHub `astrbot_plugin_*` 全局发现。

默认只启用市场和 Collection；主仓旧 Issues 与 GitHub 全局发现默认关闭。默认关闭表示运行时成本与可信度策略，不表示移出 MVP。

MVP 事件只有：

- `discovered`：新增。
- `updated`：实质更新。

以下不在 MVP：

- 移除、下架或连续缺失事件。
- 仓库迁移独立事件、自动跟随重定向后的迁移通知。
- Star 独立变化事件或热度阈值通知。Star 数仍需采集、缓存并显示在摘要中。
- 固定每日时刻、cron、实时秒级监控。
- 自动安装、升级、执行插件代码、安全审核或质量评级。
- 私有仓库、私有 Issue、WebUI、多租户、人工别名管理命令。
- `preview`、`sources`、`baseline` 等扩展命令。

---

## 总体处理流程

每轮检查必须按以下顺序执行：

1. 获取统一的 `asyncio.Lock`；定时任务抢锁失败则跳过，手动命令抢锁失败则返回“忙碌”。
2. 读取并校验 `state.json`；若状态不可安全使用，停止本轮。
3. 按开关和优先级采集来源，得到各自的 `FetchResult`。
4. 对成功来源执行解析、规范化与来源内去重；失败来源沿用旧状态。
5. 跨来源合并为本轮 `PluginRecord`，补充预算内的 GitHub 元数据和 Star 缓存。
6. 对每个首次成功的来源静默建立来源基线；仅对已有来源基线的数据检测事件。
7. 折叠同轮重复事件；GitHub metadata 完成后可调用一次 LLM 生成可选导语，再生成稳定事件 ID 与 `DeliveryBatch`。
8. 先原子保存新快照和 pending outbox；保存失败时禁止推送。
9. 向明确的 `push_targets` 分批投递；每次结果均原子更新 outbox。
10. 保存轮次统计、来源健康与脱敏错误摘要。

相同旧状态和相同输入必须得到相同快照，且产生零个新事件。

---

## 数据源契约

### 固定端点与优先级

端点作为实现内部常量，不新增用户配置。实现前需用真实响应验证地址和字段；若官方地址变化，只修改适配器常量与 fixture，不改变领域层契约。

| 优先级 | 来源 | 固定 URL/API 形态 | 默认开关 | 可信度与用途 |
| --- | --- | --- | --- | --- |
| 1 | 市场 | `GET https://api.soulter.top/astrbot/plugins`；fallback：`GET https://raw.githubusercontent.com/AstrBotDevs/AstrBot_Plugins_Collection/main/plugins.json` | `source_market_api=true` | 已进入市场的主要事实来源 |
| 2 | Collection Issues | `GET https://api.github.com/repos/AstrBotDevs/AstrBot_Plugins_Collection/issues?state=all&per_page=100&page={n}` | `source_collection_issues=true` | 当前主要发布入口和候选事实来源 |
| 3 | 主仓旧 Issues | `GET https://api.github.com/repos/AstrBotDevs/AstrBot/issues?state=all&labels=plugin-publish&per_page=100&page={n}` | `source_plugin_publish_issues=false` | 仅兼容历史与残留信号，不证明已收录 |
| 4 | GitHub 全局发现 | `GET https://api.github.com/search/repositories?q=astrbot_plugin_+in:name&sort=updated&order=desc&per_page=100&page={n}` | `source_github_discovery=false` | 补充发现，不表示官方审核或收录 |

市场 fallback 的准确 raw URL、Collection 仓库名及标签规则是实现前端点验证项。默认决策是保持表中主地址，适配器允许一个代码内置 fallback，不增加配置键，也不阻塞 FSD 完成。

### 市场来源

- 期望形态：JSON 对象内的插件数组，或顶层插件数组；适配器显式支持经 fixture 固化的两种形态。
- 完整性：HTTP 成功、内容类型可接受、响应未超大小限制、JSON 可解析、插件数组存在，且记录级解析成功率不低于 95%。空数组只有在端点明确返回有效全量语义时才算成功；否则按失败处理。
- 分页：若 API 返回分页字段，必须遍历到 `next` 为空或达到内部页数上限；`plugins.json` 视为单页全量。
- 缓存：优先发送 `If-None-Match`，其次 `If-Modified-Since`；`304` 使用旧来源快照并记为成功且未变化。
- fallback：主 API 网络失败、5xx、格式不兼容或不完整时尝试一次官方 `plugins.json`；fallback 成功时来源仍记为 `market`，同时记录实际端点。

### Collection Issues

- 采集普通 Issue，不把 Pull Request 混入。
- 从标题、正文、表单字段、标签和 Issue URL 提取仓库、插件名、作者声明与状态证据。
- 完整性：从第 1 页开始连续获取，直到返回数量小于 `per_page` 或 `Link` 无 `next`；任何中间页失败则整源不完整，本轮不得以部分结果覆盖旧快照。
- Issue 的关闭、重开和标签变化可作为 `updated` 的来源字段证据，但摘要必须称为“Collection 提交状态”，不得称为“市场已收录”，除非市场来源同时证明。

### 主仓旧 Issues

- 解析和分页规则与 Collection 相同，但必须限定 `plugin-publish` 标签或经 fixture 验证的历史发布模板。
- 无论 Issue 状态如何，其事实优先级均低于市场和 Collection。
- 默认关闭；开启时用于补齐历史仓库地址、名称和来源链接，不得覆盖高优先级来源的非空字段。

### GitHub 全局发现

- 仅接受公开、非 fork、非镜像、未归档，且仓库名以前缀 `astrbot_plugin_` 开始的结果；大小写比较不敏感。
- 搜索 API 最多读取内部常量 `2` 页，每页 `100` 条；这是内部成本上限，不是用户配置。
- 当 `incomplete_results=true`、页中断或预算不足时，`FetchResult.complete=false`；已有状态不得被部分结果覆盖。
- 全局发现只能生成“GitHub 补充发现”证据，不能生成“已进入市场”的表述。

### 来源 fallback 与失败原则

- 单源失败不阻塞其他来源完成，但失败来源的旧 observations 和基线必须原样保留。
- 只有 `success=true` 且 `complete=true` 的来源才可替换该来源快照。
- `success=true, complete=false` 的结果只用于来源健康与诊断统计，不参与合并、事件检测或快照替换。
- 四类来源均失败时，本轮为失败；不生成事件、不建立基线、不推送普通摘要。

---

## 模块边界与计划目录树

依赖方向必须保持为 `main/AstrBot 适配层 -> application 编排 -> domain 纯逻辑`；`infrastructure` 实现网络、状态、LLM 和推送端口，但领域层不得导入 AstrBot、HTTP 客户端或文件系统。

不为每个小函数建立 `Protocol`。仅在需要替换外部 I/O 或进行测试注入时保留少量端口：来源抓取、状态存储、LLM、消息投递和时钟。领域模型、规范化、合并、变化检测使用普通函数或具体服务。

```text
astrbot_plugin_market_watcher/
├── main.py                         # AstrBot 生命周期、命令和调度入口
├── market_watcher/
│   ├── models.py                   # 领域值对象和枚举
│   ├── normalize.py                # URL、文本和来源记录规范化
│   ├── merge.py                    # 跨来源合并与同轮折叠
│   ├── detect.py                   # 新增/实质更新纯逻辑
│   ├── render.py                   # 确定性事实模板
│   ├── service.py                  # 单轮检查应用编排
│   ├── state.py                    # schema v1、原子写和 outbox
│   ├── github.py                   # GitHub 客户端、缓存、预算和限流
│   ├── sources/
│   │   ├── market.py
│   │   ├── collection_issues.py
│   │   ├── legacy_publish_issues.py
│   │   └── github_discovery.py
│   ├── ai.py                       # AstrBot LLM Provider 适配
│   └── delivery.py                 # UMO 分批投递
└── tests/
    ├── fixtures/                   # 四类来源及异常响应 fixture
    ├── test_normalize.py
    ├── test_merge_detect.py
    ├── test_state_outbox.py
    ├── test_sources.py
    ├── test_github_client.py
    ├── test_render_ai.py
    └── test_lifecycle_integration.py
```

该树是职责与依赖基线，不要求一次性机械搬迁现有骨架；实际实现可在不破坏边界的前提下合并小模块。

---

## 统一领域模型

### SourceObservation

表示某来源中的一条原始观察：

- `source_kind`：`market | collection_issue | legacy_publish_issue | github_discovery`。
- `source_record_id`：来源内稳定 ID，例如市场条目键、Issue number、GitHub repository ID。
- `source_url`：用户可访问的公开证据链接。
- `fetched_from`：实际请求端点，不得含认证信息。
- `observed_at`：UTC ISO 8601 时间。
- `repo_url`、`name`、`display_name`、`description`、`author`、`version`。
- `astrbot_version`、`platforms`、`market_status`、`issue_state`、`issue_labels`。
- `stars`、`forks`、`archived`、`repo_updated_at`。
- `content_hash`：对规范化的实质字段计算的 SHA-256。
- `raw_excerpt`：受限且脱敏的排障字段，序列化后最大 8 KiB。

### FetchResult

- `source_kind`。
- `success`：请求和解析是否达到可用条件。
- `complete`：是否确认覆盖该来源本轮应采集范围。
- `observations`：解析成功的 `SourceObservation` 列表。
- `endpoint`、`http_status`、`etag`、`last_modified`。
- `pages_fetched`、`records_received`、`records_rejected`。
- `from_cache`、`stale_cache_used`。
- `rate_limit_remaining`、`rate_limit_reset_at`。
- `error_code`、`error_summary`：稳定类别与脱敏摘要。
- `started_at`、`finished_at`。

### PluginRecord

- `canonical_id`：首选 `github:{owner}/{repo}`，否则为来源约束 fallback ID。
- `repo_owner`、`repo_name`、`repo_url`。
- `name`、`display_name`、`description`、`author`、`version`。
- `astrbot_version`、`platforms`、`market_status`。
- `stars`、`forks`、`archived`、`repo_updated_at`。
- `first_seen_at`、`last_seen_at`、`observed_at`。
- `field_sources`：每个合并字段采用的来源及来源记录 ID。
- `evidence`：参与合并的来源、记录 ID、链接和观察时间列表。
- `content_hash`：只覆盖实质更新字段，不包含 Star、抓取时间和来源顺序。

### ChangeEvent

- `event_id`：稳定 ID，算法见投递章节。
- `kind`：仅 `discovered | updated`。
- `canonical_id`。
- `current`、`previous`：当前与上一份 `PluginRecord`；新增的 `previous=null`。
- `changed_fields`：排序后的实质字段名。
- `evidence`：支撑本事件的来源证据。
- `detected_at`、`run_id`。
- `fact_lines`：确定性事实模板的结构化输入，不保存 LLM 推断。

### DeliveryBatch

- `batch_id`：稳定 ID。
- `target`：完整 UMO 仅存状态，不写普通日志；日志使用哈希或遮罩值。
- `event_ids`：按稳定顺序排列。
- `message`：最终待投递文本；不得含 Token。
- `created_at`、`next_attempt_at`、`last_attempt_at`。
- `attempts`、`max_attempts`：MVP 内部常量为 `5`。
- `status`：`pending | delivered | exhausted`。
- `last_error_code`、`last_error_summary`。

---

## canonical ID 与规范化

对 GitHub 仓库 URL 执行以下确定性步骤：

1. 仅接受 `github.com`、`www.github.com` 及经验证的 GitHub API repository URL。
2. URL host 小写，提取恰好两段路径 `owner/repo`。
3. owner、repo 转小写；移除 repo 末尾 `.git`。
4. 丢弃 query、fragment 和尾斜杠。
5. percent-decoding 后重新校验字符；拒绝空段、`.`、`..` 或额外路径伪装。
6. 输出规范 URL `https://github.com/{owner}/{repo}` 与 `canonical_id=github:{owner}/{repo}`。

无法规范化仓库时使用来源约束 fallback：

```text
source:{source_kind}:{normalized_source_record_id}
```

`normalized_source_record_id` 必须来自来源稳定主键并做小写、去首尾空白和安全转义，绝不只使用展示名。

后续某轮获得可规范化仓库时：

- 若 fallback 记录与 GitHub ID 共享同一来源稳定 ID，则合并到 GitHub ID。
- 迁移时保留 `id_aliases[fallback_id] = github_id`，旧记录、事件与 pending batch 仍可解析。
- 若只有同名、同作者等弱证据，不自动合并。
- MVP 不生成仓库迁移事件；canonical ID 改善只作为内部归并。

---

## 跨来源合并与同轮折叠

字段优先级默认是市场 > Collection > 主仓旧 Issues > GitHub 全局发现。合并规则：

- 高优先级非空值覆盖低优先级值；空值不得覆盖非空值。
- GitHub 仓库实时字段 `stars`、`forks`、`archived`、`repo_updated_at` 以仓库 API 缓存为准，不从文本 Issue 覆盖。
- 所有冲突均保留在 `field_sources` 和 `evidence`，摘要只展示选定值及关键来源。
- 列表字段先去空白、大小写归一、去重并排序；描述统一换行和首尾空白，但不改写语义。
- 同一来源出现重复稳定 ID 时，保留最新 `updated_at`；无法判定时拒绝该组并记录解析错误，不随意取第一条。

同轮折叠以最终 `canonical_id` 为键：

- 同一插件在多个来源首次出现，只产生一个 `discovered`。
- 同一插件同时新增和字段变化时只保留 `discovered`，当前记录包含合并后的最终字段。
- 同一插件多个实质字段变化只产生一个 `updated`，`changed_fields` 为字段并集。
- 事件排序固定为 `discovered` 在前，再按 `canonical_id` 字典序；保证消息、ID 和测试稳定。

---

## 基线与变化检测

### 按来源静默建基线

- 每个来源独立记录 `baseline_established`。
- 某来源第一次得到 `success=true, complete=true` 时，只保存该来源 observations 并将其标为已建基线，不因该来源历史数据生成事件。
- 已有其他来源基线不影响新启用来源的静默初始化。
- 新来源基线中的记录若与既有 canonical 插件合并，可补充字段；只有既有可信记录的实质字段确实改变时才可产生 `updated`，不得把新来源自身的历史条目当作 `discovered`。
- 失败或不完整结果不建立基线，也不覆盖旧状态。

### 新增判定

满足以下全部条件才生成 `discovered`：

- 本轮成功且完整的已建基线来源观察到 canonical 插件。
- 上一份合并快照中不存在该 canonical ID 或可解析 alias。
- 记录至少有名称和公开来源证据；仅有无法验证的文本引用不构成新增。

### 实质更新判定

仅以下规范化字段变化触发 `updated`：

- `version`
- `display_name`
- `description`
- `author`
- `repo_url`（同 canonical ID 下的规范 URL 修正；不产生迁移事件）
- `astrbot_version`
- `platforms`
- `market_status`
- Collection 或旧 Issue 的 `issue_state`、关键 `issue_labels`，但仅影响对应来源事实
- `archived`

以下变化本身不触发事件：

- `stars`、`forks` 或其缓存时间。
- `repo_updated_at`、`observed_at`、`last_seen_at`。
- 来源顺序、证据顺序、ETag、分页信息。
- 空白、换行、列表顺序、URL 大小写、`.git`、query、fragment 等规范化后等价变化。
- raw excerpt 或错误统计变化。

Star 最新值仍写入当前记录，并在新增/更新摘要生成时显示；若本轮 Star 获取失败，则显示上次成功值并标注缓存时间，不显示为 `0`。

移除/下架、仓库迁移、Star 独立变化均不得创建 MVP `ChangeEvent`，后续通过 1.0 后 Issue 决定。

---

## 状态文件与恢复

MVP 使用插件数据目录中的单个 `state.json`，`schema_version` 固定为 `1`。时间均为 UTC ISO 8601，映射键和数组顺序在写入时稳定化。

```json
{
  "schema_version": 1,
  "updated_at": "2026-07-20T12:00:00Z",
  "subscriptions": [],
  "last_run": {
    "run_id": "run:20260720T120000Z:4f2a1c",
    "started_at": "2026-07-20T12:00:00Z",
    "finished_at": "2026-07-20T12:00:04Z",
    "status": "partial",
    "events_created": 1
  },
  "sources": {
    "market": {
      "baseline_established": true,
      "last_success_at": "2026-07-20T12:00:01Z",
      "etag": "W/\"abc\"",
      "complete": true,
      "error_code": null,
      "observations": {
        "market:42": {
          "canonical_id": "github:owner/astrbot_plugin_demo",
          "source_url": "https://example.invalid/plugin/42",
          "content_hash": "sha256:5b0b...",
          "observed_at": "2026-07-20T12:00:01Z"
        }
      }
    }
  },
  "plugins": {
    "github:owner/astrbot_plugin_demo": {
      "name": "astrbot_plugin_demo",
      "display_name": "Demo",
      "description": "示例插件",
      "version": "1.2.0",
      "repo_url": "https://github.com/owner/astrbot_plugin_demo",
      "stars": 12,
      "star_fetched_at": "2026-07-20T11:00:00Z",
      "content_hash": "sha256:92ac...",
      "first_seen_at": "2026-07-19T12:00:00Z",
      "last_seen_at": "2026-07-20T12:00:01Z",
      "field_sources": {"version": "market:42"},
      "evidence": ["market:42", "collection_issue:88"]
    }
  },
  "id_aliases": {
    "source:collection_issue:88": "github:owner/astrbot_plugin_demo"
  },
  "github": {
    "rate_limit": {
      "remaining": 4812,
      "reset_at": "2026-07-20T13:00:00Z",
      "status": "ok",
      "error_code": null
    },
    "repos": {
      "github:owner/astrbot_plugin_demo": {
        "canonical_id": "github:owner/astrbot_plugin_demo",
        "stars": 12,
        "forks": 3,
        "archived": false,
        "repo_updated_at": "2026-07-20T10:00:00Z",
        "etag": "W/\"repo-etag\"",
        "fetched_at": "2026-07-20T11:00:00Z",
        "status": "fresh",
        "error_code": null
      }
    }
  },
  "outbox": {
    "batch:8e31...": {
      "target": "aiocqhttp:GroupMessage:123456789",
      "event_ids": ["event:discovered:4d91..."],
      "message": "AstrBot 插件市场变化……",
      "attempts": 1,
      "max_attempts": 5,
      "status": "pending",
      "next_attempt_at": "2026-07-20T12:02:00Z",
      "last_error_code": "delivery_timeout"
    }
  }
}
```

原子写契约：

- 在同目录写入临时文件，UTF-8 编码并完成 flush；可用时执行 `fsync`。
- 将现有文件保留为 `state.json.bak`，再用原子 replace 替换正式文件。
- 状态写入必须串行，并受同一轮检查锁保护。
- schema v1 的 `subscriptions` 是严格字符串数组：每项 trim 后长度 1 至 512，加载时去重并稳定排序；当前 schema 尚未发布，不兼容缺少该字段的宽松旧文件。
- 写后可重新打开并完成最小 schema 校验；失败则报告并保留可恢复文件。

损坏与版本行为：

- 正式文件 JSON 损坏或缺少必需字段时，尝试读取 `.bak`；备份有效则恢复到内存并进入降级状态，等待下一次安全保存。
- 正式与备份均损坏时，不静默重建、不推送，`status` 报告 `state_corrupt`，管理员修复或移走文件后才能重新建基线。
- `schema_version > 1` 时拒绝加载和写入，报告 `state_version_unsupported`，防止旧插件破坏新状态。
- `schema_version < 1` 不做猜测迁移；MVP 没有更旧正式 schema，应拒绝并提示。

---

## Outbox 与投递语义

MVP 提供 at-least-once 投递，无法保证 exactly-once：进程可能在平台已接收消息、但本地尚未来得及记录成功时崩溃，因此重启后可能重复发送。

- 事件 ID：`event:{kind}:{sha256(UTF-8(kind + NUL + canonical_id + NUL + previous_content_hash + NUL + current_content_hash))}`；首次发现时 `previous_content_hash` 固定为 `none`。
- 事件先按 `discovered`、`updated` 排序，同类再按 `canonical_id` 排序。
- batch ID：`batch:{sha256(UTF-8(ordered_event_ids + NUL + sorted_target_snapshots + NUL + batch_index/total))}`；AI 导语和 rendered message 不参与 ID，创建后目标快照固定，后续配置增删不改写历史 batch。
- 相同状态转换、目标、分批边界和渲染版本必须得到相同 ID。
- 创建事件后先把各目标的 batch 写入 outbox，再开始发送。
- 状态加载成功后先执行一次不改变语义的保存以确认可写；确认成功后立即处理到期的 pending batch，再执行新一轮采集；同 ID 已是 `delivered` 时不得重发。
- 每批最多尝试 `5` 次，初始立即发送，后续采用带抖动的指数退避；到达上限标为 `exhausted`，保留状态并在 `status` 显示计数。
- 推送目标只接受去除首尾空白后长度为 `1` 至 `512` 的字符串；非法目标跳过，并在运行报告中记录数量和稳定错误码 `invalid_push_targets`。
- 一次调用是否成功以 AstrBot 推送 API 正常返回为准；不得因无法获得平台消息 ID 而宣称 exactly-once。
- outbox 更新失败时停止继续投递，以免扩大重复窗口。

---

## GitHub REST API 契约

### 认证、并发与预算

- `github_token` 为空时使用未认证请求；非空时发送 `Authorization: Bearer ...` 和固定 `User-Agent`。
- GitHub 请求共享并发信号量，最大并发为 `2`。
- 每轮 GitHub API 请求预算：有 Token `20` 次，无 Token `5` 次。Issue、搜索、仓库元数据和条件请求均计入预算；市场非 GitHub 请求不计入。
- 预算按 gateway 接收的逻辑 API 请求计数；同一逻辑请求内部的 HTTP 瞬时重试不在 gateway 重复退避，所有睡眠和轻量抖动仅由 HTTP retry 层执行。
- 优先级：Collection > 已启用的主仓旧 Issues > 当前事件涉及仓库的元数据/Star > 普通 Star 刷新 > 全局发现后续页。
- 达到预算后停止低优先级请求，返回部分结果或陈旧缓存，不透支下一轮。

### ETag 与 TTL

- 对支持的 GET 保存 ETag，并发送 `If-None-Match`；`304` 视为成功且不下载正文。
- 仓库元数据和 Star TTL：有 Token 为 `6h`，无 Token为 `24h`。
- TTL 未过期直接使用缓存，不消耗请求预算。
- TTL 过期但刷新失败时可使用最后成功缓存并标记 `stale=true`；摘要注明“缓存值”，不得改成零。

### 状态码处理

- `401`：视为 Token 无效；本轮停止携带该 Token 的请求，不自动降级重试未认证请求，避免掩盖配置错误。缓存可读，状态报告认证错误。
- `403`：结合 `X-RateLimit-Remaining` 和正文分类。配额为零时等待 `X-RateLimit-Reset`；其他权限拒绝不重试。
- `404`：仓库元数据记为不可访问，不把 Star 设为零，不据此生成移除或迁移事件。
- `429`：尊重 `Retry-After`；本轮不继续同端点请求，可使用陈旧缓存。
- `5xx` 和连接错误：按通用重试规则执行。

Token 绝不写入日志、状态、消息、AI 输入、异常 repr、fixture 或 URL。发送请求前后均使用脱敏包装，错误只保留稳定类别、状态码和无凭据端点。

---

## AI 摘要契约

确定性事实模板永远可用，LLM 只生成最多一段导语，不能替换、删除或改写事实条目。

- 未启用 `enable_ai_summary` 时不调用 LLM。`llm_provider_id` 非空时显式使用；为空时，手动检查以当前 `event.unified_msg_origin`、自动检查以合并后首个有效目标调用 `get_current_chat_provider_id(origin)`。无 origin、解析为空或异常时直接发送事实模板。
- 输入仅包含本批事件类型、规范化公开字段、变化字段、来源类别和公开链接；最多 10 个事件、总字符不超过 6000。
- 不发送 Token、UMO、配置、raw response、日志、私人消息或未采用的冲突字段。
- 提示词要求使用中文、最多 120 个汉字，不得推断安全性、审核通过、官方推荐、代码质量、受欢迎程度或变化原因。
- 调用超时读取 `ai_timeout_seconds`，默认 60 秒、允许 10 至 120 秒，不额外重试；超时、异常、空输出或超长输出均降级到无导语事实模板。
- 输出去除控制字符和可疑角色提及；不得执行外部文本中的指令。
- AstrBot 适配层使用非流式 `context.llm_generate(chat_provider_id=..., prompt=..., system_prompt=...)`；`AstrBotAiClient` 通过 `asyncio.wait_for` 仅包裹该 Provider 调用，到期取消底层协程并映射为 `AiTimeout`，插件层不重试，外部 `CancelledError` 必须继续传播。
- 真实线上验收中 10 秒和 30 秒均返回 `ai_timeout`，60 秒真实 Provider 调用成功，因此默认值调整为 60 秒。
- 运行报告仅记录 `disabled | skipped | success | fallback` 和稳定错误码，不保存 prompt、模型原文或异常详情。

事实模板每条至少包含：事件类型、插件名、版本（如有）、实质变化字段、Star 当前值或缓存标识、来源类别和公开证据链接。

---

## 调度与并发

- 采用 fixed-delay：一轮结束后再等待 `poll_interval_minutes`，不使用 fixed-rate 追赶。
- `enabled=true` 后以内部短延迟 `10s` 启动首次检查，使 AstrBot 完成初始化；该值不是用户配置。
- 默认轮询间隔 `30` 分钟，读取现有 `poll_interval_minutes`。
- 调度、`marketwatch check`、群订阅变更和状态写入共享一个 `asyncio.Lock`。
- 定时任务发现锁已占用时记录 `run_skipped_busy` 并跳过，不排队。
- 手动 check 发现锁已占用时立即向调用者返回“检查正在进行，请稍后重试”。
- 插件终止时设置停止事件、取消并等待调度任务；正确传播和消费 `CancelledError`，不得遗留任务。
- 任一来源首次成功只建立其基线，不推送历史数据。

---

## 推送与命令契约

本节使用不含唤醒词的 canonical 命令名。AstrBot 的 `WakingCheckStage` 在命令 filter 前统一处理并剥离当前 `wake_prefix`；插件 handler 不重复解析消息或唤醒词。默认 `/`、自定义前缀和无前缀列表下的 @/回复唤醒均服从宿主规则。

### 主动推送

- 每轮将 WebUI `push_targets` 与状态中的群 `subscriptions` 合并；去除首尾空白、验证长度、去重并稳定排序。合并结果为空时绝不主动发送。
- 新订阅只影响之后创建的 batch；历史 outbox target snapshot 不随订阅增删改写。
- 每次轮询对本轮新事件即时批量推送；MVP 不实现固定每日时刻或 cron。
- 每个目标按 `max_items_per_push` 切分，保持稳定事件顺序；每批包含“第 x/y 批”和本轮总数。
- 一个目标失败不阻止其他目标尝试，但各自 batch 独立留在 outbox。
- 无事件、仅首次基线、仅 Star 刷新或仅运行错误时不发送普通变化消息。
- `aiocqhttp` 与 `qq_official` 共用同一 subscription、outbox 和 `context.send_message` 边界，不增加平台分支或 UMO 字符串重写。
- `qq_official` 当前优先验收无 `msg_id` 也可主动发送的群聊路径；C2C 留待后续验证，频道 cron 主动推送与 `qq_official_webhook` 不在当前契约内。

### `marketwatch status`

不得访问网络，只读取内存与本地状态，输出：

- 配置启用状态、调度任务状态、当前是否忙碌、轮询间隔。
- 状态 schema 版本、上次尝试与上次成功时间、上轮结果。
- 四来源开关、是否已建基线、上次成功、陈旧/错误类别。
- canonical 插件数、GitHub 缓存数、pending/exhausted batch 数。
- Token 仅显示“已配置/未配置”；目标分别显示配置目标数、群订阅数和本轮有效目标数，不显示完整 UMO 或 Provider ID。

状态文件不可读时命令仍应返回可理解的本地错误，不触发重建。

### `marketwatch check`

- 执行与定时任务相同的正常检查、状态提交和推送，不是 preview。
- 完成后向调用者返回本轮摘要：结果、来源成功/失败数、观察数、新增数、更新数、已创建/已投递/待重试批次数及脱敏错误。
- 即使 `push_targets` 为空，也向命令调用者返回检查摘要；这不是主动推送目标绕过。
- check 要求管理员权限。若 AstrBot 当前 API 无法稳定表达命令级管理员校验，实现必须停止在该能力上并记录阻断，不得自行通过群角色猜测、硬编码用户 ID 或取消权限要求。

### `marketwatch test-push`

- 命令要求 `PermissionType.ADMIN` 且仅允许群聊，不接受额外目标参数。
- 目标只读取当前 `event.unified_msg_origin` 并原样交给生产 outbox 共用的 `AstrBotNotifier`，最终调用 `context.send_message(target, MessageChain...)`；不得解析 UMO、依赖 `msg_id` 或增加 QQ 专属发送分支。
- 诊断正文明确标识 Market Watcher 主动推送测试；被动命令回复只报告成功或稳定脱敏错误类别，不输出完整 UMO、异常文本、Token 或堆栈。
- 命令不运行来源 fetch/merge/detect，不创建市场事件或 batch，不读取或写入订阅、插件、outbox 持久状态。

### `marketwatch test-ai`

- 命令要求 `PermissionType.ADMIN` 且仅允许群聊，以固定虚构插件 `astrbot_plugin_demo` 的公开事实调用与生产检查共享的 `AiIntroService` 和 `AstrBotAiClient`。
- `llm_provider_id` 非空时直接使用显式 Provider；为空时把当前 `event.unified_msg_origin` 交给 `resolve_provider_id`。选择顺序与生产 AI 导语一致，且诊断固定以 `enabled=True` 执行，不受 `enable_ai_summary=false` 阻止。
- 成功回复包含“真实 Provider 调用成功”和经 `sanitize_output` 校验的导语，不输出内部 prompt、完整 UMO、Token 或供应商响应元数据。
- Provider 缺失、超时、角色错误、调用异常、空输出或超长输出均复用 `AiIntroResult` 的稳定错误类别，并明确展示固定纯事实模板，不伪装为成功。
- 命令不执行来源 fetch/merge/detect，不创建真实市场事件或 batch，不读取或写入插件状态、订阅或 outbox。

### `marketwatch test-github`

- 命令要求 `PermissionType.ADMIN` 且仅允许群聊。每次创建独立短生命周期 `AioHttpClient` 与 `GitHubAuthHttpClient`，使用 `request_timeout_seconds` 请求固定 `https://api.github.com/rate_limit`，并在 `finally` 关闭 client。
- Token 只从 runtime config 读取，输出仅显示“匿名”或“已配置 Token”；不得输出 Token 内容、长度、前后缀、Authorization、Set-Cookie、响应正文、异常文本或 UMO。
- 诊断与生产 `GitHubGateway` 共用纯响应分类：2xx/3xx 为 `ok`；401 为 `auth_failed/github_auth_failed`；403 在 remaining=0、Retry-After 或 secondary signal 时为 `rate_limited/github_rate_limited`，否则为 `permission_denied/github_permission_denied`；429 为 rate limited，其他 HTTP 失败为稳定安全类别。
- 输出包含 HTTP 状态、分类、错误类别、`X-RateLimit-Limit`、Remaining 与 UTC ISO Reset；缺失或解析失败显示 `unknown`，不解析或显示 body message。
- 网络超时和异常复用安全错误分类。诊断不实例化 `GitHubGateway`，不消耗生产 5/20 预算，不更新 `state.github.rate_limit`、插件状态或 outbox，也不执行来源检查。
- `/rate_limit` 只验证认证和 primary rate limit 查询，不代表所有端点权限；禁止主动耗尽真实限额，403/429 通过离线响应模拟覆盖。

### Pending outbox 跨重启诊断

- `marketwatch test-outbox-prepare`、`test-outbox-status`、`test-outbox-deliver`、`test-outbox-cleanup` 均为无额外参数的 canonical 子命令，要求 `PermissionType.ADMIN` 且仅允许群聊。
- 四个 handler 只调用 `MarketWatcherService` 诊断方法。方法与正常检查复用同一 `asyncio.Lock`、store 和 notifier，handler 不直接读写状态或调用 notifier。
- prepare 先用现有 `validate_targets` 校验当前 `event.unified_msg_origin`，再以 `diagnostic:outbox:<sha256(target)>` 生成稳定且不泄露目标的 ID。同一目标幂等，只构造 `DeliveryBatch`，不构造 `PluginRecord` 或 `ChangeEvent`，不修改 subscriptions、plugins、last_run 或其他状态字段。
- prepare 的唯一 target 为 PENDING，`next_retry_at=9999-12-31T23:59:59Z`。该长期 hold 阻止 scheduler 或重启后的正常检查抢先投递，必须显式 deliver 或 cleanup；状态通过原子 `store.save` 持久化。
- status 只统计 `diagnostic:outbox:` batch 数和 pending/failed/sent/exhausted target 数，不输出 batch ID、hash、target、UMO、message、时间或错误原文。
- deliver 在同一锁内 load state，只清空诊断 PENDING/FAILED target 的 `next_retry_at`，先保存，再调用生产 `deliver_pending(state, store, notifier, now=..., clock=...)`，不得直接 `notifier.send`。因此其他已到期真实 pending 也可能同时处理。
- 成功 target 保持 SENT 作为验收证据；失败沿用生产 attempts、backoff 与 exhausted 规则。cleanup 只删除诊断前缀并保存，不影响任何真实 `batch:` 项，且重复执行安全。
- 异常只捕获 `StateError`、`OSError`、`ValueError` 并返回稳定类别，不泄露状态路径、UMO、Token 或异常文本。prepare 会使全局 `count_pending` 临时增加 1，cleanup 后恢复。
- 真实验收顺序：prepare → status `pending=1` → 完整重启 AstrBot（不是仅插件重载）→ status `pending=1` → deliver → 群收到诊断消息 → status `sent=1/pending=0` → cleanup → status `count=0`。

### 群订阅命令

- `marketwatch subscribe`、`marketwatch unsubscribe`、`marketwatch subscriptions` 均要求 `PermissionType.ADMIN` 且只允许群聊。
- 当前群标识只读取 AstrBot 官方 `event.unified_msg_origin`，不解析 adapter、群号或字符串结构。
- subscribe/unsubscribe 幂等并原子保存；subscriptions 只显示总数和当前群是否订阅，不列出完整 UMO。
- 状态损坏、版本不支持或保存失败时返回安全中文错误，不改写其他状态。

---

## 网络健壮性与部分提交

- 单次请求超时读取 `request_timeout_seconds`，覆盖连接与响应读取。
- 对连接错误、超时、`408`、`429` 和 `5xx` 最多重试 `2` 次，即总尝试不超过 `3` 次。
- 退避为 `1s`、`2s` 并加入小幅抖动；`Retry-After` 更长时优先采用，但不得让插件终止无法及时取消。
- JSON/文本响应体上限内部常量为 `5 MiB`；超过上限立即中止并标为 `response_too_large`。
- 不重试 `400`、`401`、普通 `403`、`404`、解析错误或 schema 不兼容。
- 成功且完整的来源可与其他失败来源一起提交；失败来源旧状态保持不变。
- 合并快照、来源状态和 outbox 必须作为一次状态事务原子保存。
- 状态保存失败时禁止任何新 batch 推送；旧 pending batch 只有在其状态可成功记录尝试结果时才可继续投递。

---

## 日志与敏感信息

轮次结构化观察回调使用固定字段：

- `run_id`、`phase`、`duration_ms`。
- `events`、`sources_succeeded`、`sources_failed`、`error_code`。

规则：

- 日志级别：轮次结果和不可恢复错误为 info/error；重试和来源降级为 warning；分页与缓存细节为 debug。
- Token、Authorization、完整 UMO、LLM 输入全文、响应正文和 raw excerpt 不得写日志。
- URL 日志必须移除 userinfo、query 和 fragment；异常字符串经过 Token、Bearer、疑似密钥与 UMO 清洗。
- 当前轮次阶段耗时固定记录 fetch、merge、detect、github、ai、save、deliver 和 overall；overall 截止于投递后的最终业务状态保存完成，并不小于各阶段耗时之和。随后只执行一次不计入 overall 的完成报告 checkpoint，以持久化 `finished_at` 和最终耗时。主程序结构化回调只输出 run ID、阶段、耗时、数量与稳定错误码。

---

## 配置字段语义

FSD 仅使用当前 `_conf_schema.json` 已有字段，不新增未实现配置：

| 字段 | MVP 语义 |
| --- | --- |
| `enabled` | 是否启动自动调度；不影响管理员手动 `check` 是否可用 |
| `poll_interval_minutes` | fixed-delay 间隔，默认 30 分钟 |
| `push_targets` | WebUI 主动推送 UMO；每轮与持久化群订阅合并 |
| `github_token` | 可选 GitHub Token，仅用于提高 REST API 限额 |
| `llm_provider_id` | 显式 AstrBot LLM Provider ID；为空时按当前/首个目标解析默认 Provider |
| `source_market_api` | 启用市场来源，默认开 |
| `source_collection_issues` | 启用 Collection Issues，默认开 |
| `source_plugin_publish_issues` | 启用主仓旧 Issues 兼容来源，默认关 |
| `source_github_discovery` | 启用 GitHub 全局发现，默认关 |
| `include_star_count` | 是否补充和显示 Star；关闭时不请求且摘要不显示 |
| `enable_ai_summary` | 是否尝试生成 LLM 导语，默认关 |
| `ai_timeout_seconds` | 生产 AI 导语与 `test-ai` 共用的 Provider 超时，默认 60 秒，范围 10 至 120 秒 |
| `request_timeout_seconds` | 单次 HTTP 请求超时，默认 15 秒 |
| `max_items_per_push` | 每个 DeliveryBatch 的最大事件数，默认 10 |

内部常量包括：首次运行延迟 10 秒、GitHub 并发 2、有/无 Token 每轮预算 20/5、Star TTL 6h/24h、搜索最多 2 页、响应上限 5 MiB、投递最多 5 次。LLM 超时已基于线上证据通过 `ai_timeout_seconds` 配置化，不再属于内部常量。

---

## 测试策略

### 纯函数单元测试

- GitHub URL 规范化：大小写、`.git`、query、fragment、尾斜杠、非法 host、额外路径。
- fallback ID 稳定性、alias 合并、弱证据不误合并。
- 文本和列表规范化、content hash 稳定性。
- 字段优先级、空值不覆盖、同轮事件折叠。
- 新增/实质更新正反例；Star、时间、排序变化产生零事件。

### 四来源 fixture

- 每类来源至少包含：正常单页、正常多页或等价完整性信号、空/缺字段、重复记录、格式变化和错误响应。
- 市场主端点与 fallback 各有 fixture。
- Issues 包含 PR 排除、开放/关闭、标签与表单正文变体。
- GitHub 搜索包含 fork、归档、大小写、`incomplete_results`。
- fixture 不包含真实 Token 或私人数据。

### 状态与 outbox

- 临时文件写入、replace 失败、备份恢复、双文件损坏、版本过高。
- 保存失败时零推送。
- 稳定 event/batch ID、跨重启 pending 重试、delivered 不重发、最多五次后 exhausted。
- 模拟“平台成功后进程崩溃”证明文档声明的 at-least-once 重复窗口。
- 覆盖诊断 prepare 原子保存、稳定 ID 与幂等、非法 origin/私聊/非管理员、长期 hold、跨新 service 实例恢复、生产 deliver_pending 成功与失败、cleanup 隔离、并发锁、脱敏输出和无参 CommandFilter handler。

### HTTP、GitHub 与降级

- 模拟 ETag/304、分页中断、超时、两次重试、响应过大。
- 有/无 Token 预算 20/5、并发不超过 2、TTL 6h/24h。
- 401、403 限流/权限、404、429/Retry-After、5xx 和陈旧缓存。
- 单源失败不覆盖旧状态；部分成功只提交完整来源。

### LLM 与推送

- LLM 禁用、超时、异常、空输出、越界输出均保留确定性事实模板。
- Prompt 不含 Token、UMO、raw response，输出不推断安全/审核/质量。
- 空目标零主动推送，多目标隔离，按 `max_items_per_push` 稳定分批。
- 推送失败进入 outbox，其他目标继续，重启后重试。

### 生命周期与 AstrBot 集成

- 禁用、启用、短延迟首次检查、fixed-delay、busy skip、手动 busy 返回。
- 加载、重载、卸载和 `CancelledError` 路径无遗留任务。
- AstrBot v4.26.6 停用时 registry 保留已绑定 handler，重新启用再次 partial 后可能形成 `original(old_instance, new_instance, event)`。插件在 `initialize()` 的第一段同步逻辑执行 handler binding 规范化，且 `terminate()` 不修改 registry。
- 兼容层只按 `type(instance).__module__` 精确查询自身 metadata；只有 root 与当前类原始函数同一、无 keywords、全部 bound args 均为当前 plugin type 实例时才收敛为当前实例一次。其他形状安全跳过，不修改 handler 参数签名或其他插件。
- `status` 零网络访问；`check` 走完整流程并返回调用者摘要。
- 验证管理员过滤 API 与 UMO 主动发送 API；这些测试可在 AstrBot 环境中运行，普通单测不要求安装 AstrBot。
- 集成测试通过安装包或 `PYTHONPATH` 使用真实 AstrBot，不硬编码相邻源码仓库路径。
- 若真实环境仍出现 handler 重绑定错误，先完整重载 AstrBot，必要时重新安装最新验收包。缺陷待上报；最低支持版本包含上游修复后删除兼容层。

---

## PRD 验收标准映射

| PRD 验收标准 | 可验证证据 |
| --- | --- |
| AstrBot 加载、禁用、启用、重载、卸载无遗留任务 | 生命周期集成测试；终止后任务集合断言；日志无未消费异常 |
| 无 Token、无推送目标可安全运行且不主动推送 | 未认证 HTTP 模拟；空目标 pusher 调用次数为 0 |
| 首次成功只建基线，第二次相同输入零事件 | 四来源参数化基线测试；状态快照比较；事件列表为空 |
| 四类 fixture 可标准化，同仓库只形成一个 canonical 插件 | 四适配器 fixture 测试；合并结果 canonical ID 唯一 |
| 市场或 Collection 新增、版本变化生成带证据事件 | detect 测试断言 `discovered/updated`、changed_fields 和 evidence |
| 单源失败、空响应、GitHub 限流、LLM 失败按规则降级 | HTTP/LLM 故障注入；旧来源状态不变；事实模板仍可用 |
| 日志、状态、测试输出和消息无 Token 明文 | canary Token 扫描断言；序列化状态、捕获日志和消息检查 |
| 静态测试无需安装 AstrBot，另有 AstrBot 集成测试 | 普通 CI 单元测试命令；独立标记的 AstrBot integration suite |

---

## 里程碑与完成定义

### M1：状态、模型与四来源适配

- 完成 schema v1 原子状态、领域模型、规范化与 canonical ID。
- 完成四类来源适配器、分页/完整性、fallback 和 fixture。
- 完成按来源静默基线与失败不覆盖旧状态。

### M2：合并、事件与可靠推送

- 完成来源优先级、跨来源合并、同轮折叠。
- 仅实现新增与实质更新检测。
- 完成事实模板、分批、outbox、稳定 ID 和跨重启重试。
- `marketwatch check` 执行正常检查和推送。

### M3：GitHub 增强与调度

- 完成 Token/未认证请求、ETag、TTL、预算、并发和状态码降级。
- 完成 Star 采集、缓存与摘要显示，但不生成 Star 事件。
- 完成 fixed-delay、短延迟首次运行、统一锁和生命周期处理。

### M4：AI、可观测性与发布验收

- 完成可选 LLM 导语与确定性降级。
- 完成结构化日志、敏感信息扫描和故障注入。
- 完成可选 AstrBot 集成契约；真实管理员权限、UMO 推送与生命周期已于 2026-07-21 完成发布前受控验收。
- PRD 验收标准全部有自动化或可重复的人工证据。

MVP 完成定义：M1-M4 全部完成，四来源及 fixture 均交付，所有 MVP 事件和可靠投递契约通过测试，阻断项已验证或以明确兼容实现解决，README 与 CHANGELOG 和实际行为一致。该定义已于 2026-07-21 达成并作为 `1.0.0` 基线通过验收；后续功能、缺陷和技术债通过 GitHub Issues 跟踪，设计变化同步到 `docs/DESIGN.md`，不继续扩张本历史规格。

---

## 1.0.0 发布前真实验收结论

以下原阻断项已在 2026-07-21 的受控线上验收中关闭，脱敏证据见 `docs/ONLINE_ACCEPTANCE.md`：

- **市场与 Collection 真实端点**：默认来源检查通过，fixture 继续承担字段变体与故障回归。
- **AstrBot 管理员命令校验**：管理员允许路径与非管理员拒绝路径均通过。
- **AstrBot 主动消息 API**：QQ 官方 WebSocket 群 UMO 推送、订阅持久化和 pending outbox 跨完整重启投递通过。
- **AstrBot LLM Provider API**：默认与显式 Provider 路由通过；10/30 秒超时安全降级，60 秒调用成功。
- **API canary**：匿名、有效 Token、401 降级恢复通过。真实 403/429 配额耗尽未主动制造，保留离线模拟和响应 headers 证据。

后续验证结果写入测试、GitHub Issue 或设计/验收文档；不要为规避验证而新增用户配置、猜测权限或改变已发布基线。
