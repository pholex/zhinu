# HTTP 持久化问答

自 0.62.0 起提供。serve 与 SDK 共用问题状态机；serve 使用自己的会话日志、访问控制和
事件缓冲，不需要安装 SDK 包。SDK 的 Python 调用见[持久化待答](sdk-deferred-questions.md)。

## 开启

创建会话时显式传 questions；省略或 null 保持原行为。服务必须开启持久化，
`persist=False` 拒绝启用问答。

```json
POST /session
{"questions":{"foreground_timeout_seconds":60}}
```

空对象 `{}` 等价于等待 0 秒，立即返回待答；有限正数开启前台等待。问题从 open
开始，超时转 pending；显式提交后先 queued，在安全 step／下一次显式轮次接纳为
answered。及时回答也只返回 queued 工具回执，完整答案通过普通 user 历史进入模型。

宿主使用 prompt_async 发起工作，再查询问题或消费事件。前台等待不增加模型请求，
提交回答不自动启动空闲模型。已有 token／费用预算与工具审批保持原样。

## 可运行的宿主示例

[questions.py](../examples/serve/questions.py) 的真实客户端只用 Python 标准库，
通过 REST 访问已启动的当前源码版 serve；不需要 SDK 包。离线演示需安装本仓库的
`[serve]` 可选依赖和 TestClient 使用的 `httpx`，采用真实路由、状态机与临时 JSONL
日志，仅模型由脚本替代，不监听端口、不访问模型、不读取 API 凭据：

```sh
python -m pip install -e ".[serve]" httpx==0.28.1
python examples/serve/questions.py --demo
python -m unittest tests.test_serve_questions_example -v
```

真实接入先按 [HTTP API](http-api.md) 启动服务。若服务使用 token，客户端通过
`XIAOYU_SERVE_TOKEN` 环境变量读取同一 token。以下 SID、QID、ITEM_ID 均替换为
前一步实际返回的标识；`--url` 可在子命令之前指定其他服务地址。

```sh
python examples/serve/questions.py create --foreground-timeout 60
python examples/serve/questions.py prompt SID '用 ask_user 询问我偏好的输出格式' --key ask-001
python examples/serve/questions.py watch SID
```

watch 首次读取含终态的完整快照，然后 long-poll；Ctrl-C 只结束观察。在另一个
终端提交答案。先由用户阅读题目，把明确输入写入 `answer.json`，每个 item_id
都要回答，以下仅示范自由文本：

```json
{"answers":[{"item_id":"ITEM_ID","custom":"请输出 Markdown"}],"idempotency_key":"answer-001"}
```

```sh
python examples/serve/questions.py answer SID QID answer.json
python examples/serve/questions.py get SID QID
```

保留原文件；丢失回执时用同一个文件重试，不换幂等键。遇到存储 503 应先由服务
维护方关闭、恢复并查询状态，不能直接假定写入未发生。真实模式不会挑选选项、
自动跳过题目或自动开始下一轮。若已空闲而答案仍 queued，由用户显式继续：

```sh
python examples/serve/questions.py prompt SID '根据我提交的答案继续' --key continue-001
python examples/serve/questions.py watch SID --once
```

再次运行 watch 即重连：先取快照与 next_seq，再按问题 version 去重。运行中的
示例把事件作为更新通知，只要出现事件或 first_seq 越过游标就重新取完整快照，
因此也覆盖 max_field 对嵌套字段的截断。连接异常会退出，保留服务端问题状态；
重新执行 watch 不会回答、取消或唤醒模型。`answered` 只证明历史接纳，模型请求
结果仍需查看 `/session/SID/status`。

首版验收边界与发布说明草稿见[交付记录](sdk-questions-release.md)。

## 查询与提交

以下端点沿用服务的 Authorization／X-Xiaoyu-Token 校验；无 token 时沿用本机
Host／Origin 限制。服务 token 仍代表对该实例的访问权，不新增用户级租户隔离。
question_id 只能在所属会话路径中使用，不是审批 request_id。

| 端点 | 返回 |
|---|---|
| `GET /session/{id}/questions` | `{session_id, questions, next_seq}`；默认含 open／pending／queued |
| `GET /session/{id}/questions?include_terminal=true` | 全部问题最新状态，包含 answered／cancelled；用于重连 |
| `GET /session/{id}/questions/{qid}` | `{question}`；包含版本、题目、答案和提交回执 |
| `POST /session/{id}/questions/{qid}/answers` | `{question}`；排队回执 202，同键重试时已接纳返回 200 |
| `DELETE /session/{id}/questions/{qid}` | 204；重复取消幂等，已接纳答案不能撤回 |

