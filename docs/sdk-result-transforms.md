# 工具结果文本变换

自 0.62.0 起提供。宿主通过 `SessionOptions.result_transforms` 显式注册
同步或异步 Python 回调，在工具返回文本进入截断、recall 落盘与模型上下文之前
完成业务脱敏或格式转换。默认空元组，不启用变换，不增加依赖。

```python
from dataclasses import replace
from xiaoyu_agent_sdk import ResultTransform, Session, ToolOutput

def redact(output: ToolOutput) -> str:
    return output.text.replace("token=example-private", "token=[redacted]")

options = replace(options, result_transforms=(
    ResultTransform("redact-order-token", redact, tool_name="order"),
))
with Session(options) as session:
    result = session.run("查询订单")
```

这个例子只替换一个已知业务格式，不是通用密钥扫描器。完整可运行示例见
[result_transforms.py](../examples/sdk/result_transforms.py)。移除注册项即可关闭它。

## 接口与组合

- `ResultTransform(name, callback, tool_name="")`：name 是非空、会话内唯一的审计
  名称。tool_name 为空匹配所有已执行工具的返回文本，否则精确匹配工具名。
  MCP 经 use_tool 转发时，既可匹配 use_tool，也可匹配目标工具全名。
- callback 接收不可变 `ToolOutput`，字段为 tool_name、text、is_error、session_id、
  run_id、task_id、tool_call_id。tool_name 是实际执行入口；经 use_tool 时仍为
  use_tool。不提供可变 Agent、执行参数或重试方法。
- `ResultTransformHandler` 为 `Callable[[ToolOutput], str | Awaitable[str]]`。
  返回 str 作为下一项的输入；原样返回 output.text 即不修改。空字符串是合法替换，
  None、ToolResult、字典及其他返回值均不合法。注册顺序即执行顺序，不做自动排序。
- 同步 Session 使用同步回调；async def 回调须用 AsyncSession，运行在宿主事件循环。
  同步回调使用 SDK 已有回调线程池。不同 DAG 子任务可能同时调用同一函数，宿主须
  保护自己共享的可变状态。
- `result_transform_timeout=30.0` 是每项的超时秒数，须为有限正数。注册列表由
  Session 复制，不支持执行中替换；用新 Session 或显式 fork options 改配置。

变换不改变执行成败：失败文本沿用内核 ERROR: 前缀，回调去掉时会补回；成功结果
若改成 ERROR: 则按无效变换处理。需要拒绝发布结果时抛异常，不能把已执行的动作
改写成“未执行”或伪造成功。字段 is_error 只表达返回文本的内核分类，不替代业务验证。

## 数据处理顺序

| 阶段 | 接收到的内容 |
|---|---|
| deny／模式／审批／PreToolUse | 原有工具参数；在执行前决定是否允许动作，不受结果变换影响 |
| 工具 handler | 执行一次；SDK Python 工具先把 ToolResult／JSON 值转换为文本；常规执行异常归一为失败文本 |
| 注册的结果变换 | 完整返回文本，尚未经过内核截断；每项接收前一项的返回值 |
| 隐形字符清理、长度限制与 recall 落盘 | 最后一项的输出；自动生成的完整 recall 文件不会保存变换前文本 |
| 内核附注、PostToolUse、ToolFailed | 已处理且可能截断的结果；PostToolUse 仍只能附加反馈，不能替换原文 |
| trace、ToolCompleted | 处理后的结果与随后附注；不会另留原始工具返回文本 |
| 外部内容来源标注、通知、会话消息 | 处理后的文本仍遵循 MCP 等来源的 untrusted_content 标注；变换不提升其可信度 |
| 持久化／下次模型请求 | 以上最终消息；恢复、fork 不重复变换已有历史 |

只处理真正执行过的 handler 返回文本；权限拒绝、未知工具、执行前参数错误等
内核诊断不进入回调。recall 是新的工具调用，其返回文本可以再次匹配全局变换；
只想改业务工具的格式时应精确指定 tool_name。

SDK 模型委托和 `session.tasks` 的子 Agent 都继承这条管线，在子模型读取结果、
子历史归档之前处理。委托返回父级的汇总是另一次工具结果，也可匹配变换。
子 Agent 最终回答本身不属于工具返回文本；它不是额外的一条模型输出过滤管线。

注册后，持久化日志中的 `sdk.result_transform` 只记录名称、序号、工具、关联 ID
及 applied／failed 状态，不写原文、中间文本或异常正文。成功项的审计顺序与配置
一致。中断可能没有最终状态，存储故障可能无法记入；这不是跨崩溃的执行证明。

## 失败、中断与关闭

异常、返回类型错误或超时会停止后续变换。主轮次抛 `ResultTransformError`
（ExecutionError 子类），不继续请求模型、不自动重跑工具。子任务沿用现有失败
归档／任务终态；模型发起的子委托失败仍由委托报告交回父级，父级可以继续判断。
这些流程都不等于撤销已经发生的文件修改、交易或远端请求。

失败结果只留下固定的“工具已执行、结果被扣留、先核实再决定是否重试”说明；
终态事件和历史不包含回调异常正文。一个批次尚未执行的工具调用补齐未执行结果。
注册了变换时，执行中断异常附带的未经处理部分结果也会扣留。变换中断或失败时
一并丢弃工具箱中尚未交出的媒体，避免后续轮次误用这一批残留数据。

同步回调无法被强制终止：超时后仍由会话持有，未退出前不能启动新轮次；close
超时会抛 CloseTimeoutError，宿主应等回调退出后重试关闭。异步取消复用现有
回调取消与清理等待契约，迟到的回调结果不会重新进入历史。

## 覆盖边界

这是**返回文本**的处理接口，不能当作整个会话的数据出口防护。以下通道不经过它：

- 工具参数、审批预览、图片／媒体、MCP 进度／日志、后台任务通知。
- 工具内部自己写出的文件、数据库、远端记录、标准输出及其他工具自管日志。
- 原有历史、模型正文、结构化最终对象，以及计划更新事件等工具内部另发的事件。
- 随后由审批附言、PostToolUse 反馈、项目说明或宿主通知加入的文本。

因此不要把敏感值放进工具参数，再期望仅靠结果变换从转录中移除它。配置新的
脱敏规则不会回溯清洗旧日志。宿主 Python 扩展是可信代码，可能主动保存原文；
本接口约束不构成对恶意扩展的隔离。

观察类扩展继续消费事件，无需注册变换。[cache_report.py](../examples/sdk/cache_report.py)
只聚合 RequestEnded 已报告的 prompt／cached token，缺失 usage 的请求单独计数，
不请求模型、不修改工具结果、不推算账单节省。移除观察调用即可关闭报告。
目标验收继续使用现有 Stop hook；本批不增加第二套收尾状态机。
