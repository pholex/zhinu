# 会话诊断

`xiaoyu sessions inspect` 只读扫描本地 JSONL，按追加顺序列出用户输入、模型请求、
工具调用与结果、拒绝和压缩事件。不连接模型，不恢复执行，也不修改会话。

```bash
xiaoyu sessions inspect 1
xiaoyu sessions inspect my-session --kind request --errors
xiaoyu sessions inspect ./saved-session.jsonl --turn 2 --raw
xiaoyu sessions inspect 1 --request 3 --json
xiaoyu sessions inspect 1 --tool-call call_123 --raw
xiaoyu sessions inspect 1 --kind compact --kind approval --limit 50
```

引用支持历史列表序号、命名会话、文件名和直接文件路径。`--all` 跨工作区查找。
默认返回过滤后的最后 200 条，可用 `--limit` 调整；表头区分命中总数和实际显示数。

每条记录标出 `L` 物理行号和 `R` 请求序号。请求编号在整个文件中递增，每次重试
独立编号。工具结果按调用 ID 关联到产生它的请求。一个 assistant 记录含多个工具时，
各工具占一条展示行，但 `L` 指向同一个原始记录。没有请求记录的旧日志显示 `R?`。

`--turn` 按非系统注入的用户消息划分输入段，0 是会话前言，1 是首段。
运行中插话也会另起输入段，因此这个编号不是 SDK `run_id`。系统注入消息不增加编号。
诊断展示的是完整追加历史：压缩、清空和回退不会从视图中删除早期记录。

`--kind` 可重复，支持精确事件名和类别：`tool` 匹配调用与结果，`request` 匹配
模型请求，`approval` 匹配可识别的拒绝，`compact` 同时匹配开始、结果与结束。
`--errors` 显示失败请求、空补全、以 `ERROR` 开头的工具结果、非零 shell 退出码、
可识别的拒绝以及损坏记录。没有统一错误标志的第三方工具自然语言结果不作猜测。

默认摘要至多 240 字符；`--raw` 添加完整记录字段，仍做凭据脱敏与终端控制字符
清理。它会包含记录中的对话、参数与工具正文，不等于仅含运行指标的报告。
`--json` 输出相同结构，适合宿主进一步处理。读取使用固定文件大小边界，检查正在
运行的会话不会持续追随新数据；末尾半行与中段损坏分别报告。外部字节无法解码时
使用替换字符继续读取。

当前持久化日志没有完整审批生命周期，也没有每个流式参数分片。拒绝只按已知
工具结果格式识别；不推断批准时间、审批等待时长、缺失的模型请求或工具耗时。
实时参数生成进度由 `tool.preparing` 事件提供，见[事件契约](embedding.md#事件消费)。
