# AstrBot 插件开发与发布 Playbook

本文记录 `astrbot_plugin_market_watcher` 已验证的包结构、测试边界和发布检查。内容适用于当前 `1.1.1`；真实线上验收结论见 [ONLINE_ACCEPTANCE](ONLINE_ACCEPTANCE.md)。

---

## ZIP 与入口契约

- WebUI 验收 ZIP 只包含一个版本化顶层目录，目录内直接放置 `metadata.yaml`、`main.py`、`_conf_schema.json`、`requirements.txt` 与 `market_watcher/`。
- AstrBot 以类似 `data.plugins.astrbot_plugin_market_watcher.main` 的包名加载入口。此时 `main.py` 必须使用 `.market_watcher` 相对导入解析 sibling package。
- 本地离线测试仍会以顶层 `main` 加载入口，因此 `__package__` 为空时使用 `market_watcher` 绝对导入。
- 两条路径采用显式 `if __package__` 分支，不使用宽泛的 `try/except ImportError`，避免把内部模块的真实导入错误误判为加载模式差异。
- 函数内动态导入也必须遵守相同双路径契约。发布脚本会静态核对两组导入并在解包目录中执行包上下文加载。

---

## T2I Renderer 诊断日志

- `__init__` 为实例设置进程内安全 marker `_instance_marker`（`id(self)` 的十六进制）。
- `initialize()` 在构造 notifier 前执行只读诊断采样，记录到一条结构化日志（`renderer diagnostic`），包含 `instance_marker`、`notifier_marker`、`runtime_marker`、`image_card`、`plugin_callable`、`context_callable`、`api_callable`、`owner`、`plugin_type`、`context_type`、`notifier_callable`。
- `test_push()` 在图片/文本分支前记录 `test-push diagnostic`，包含 `instance_marker`、`notifier_marker`、`runtime_marker`、`image_card`、`plugin_callable`、`notifier_callable`、`condition`、`notifier_present`、`service_present`。
- Notifier 始终只接收 `self.html_render`（callable 时）或 `None`；不使用 `astrbot.api.html_renderer.render_custom_template` 作为发送 fallback。`api_callable` 仅用于观测宿主是否提供该 API。
- `AstrBotNotifier.prepare()` 始终使用 `return_url=False, options=options` 关键字调用 renderer，并对每次调用记录结构化诊断（`prepare diagnostic`），覆盖 `skipped` / `image_ready` / `invalid_image` / `string` / `none` / `exception` / `timeout` / `cancelled` 路径。日志不含原始值、URL、路径或凭据。`image_ready` 含 `source=bytes/file`、`image_kind` 和 `size`；`invalid_image` 含安全 `invalid_signature`（`internal_server_error`/`html`/`json`/`unknown`）和 `size`。现有文件路径不再出现 `outcome=string`，而是经 `asyncio.to_thread` 读取后进入校验路径。

---

## Metadata 与配置

- `metadata.yaml`、`main.py` 注册版本和 `pyproject.toml` 当前均为 `1.1.1`，版本不使用 `v` 前缀；Git tag 使用 `v1.1.1`。
- 当前声明平台为 `aiocqhttp` 与 QQ 官方 WebSocket `qq_official`。`qq_official_webhook` 尚未验收，不在支持声明中。
- `github_token` 默认值必须为空，并使用 `obvious_hint` 提醒这是敏感输入。Token 只用于提高 GitHub API 限额，不得进入日志、fixture、文档或发布包示例。
- `llm_provider_id` 使用 AstrBot `_special: select_provider` 选择器。留空时，手动检查解析当前会话默认 Provider，自动检查解析首个有效推送目标的默认 Provider。
- `ai_timeout_seconds` 由生产 AI 导语与 `test-ai` 共用，默认 60 秒、范围 10 至 120 秒。它只包裹 `context.llm_generate`，不改变来源 HTTP、scheduler、outbox 或消息投递超时。
- AI 导语与 GitHub Token 均为可选能力；缺失或失败不得阻塞事实模板、状态保存和 outbox 投递。

---

## QQ 官方 WebSocket 验收

