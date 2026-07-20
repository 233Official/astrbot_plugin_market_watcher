# AstrBot 插件市场观察器

`astrbot_plugin_market_watcher` 计划聚合 AstrBot 插件市场、发布 Issue 与 GitHub 仓库信号，识别新插件和重要变化，并向指定 AstrBot 会话推送可读摘要。

当前版本为 **0.1.0 初始化版本**。它只提供可持续开发所需的插件生命周期、命令、配置、领域接口和文档，不会访问网络、采集市场数据或主动推送消息。

---

## 目标功能

- 发现新增、更新、移除或迁移的 AstrBot 插件。
- 合并多个来源中的同一插件，避免重复提醒。
- 获取并缓存 GitHub Star 数等补充指标。
- 可选调用 AstrBot LLM Provider 生成中文变化摘要。
- 按目标会话、批次和重要程度推送通知。

---

## 规划数据源

- AstrBot 市场 API 或 `plugins.json`：主要事实来源。
- AstrBot 插件 Collection Issues：主要发布入口和候选信号。
- AstrBot 主仓 `plugin-publish` Issues：仅兼容历史与残留信号；官方发布指南已称该入口废弃。
- GitHub 全局 `astrbot_plugin_*` 仓库发现：低频补充发现来源，默认关闭。

详细规则见 [产品需求文档](docs/PRD.md)。

---

## 安装方式

在 AstrBot WebUI 的插件管理中使用仓库地址安装：

```text
https://github.com/233Official/astrbot_plugin_market_watcher
```

也可以将仓库克隆到 AstrBot 的插件目录后重载插件。当前版本要求 AstrBot `>=4.24.0,<5`，Python `>=3.10`。

---

## 命令

- `/marketwatch status`：显示启用状态、占位轮询任务状态和开发阶段。
- `/marketwatch check`：明确返回“初始化阶段、尚未实现完整采集”，且不会发起网络请求。

---

## 配置

核心配置位于 `_conf_schema.json`：

- `enabled`：是否启动占位轮询，默认关闭。
- `poll_interval_minutes`：未来轮询基础间隔。
- `push_targets`：未来主动推送目标 UMO 列表。
- `github_token`：可选 GitHub Token，默认空。
- `llm_provider_id`：未来 AI 摘要使用的 AstrBot Provider。
- `source_*`：各数据源开关；高成本或兼容来源默认关闭。

当前配置仅为后续实现预留，除生命周期状态外不会触发真实业务。

---

## 安全说明

- 仓库不包含任何凭据，`github_token` 默认值为空。
- GitHub Token 应使用最小权限 Fine-grained Token，不得写入日志、Issue、截图或版本库。
- 当前 AstrBot 插件配置 schema 未提供通用密码输入控件，WebUI 可能以普通字符串框展示 Token；请限制 AstrBot 配置文件权限，并只在确有配额需要时配置。
- 推送目标默认空，插件不得在用户未配置目标时主动发送消息。
- 后续网络实现必须设置超时、限流、缓存、响应大小限制和敏感信息清洗。

---

## 开发状态

已完成：

- Star 插件注册、`initialize` / `terminate` 生命周期。
- 可取消并可等待结束的占位 `asyncio` 任务。
- `status` 与 `check` 命令。
- 配置 schema、领域模型与五类组件协议。
- 无需安装 AstrBot 的静态结构测试。

尚未完成：

- 数据源客户端、持久化、去重和变化检测实现。
- Star 缓存、AI 摘要、消息渲染与推送。
- 真实网络、AstrBot 集成和端到端测试。

---

## 路线图

1. 实现本地原子状态存储、标准化模型和确定性去重测试。
2. 接入市场与 Collection 主要来源，建立首次运行基线。
3. 增加变化检测、手动检查预览和受控推送。
4. 接入兼容 Issue、GitHub 全局发现、Star 缓存与限流保护。
5. 增加可选 AI 摘要、可观测性和完整 AstrBot 集成测试。

---

## 开发验证

初始化版本可使用标准库完成最低验证：

```bash
python -m compileall main.py market_watcher tests
python -m unittest discover -s tests -v
```

---

## 许可证

本项目使用 [GNU Affero General Public License v3.0](LICENSE)。
