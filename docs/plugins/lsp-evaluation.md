# LSP 接入验证记录

2026-11-03：小羽支持为 explore 显式选择主会话的 MCP 查询工具，参见
[配置说明](../configuration.md#给-explore-接入代码检索扩展)。默认关闭此扩展选择，
不自动安装语言服务器，不增加核心依赖。

本页是候选验证记录，**不属于官方推荐清单**。

## Python 候选

- 桥接：[mcp-language-server](https://github.com/isaacphi/mcp-language-server)，v0.1.1。
- 语言服务器：Pyright 1.1.414。
- 环境：macOS，本地临时项目；经小羽真实 MCP 客户端启动并调用，结束后关闭。
- 仅暴露 definition、references、hover、diagnostics 四个查询工具；未暴露编辑和重命名。

可复现的测试代码：

```python
# lib.py
def greet(name: str) -> str:
    return name
```

调用文件：

```python
# app.py
from lib import greet as hello
result = hello("world")
```

| 查询 | 结果 |
|---|---|
| definition，符号 greet | 返回 lib.py 的函数实现与位置 |
| hover，app.py 第 2 行第 10 列 | 返回 hello 的字符串参数和返回类型 |
| diagnostics，初始 app.py | 未报告诊断 |
| 外部写入把参数改成 123，再查询 diagnostics | 返回第 2 行参数类型不匹配错误 |
| references，符号 greet | 返回导入位置，**遗漏第 2 行别名调用** |

这次证明了实际连接、查询与修改后的诊断刷新，没有证明引用完整性、复杂项目兼容性，
也没有测量真实模型的任务成功率或 token 节约。上述引用遗漏足以暂缓推荐，
不能把查询返回的位置当成全部调用点；仍需文件检索和测试交叉检查。
TypeScript、多项目工作区、语言服务器崩溃恢复尚未做真实验证。

另用确定性的模型调用 fixture 驱动真实 explore，复用该 MCP 连接调用 definition，
已确认函数定义证据进入子会话的下一次模型请求；未调用线上模型。

接入层另有无网络回归：真实子 agent 工具调用链路、精确工具名筛选、隐藏工具不可
绕过调用、父级 deny 和审批继承、plan 模式、默认文件检索行为及连接复用。