提交示例（使用查询返回的 item_id）：

```json
{
  "answers": [{"item_id":"item-id","selected":["Blue"],"custom":"","skipped":false}],
  "idempotency_key":"ui-submit-123"
}
```

问答幂等键在 JSON 请求体中；它与 prompt 端点的 Idempotency-Key 请求头分别管理。
同题同键同内容重试返回原 answer_id 的最新状态；不同键重复回答或改动同键内容
返回 409。每题必须显式回答或 skipped，不可漏项、重复、猜测默认选项或以空答复
代表跳过。题目、选择与文字长度沿用 SDK 校验；HTTP 请求体额外限制为 128 KiB。

错误：非法字段／答案为 400，超长请求体为 413，未知会话或该会话中不存在的问题
为 404，未启用问答／状态冲突为 409，存储不可用为 503。框架对创建会话请求的
类型错误可能返回 422。503 后先关闭服务、恢复并查询状态，使用原幂等键重试，
不要假定写入已经撤销。

## 版本观察与重连

问题事件进入现有 `/session/{id}/events` 和 `/events/stream`，没有独立 SSE 通道：

```json
{
  "seq":42,
  "kind":"question.reply_queued",
  "question":{"session_id":"session-id","question_id":"question-id","version":2,"state":"queued"}
}
```

这里省略了 question 的其他快照字段。kind 包括 question.opened、question.pending、
question.reply_queued、question.answered、question.cancelled；身份、代数、来源
工具调用 ID 和 version 均在 question 内。

1. 首次连接或重连时获取 `questions?include_terminal=true`，保存全部最新状态。
2. 从响应的 next_seq 继续 long-poll 或 SSE；快照前捕获游标，允许并发变化重复出现。
3. UI 按 `(session_id, question_id)` 保存最大 version，忽略旧版本及重复版本。
4. 若事件水位已越过游标、服务重启，或事件文本被 max_field 截断，重新取完整快照。

事件缓冲有容量上限，重启后不重放旧缓冲；快照和持久化状态才是恢复依据。
`follow=false` 的 SSE 会在空闲且追平游标后结束；长连接使用默认 follow=true。
断开 HTTP／SSE、收起 UI 不提交、不取消问题，也不重新开始前台倒计时。
`/status` 继续使用既有轮次状态，前台等待通过问题的 open 状态识别。

## 落盘与生命周期

serve 继续使用其 JSONL 会话日志。问题状态写入要求持有独占日志锁，并在同步文件
成功后才返回回执；拒绝日志的无锁降级。接纳状态与用户历史消息在**同一条记录**
中保存，没有第二份问题表。普通诊断日志的尽力写入行为保持原样。

重放沿用已封口残缺尾行的修复规则，问题版本和状态转换还需通过完整校验；中段
损坏拒绝恢复。已提交却丢失回执时，恢复从实际记录查证，不重复接纳。保证范围为
本地文件系统与现有进程独占写入模型，不扩展为分布式数据库或任意硬件断电保证。
SDK 仍只开放 SQLiteSessionStore；本次 JSONL 支持限定在 serve 的严格适配器中。

- abort／优雅停机：停止当前等待，未答 open 转 pending，已提交 queued 保留。
  遗留 open 在恢复时转 pending，不重开倒计时、不自动重跑模型。
- DELETE 问题：取消该问题并唤醒等待，不取消整个轮次。
- DELETE 会话：先标记关闭并从注册表摘除，后续请求返回 404；等待执行收尾后取消
  未接纳问题、关闭资源、释放日志锁。删除返回不代表后台清理已经完成；不会重新
  写回已删除的会话清单。日志文件作为留痕保留，不是数据擦除 API。
- fork：不复制可回答的问题状态；需要新分支提问时创建请求显式传 questions。
- 回答与取消、超时、关闭由同一把锁决定顺序；回答不能批准工具、退出规划或扩张权限。

前台等待会占用模型工作线程；HTTP 问题操作使用独立的两线程控制池，保证全部
模型线程都在等待回答时仍能提交或取消。没有为每个问题创建线程。

本批未提供问答前端、MCP 问答工具或自动唤醒策略；现有 REST／SSE 足以让宿主
显式驱动完整流程。
