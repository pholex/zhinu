# 模块分层不变量

内核必须能在没装 TUI 可选依赖（`pip install xiaoyu-agent` 不带 extra）的机器上工作，
所以「核心模块不碰 rich / prompt_toolkit」是架构承诺，不是风格偏好。
静态工具管不了"顶层 import 还是函数内 import、注释里提到还是真的依赖"，由本审计把关。

## Case 1: 核心模块不得依赖 TUI 库

**Scope**: `xiaoyu/agent.py`、`xiaoyu/render.py`、`xiaoyu/events.py`、
`xiaoyu/cli.py`、`xiaoyu/scripted.py`、`xiaoyu/wire.py`、`xiaoyu/embedding.py`

**Requirements**:
- 以上文件中不得出现任何对 `rich` 或 `prompt_toolkit` 的 import（顶层或函数内都不行）。
  TUI 相关实现只允许住在 `xiaoyu/tui.py`（以及 `xiaoyu/theme.py` 等 tui 侧模块）。
- `xiaoyu/cli.py` 引用 TUI 时必须经由延迟导入 `tui` 模块并有缺依赖回退
  （找不到 tui 时退回明文 REPL 的路径要存在）。

<examples>
反例：`xiaoyu/agent.py` 里 `from rich.console import Console`（无论在哪一层）。
正例：`xiaoyu/cli.py` 在函数体内 `from . import tui` 并 try/except ImportError 回退。
</examples>

## Case 2: 协议层零第三方依赖

**Scope**: `xiaoyu/events.py`、`xiaoyu/wire.py`、`xiaoyu/scripted.py`

**Requirements**:
- 这三个文件只允许 import 标准库与 `xiaoyu` 包内模块，不得 import 任何第三方包
  （包括 `openai`）。事件协议与 wire/测试桩是"哪天要跨进程/换内核"的边界资产，
  不能被 SDK 绑死。
