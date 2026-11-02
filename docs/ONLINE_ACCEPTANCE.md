# Market Watcher 1.0.0 线上验收记录

本文是 1.0.0 发布前真实线上验收的脱敏记录，不包含 Token、UMO、账号、服务器路径或 Provider 私有信息。

---

## 日期与环境范围

- 验收日期：2026-07-21。
- 宿主：AstrBot v4.26.6。
- 真实消息平台：QQ 官方 WebSocket `qq_official` 群聊。
- 网络能力：AstrBot 插件市场/公开来源、GitHub API、AstrBot LLM Provider。
- 支持声明中的 `aiocqhttp` 保持离线契约覆盖，本次未重复进行真实平台验收。
- QQ 官方频道、C2C 主动消息和 `qq_official_webhook` 未验收。

---

## 判定规则

- 命令必须按 AstrBot 当前唤醒规则到达 canonical `marketwatch` handler，插件不依赖固定前缀。
- 成功不仅看命令回复，还核对生命周期、持久状态、主动消息、降级结果和重启后的恢复。
- 安全降级必须返回稳定错误类别或纯事实模板，不得泄露异常原文、凭据或完整 UMO。
- 需要跨重启的项目必须执行完整 AstrBot 停止与启动，不能用插件重载替代。
- 不为制造失败主动耗尽真实 GitHub 配额；403/429 边界以离线模拟和真实响应 headers 契约判定。

---

## 逐项结论

### 生命周期与安装

- 安装与首次加载：通过。
- 连续两轮停用后启用：通过，未出现旧实例 handler 参数错误。
- 插件重载：通过。
- AstrBot 完整重启：通过。
- 卸载后重新安装：通过。

### 命令、权限与唤醒

- 自定义唤醒词：通过，canonical 命令可按宿主配置触发。
- 管理员命令允许路径：通过。
- 非管理员权限拒绝：通过。
- `status` 只读路径：通过。

### 订阅、状态与主动推送

- QQ 官方 WebSocket 群订阅：通过。
- 订阅持久化经过重载与完整重启：通过。
- 当前群主动推送诊断：通过。
- 默认来源检查：通过。
- 受控变化产生后的群主动推送：通过。

### AI Provider

- `llm_provider_id` 为空时解析当前会话默认 Provider：通过。
- 显式 Provider 优先路由：通过。
- 10 秒超时：返回 `ai_timeout` 并回退纯事实模板，通过。
- 30 秒超时：返回 `ai_timeout` 并回退纯事实模板，通过。
- 60 秒超时配置：真实 Provider 成功返回安全导语，通过；因此 1.0 默认值为 60 秒。
- AI 失败不阻塞事实消息和 outbox：通过。

### GitHub 认证与降级

- 匿名 `/rate_limit` 诊断：通过。
- 有效最小权限 Token 诊断：通过。
- 明显无效占位 Token 返回 401 并分类为认证失败：通过。
- 清除无效配置并恢复匿名或有效 Token：通过。
- 诊断输出未包含 Token、Authorization 或响应正文：通过。

### Pending outbox 跨完整重启

- prepare 后显示一个 pending 诊断目标：通过。
- 完整重启 AstrBot 后 pending 仍存在：通过。
- 显式 deliver 后群收到诊断消息：通过。
- 状态转为 sent 且 pending 清零：通过。
- cleanup 后诊断 batch 清零：通过。

---

## 未以真实耗尽方式测试的边界

- 未主动制造 GitHub primary limit 真实耗尽、403 rate-limit 或 429，以避免影响共享配额和线上业务。
- 这些分支已通过离线响应模拟覆盖，并以 `X-RateLimit-*`、`Retry-After` 等 headers 契约验证分类、阻断和恢复逻辑。
- `/rate_limit` 成功不等同于所有仓库或 Search 端点均有权限；运行时按最小权限和安全降级处理端点差异。
- secondary rate-limit 未在真实环境主动触发，使用离线安全截断响应信号与 headers 测试。

---

## 未验收与非承诺范围

- QQ 官方频道命令与 cron 主动推送未验收，不作 1.0 承诺。
- `qq_official_webhook` 未验收且未写入 metadata 支持平台。
- QQ 官方 C2C 主动消息未验收。
- `aiocqhttp` 本次没有真实线上回归；支持声明基于既有 adapter 边界和离线契约，后续真实回归应单独记录。

## #4 本地回归与 smoke（2026-11-02，非线上验收）

- `pytest`：332 passed、3 skipped、129 subtests passed；`ruff check .` 通过。3 项跳过来自本机未安装 AstrBot 的宿主集成，不代表权限装饰器、适配器或真实平台投递已验收。
- 真实公开请求仅访问 `https://cloud.astrbot.app/api/v1/market/plugins.json`，返回 HTTP 200，取得 3,074 条有效插件记录；`$meta` 未进入插件列表。数量是当时公开目录的观测值，不是固定回归断言。
- 回环 HTTP smoke 使用实际 `AioHttpClient`、来源适配器、service 和 `JsonStateStore`，请求白名单仅包括 Cloud 市场、GitHub 仓库元数据及已启用的独立 Search。503、无效 JSON、超时及相同结果恢复均保留市场基线，无伪新增或更新；已知插件暂时缺项、空市场描述与仓库描述冲突后恢复同样为零事件。
- 包含旧来源、误解析 `$meta`、订阅及 pending/failed/exhausted 队列的 v1/v2 fixture 写入临时磁盘后实际走迁移入口：旧批次隔离、不自动发送；首次 Cloud 成功静默重建；重新构造 service 后不重复迁移。订阅保留，真实版本变化产生一条正常更新。
- 50 条真实检测事件按 13+13+12+12 分页，第一窗口发送 1/4、2/4；推进时钟到后续窗口、重新构造 service 后续传 3/4、4/4，超额页 attempt 保持 0。实际相邻发送间隔分别为 2.083 秒和 2.078 秒。失败页先保存 attempt=1，再由 drain 重试成功，已完成页不重发。
- 迁移命令体在无宿主 harness 中连接真实持久化 service 执行：状态只显示安全计数，release/discard 均完成指定旧批次处置并保留 sent 状态；该 smoke 不验证 AstrBot 命令路由及权限装饰器。
- 消息仅进入本地记录器，没有调用真实消息平台。未访问或修改线上插件数据，未部署、启用、推送或发布；线上仍按停用边界处理。部署前需备份状态并审查旧队列，真实宿主及平台验收需要独立授权。
