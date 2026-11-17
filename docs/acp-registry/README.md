# ACP registry 提交物

本目录是 [agentclientprotocol/registry](https://github.com/agentclientprotocol/registry)
收录小羽所需两个文件的**权威副本**：提交/更新 registry PR 时，把 `agent.json`
与 `icon.svg` 原样拷进 registry 仓的 `xiaoyu/` 目录（目录名必须等于 `id`）。

要点（改动前先读）：

- **版本无需手动维护**：registry 每小时扫 PyPI 自动 bump `version` 与
  `package` 里的 `==` 钉版本；只有改描述、图标、新增分发方式才需要手动 PR。
- **收录硬门槛=认证**：initialize 必须返回非空 `authMethods`（只认
  agent/terminal 两型）。小羽声明 terminal 型指向 `xiaoyu config` 向导，
  无 provider 配置时 session/new 回 -32000 auth_required（见 acp.py）。
- **uvx 分发**：uvx 默认跑「与包同名的 console script」，所以 pyproject 里
  有 `xiaoyu-agent` 别名入口（v0.30.11 起）；改包名或删别名会把 registry
  安装路径弄断。
- **icon 规格**：CI 严格校验 16×16、单色（`currentColor`）。
