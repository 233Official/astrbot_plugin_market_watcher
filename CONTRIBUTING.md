# 贡献指南

感谢参与 AstrBot 插件市场观察器的维护。缺陷、功能建议和兼容性反馈请先通过 [GitHub Issues](https://github.com/233Official/astrbot_plugin_market_watcher/issues) 建立可追踪上下文。

---

## 提交 Issue

- 说明 AstrBot、Python、插件版本和 adapter 类型。
- 提供最小复现步骤、执行的 canonical 命令名和脱敏错误类别。
- 清理 Token、完整 UMO、用户或群标识、内部域名、配置文件和日志正文。
- 平台支持、架构、安全或隐私边界的建议请指出期望影响范围，不要从单平台结果外推。

---

## 开发验证

在插件目录安装开发依赖后，优先运行与变更最接近的测试，再执行离线验证：

```bash
python -m unittest discover -s tests -v
python -m compileall -q main.py market_watcher tests scripts
python -m ruff check .
python -m ruff format --check .
python scripts/verify_release.py
```

未安装 AstrBot 时，真实集成契约可能跳过；离线测试不能替代目标宿主版本和真实 adapter 回归。

---

## 发布包检查

仅在确需验证发布包时运行：

```bash
python scripts/package_plugin.py                              # 正式包
python scripts/package_plugin.py --dev-version --test-label x  # 测试包
python scripts/package_plugin.py --flat                        # 旧版扁平包
python scripts/package_release.py                              # 兼容入口（同上）
```

检查生成 ZIP 的顶层目录、必需文件、导入契约、大小和 SHA-256。不要手工修改 `dist/` 产物，也不要把测试、缓存、凭据或本地环境文件加入发布包。

---

## 文档与设计入口

- 架构、不变量、安全与平台边界：[设计文档](./docs/DESIGN.md)。
- 真实平台回归范围与证据：[线上验收记录](./docs/ONLINE_ACCEPTANCE.md)。
- 包结构、离线验收和故障定位：[插件开发与发布 Playbook](./docs/PLUGIN_DEVELOPMENT_PLAYBOOK.md)。
- 用户命令与配置：[命令参考](./docs/COMMANDS.md)、[配置参考](./docs/CONFIGURATION.md)。

涉及用户可见行为时同步更新 README 或专题文档；涉及版本新增、修复、弃用或升级影响时更新 `CHANGELOG.md`。不要把完整内部 runbook 复制到 README。
