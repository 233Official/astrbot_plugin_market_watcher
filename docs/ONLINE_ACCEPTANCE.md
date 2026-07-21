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
- `/rate_limit` 成功不等同于所有仓库、Search 或 Issues 端点均有权限；1.0 仍按最小权限和安全降级处理端点差异。
- secondary rate-limit 未在真实环境主动触发，使用离线安全截断响应信号与 headers 测试。

---

## 未验收与非承诺范围

- QQ 官方频道命令与 cron 主动推送未验收，不作 1.0 承诺。
- `qq_official_webhook` 未验收且未写入 metadata 支持平台。
- QQ 官方 C2C 主动消息未验收。
- `aiocqhttp` 本次没有真实线上回归；支持声明基于既有 adapter 边界和离线契约，后续真实回归应单独记录。
