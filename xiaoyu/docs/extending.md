# 小羽扩展指南（随包分发）

> 给准备为小羽新增能力的人和 agent。四条通道按"要加什么"选：
>
> | 要加的东西 | 通道 | 落盘位置 | 生效时机 |
> |---|---|---|---|
> | 知识 / 流程 / 提示文本 | 技能（SKILL.md） | `~/.agents/skills/<名>/` | 下次会话启动 |
> | 进程内 Python 工具 | 工具插件（entry point） | pip 包 | 安装后下次会话 |
> | 外部子进程工具 | MCP server | `.mcp.json` / 配置目录 `mcp.json` | 下次会话（会话内可 `/mcp` 重连） |
> | 生命周期钩子 | hooks | 配置目录 `hooks.toml` | 下次会话启动 |
>
> "配置目录"指：Linux/macOS `~/.config/xiaoyu/`（`$XDG_CONFIG_HOME` 可改），
> Windows `%APPDATA%\xiaoyu\`。
> 通读本文对应小节再动手；格式凭记忆猜是最常见的失败原因。

## 技能（SKILL.md）

与 Anthropic / agentskills.io 规范同形态。每个技能一个目录，内含 `SKILL.md`：

```markdown
---
name: deploy-checklist
description: 部署上线前的检查清单与回滚步骤。当用户提到部署、上线、发版时使用。
---

（markdown 正文：步骤、命令、注意事项……）
```

- 扫描目录（前者优先，同名去重）：`~/.agents/skills/`（跨客户端规范库，**推荐**）、
  配置目录 `skills/`。
- frontmatter 只认 `---` 块里平铺的 `key: value`（零依赖解析），`name` 和
  `description` 必填。
- 渐进披露：只有 name + description 进 system prompt，正文由模型按需用 `skill`
  工具加载——**description 决定这个技能会不会被选中**，写清楚"什么时候用"。
- 正文里引用同目录的脚本/参考文件用相对路径，按技能目录解析。

## 工具插件（entry point）

第三方 pip 包往小羽进程里注册工具函数。在你的包里：

```toml
# pyproject.toml
[project.entry-points."xiaoyu.tools"]
my_tool = "my_pkg.xiaoyu_plugin:make_tools"
```

```python
# my_pkg/xiaoyu_plugin.py
from xiaoyu.tools import Tool

def make_tools(config):          # 接收 Config，返回 Tool 或 list[Tool]
    return Tool(
        name="my_tool",
        description="一句话说清做什么、什么时候该用",
        parameters={             # OpenAI function-calling 的 JSON schema
            "type": "object",
            "properties": {"path": {"type": "string", "description": "…"}},
            "required": ["path"],
        },
        handler=run,             # handler(**args) -> str
        requires_approval=True,  # 默认 True（fail-closed）；真只读才写 False
        check_fn=None,           # 可选：廉价可用性探测，False 则不进 schema
    )

def run(path: str) -> str:
    ...
    return "结果文本"            # 失败以 "ERROR: " 前缀开头，会被计为工具失败
```

- `pip install` 进小羽所在环境后自动发现；单个插件坏了只警告不拦启动。
- `handler` 以关键字参数接收模型给的业务参数（需确认工具的"调用目的"参数
  由 harness 注入并在执行前摘除，handler 不会收到）。
- `XIAOYU_ENABLE_PLUGINS=0` 可整体关闭该通道。

## MCP server

声明式接入外部子进程工具（只支持 stdio transport）：

```json
// 工作区 .mcp.json（多客户端通用），或配置目录 mcp.json（用户级）
{
  "mcpServers": {
    "mydb": {
      "command": "npx",
      "args": ["-y", "@example/mcp-mydb"],
      "env": {"DB_URL": "${env:DB_URL}"},
      "timeout": 30
    }
  }
}
```

- `command`/`args`/`env` 的值里可写 `${env:VAR}`（兼容 `${VAR}`）引用环境变量。
- 远程 HTTP server 用桥接：`"command": "npx", "args": ["-y", "mcp-remote", "https://…"]`。
- 工具挂进来的名字是 `mcp__<server>__<tool>`；权限规则（`/allow`）按这个名字写。
- server 的 stderr 在配置目录 `logs/mcp-<name>.log`，排障看那里；`/mcp` 查看状态。

## hooks

在生命周期节点挂 shell 命令（**只认用户级**，工作区级刻意不读）：

```toml
# 配置目录 hooks.toml
[[hooks]]
event = "PreToolUse"      # PreToolUse | PostToolUse | UserPromptSubmit | Stop
matcher = "bash"          # 正则匹配工具名（仅 *ToolUse 有意义，可省）
command = "python ~/bin/check.py"
timeout = 10              # 秒，缺省 30
```

- stdin 收 JSON（event / tool / args / output / prompt 视事件而定）；
  **退出码 2 = block**（stderr 为理由），0 = 放行，其余 = fail-open 放行。
- `XIAOYU_ENABLE_HOOKS=0` 一键关闭。

## 插件包（一条命令装齐技能 + MCP）

要把多个技能与 MCP 声明打包分发，按 agent-plugins.org 规范布局
（`plugin.json` + `skills/<名>/SKILL.md` + `mcp.json`），用户
`xiaoyu plugin add <源>` 安装、`xiaoyu plugin update` 拉新。
插件技能带命名空间（`<包名>:<技能名>`），与散装技能不冲突。