- AstrBot WebUI 中配置的 `qq_official` adapter `id` 是 UMO 第一段，不是固定字符串。实例 ID 为 `qqws` 时，群聊 UMO 可形如 `qqws:GROUP_MESSAGE:<group_openid>`。
- 在以下步骤中，`<当前唤醒方式>` 表示 AstrBot 当前配置与会话规则产生的唤醒形式：默认 `/` 前缀、自定义前缀，或无前缀列表时先 @/回复机器人。
- 先执行 `<当前唤醒方式>marketwatch test-push` 直接验证当前群主动消息链路。本次实例使用 `!` 前缀时示例为 `!marketwatch test-push`，但 `!` 不是固定协议。
- `marketwatch test-push` 仅限管理员群聊，直接复用 `AstrBotNotifier` 向当前 UMO 发送诊断消息；不执行来源检查，也不修改订阅、插件状态或 outbox。
- AI 验收第一阶段保持 `llm_provider_id` 为空，执行 `<当前唤醒方式>marketwatch test-ai` 验证当前会话默认 Provider；本次实例示例为 `!marketwatch test-ai`，其中 `!` 不是固定协议。
- 真实线上验收中 10 秒和 30 秒均返回 `ai_timeout`，60 秒真实 Provider 调用成功，因此先使用默认 60 秒执行 test-ai；插件仍不自动重试。
- 第二阶段在 WebUI 选择显式 Provider，重载插件后再次执行 test-ai，确认显式 Provider 优先。`enable_ai_summary=false` 不阻止该管理员诊断命令。
- test-ai 使用固定虚构事实并复用生产 `AiIntroService`、`AstrBotAiClient`、超时和输出校验；不执行来源检查，不读写插件状态、订阅或 outbox。成功应显示“真实 Provider 调用成功”和安全导语，失败应显示脱敏错误类别及纯事实回退。
- 若能安全配置不可用 Provider，可补充真实降级验证；否则保留离线 Provider 缺失、超时、异常和不合规输出测试证据，不为制造失败而暴露凭据或干扰生产 Provider。
- GitHub 诊断分三阶段：先清空 `github_token`，重载并执行 `<当前唤醒方式>marketwatch test-github` 验证匿名请求；再仅在 WebUI 配置有效最小权限 Fine-grained Token，重载后验证 `ok`；最后可临时使用明显无效的占位 Token 验证 401 `github_auth_failed`，完成后立即清空无效值。
- 本次 `!` 前缀实例可执行 `!marketwatch test-github`，但 `!` 不是固定协议。真实 Token 只进入 WebUI 配置，不进入聊天、文档、fixture 或仓库。
- test-github 使用独立 client 请求 `/rate_limit`，不触碰生产 GitHubGateway 预算、rate-limit state 或 outbox。该端点成功不代表所有 GitHub 端点权限；不要真实耗尽配额，限流响应使用离线模拟。
- 推荐直接在 QQ 官方 WebSocket 测试群执行 `<当前唤醒方式>marketwatch subscribe`，让插件原样保存 `event.unified_msg_origin`；不建议手工猜测或规范化 `group_openid`。
- 执行 `<当前唤醒方式>marketwatch subscriptions` 确认当前群订阅状态，再在可产生测试变化的受控条件下执行 `<当前唤醒方式>marketwatch check`，验证 outbox 最终把同一 UMO 原样交给 `context.send_message`。
- 重启或重载 AstrBot 后按当前唤醒方式重复 subscriptions 与 check，确认 `StarTools.get_data_dir()` 下的订阅状态恢复且主动推送仍可用。
- 当前优先验收群聊。C2C UMO 可形如 `qqws:FRIEND_MESSAGE:<user_openid>`，但留待后续单独验证；频道主动发送依赖消息上下文，不承诺 cron 推送；Webhook 未验收。
- 业务层保持平台无关，不增加 QQ 专用发送分支、UMO 字符串重写或重复发送实现。
- 命令 handler 只注册不含唤醒词的 `marketwatch` canonical 名称；不要在插件内重复解析消息或剥离唤醒词，避免与 AstrBot `WakingCheckStage` 冲突。

### Pending outbox 跨重启实测

1. 在管理员群聊执行 `<当前唤醒方式>marketwatch test-outbox-prepare`。
2. 执行 `<当前唤醒方式>marketwatch test-outbox-status`，确认 `count=1`、`pending=1`。
3. 完整停止并重新启动 AstrBot，不要只重载插件；启动后再次执行 status，确认仍为 `pending=1`。
4. 执行 `<当前唤醒方式>marketwatch test-outbox-deliver`，确认当前群收到出站箱跨重启诊断消息。
5. 再次执行 status，确认 `sent=1`、`pending=0`。
6. 执行 `<当前唤醒方式>marketwatch test-outbox-cleanup`，最后执行 status，确认 `count=0`。

- prepare 的诊断 pending 使用长期 hold，不会被普通 scheduler 自动发送，必须显式 deliver 或 cleanup。
- deliver 复用生产 `deliver_pending`，所以同一时刻其他已到期的真实 pending 也可能被处理；成功诊断项保留为 SENT，直到 cleanup。
- 四个命令不显示 UMO、诊断 ID、消息正文、时间或异常原文。普通 status 的总 pending 在 prepare 后会临时增加 1，cleanup 后恢复。

---

## 状态目录与恢复

- 插件通过 `StarTools.get_data_dir("astrbot_plugin_market_watcher")` 获取宿主管理的数据目录，不在源码目录保存运行状态。
- `JsonStateStore` 将状态写入 `state.json`，先写同目录临时文件、执行 `fsync` 和严格反序列化校验，再使用原子替换更新主文件。
- 有效主文件会复制为 `state.json.bak`；主文件损坏时读取备份。主文件与备份均无效时明确报告 `StateCorruptError`，不得静默覆盖损坏证据。
- 磁盘 schema 版本高于当前实现时拒绝读写，避免旧代码破坏新格式数据。

---

## 来源 Fixture 与网络边界

- 四类来源分别是市场 API 或 `plugins.json`、插件 Collection Issues、主仓历史 `plugin-publish` Issues，以及低频 GitHub 全局发现。
- `tests/fixtures/` 保存正常、分页、不完整、字段损坏和 fallback 等离线样本；来源适配器测试通过可注入 `HttpClient` 读取 fixture，不访问真实 API。
- 默认单元测试不得依赖 GitHub Token、真实 AstrBot、真实 UMO、LLM Provider 或外部网络。真实 AstrBot 集成测试在宿主不可用时按现有契约跳过。
- 新增网络行为时应先扩展 HTTP 边界与 fixture，覆盖响应大小、分页、限流、认证主机约束和降级，再安排受控线上 canary。

---

## 发布包防线

- `scripts/package_plugin.py` 使用固定时间戳、固定文件权限、稳定排序和单一顶层目录生成确定性 ZIP，并同步写出 SHA-256 sidecar。支持正式包与 `--dev-version` 测试包，可指定 `--test-label` 和 `--flat`。`scripts/package_release.py` 是兼容包装入口。
- 打包自检拒绝绝对路径、路径穿越、符号链接、缓存、测试、脚本、环境文件和编译产物，并核对必需文件、版本、大小及包上下文导入。
- `scripts/verify_release.py` 检查三个正式版本源一致性、安全默认值、必需设计/验收文档及 README 入口、文件大小、疑似凭据、文档链接、临时 ZIP 解包导入和默认单元测试。
- 凭据扫描只是一道防线，不能替代提交前人工确认；任何真实 Token、UMO、服务器信息或私有路径都不得写入仓库。

---

## 包加载故障定位

- 已观察到的错误是 `ModuleNotFoundError: No module named 'market_watcher'`，发生在 AstrBot 成功解压 ZIP、读取 metadata 后加载包入口的阶段。
- 若 ZIP 已确认包含 `market_watcher/__init__.py` 和全部模块，而入口名是 `data.plugins.astrbot_plugin_market_watcher.main`，则证据指向 sibling 顶层绝对导入错误，而不是漏打包。
- 根因是插件根目录在该加载模式下不是顶层 `sys.path` 项；`from market_watcher...` 无法解析到入口旁的 package。
- 修复原则是在包上下文使用 `from .market_watcher...`，仅在顶层本地导入模式使用 `from market_watcher...`，并同时覆盖顶层和动态导入。
- 关闭问题前必须从实际 ZIP 解包到临时目录，以 AstrBot 风格父 package 名注册并执行入口，不能只检查 ZIP 成员列表或本地顶层导入。

---

## AstrBot v4.26.6 停用再启用故障

- 已确认 AstrBot v4.26.6 在 load 时会把 metadata handler partial 到新实例；停用保留 registry 后再次启用，会把已绑定旧实例的 handler 再次 partial，形成 nested 或由 CPython 合并的多实例 bound args。
- 不得通过给 handler 增加 `*args` 绕过，因为这会破坏 CommandFilter 参数契约，且命令仍可能使用旧终止实例。
- `normalize_plugin_handler_bindings()` 只在 `initialize()` 最开始运行，按当前类精确模块名查询自身 handlers。它验证原始函数 identity、全部 partial args、keywords 和实例类型后，才收敛为当前实例一次。
- `terminate()` 不修改 registry；root 不匹配、非插件参数、keywords、非函数属性或 AstrBot API 不可用时均不做危险修复。
- 真实验收至少执行两轮“停用 → 启用 → subscriptions → test-push”，确认命令使用新实例且不出现 positional argument `TypeError`。
- 若仍失败，先完整重载 AstrBot；必要时重新安装最新验收包。该缺陷待上报，不虚构上游 issue 编号；最低支持 AstrBot 版本包含上游修复后删除兼容模块和对应测试。

---

## 发布前检查与真实验收边界

离线检查使用以下入口：

```bash
python -m ruff check .
python -m ruff format --check .
python -m compileall -q main.py market_watcher tests scripts
python scripts/verify_release.py
python scripts/package_plugin.py
```

- `verify_release.py` 已运行默认单元测试，无需在同一 CI job 中再次执行相同 unittest 命令。
- 生成后应核对 ZIP 绝对路径、字节大小、SHA-256 sidecar，并独立确认 `market_watcher/` 成员及模拟包导入通过。
- `1.0.0` 已于 2026-07-21 在 AstrBot v4.26.6 与 QQ 官方 WebSocket 完成发布前线上验收；离线检查仍不能替代未来宿主版本和平台回归。
- 发布后功能、缺陷和技术债统一通过 GitHub Issues 跟踪；涉及架构、不变量、平台边界或安全模型的变化必须同步更新 [DESIGN](DESIGN.md)，验收范围变化同步更新 [ONLINE_ACCEPTANCE](ONLINE_ACCEPTANCE.md)。
- `aiocqhttp` 保持支持声明和离线契约覆盖；本次真实平台验收聚焦 `qq_official` 群聊。C2C、QQ 官方频道 cron 与 Webhook 不因 `1.0.0` 发布而视为已验收。
