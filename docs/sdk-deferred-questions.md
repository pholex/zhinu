# 持久化待答：SDK 首版

自 0.62.0 起提供。已实现 P6.4 前三批：**可选前台等待、跨重启提交、安全边界接纳与版本观察**。
serve 已提供独立的 [HTTP 接入](serve-questions.md)；完整目标见
[设计稿](sdk-deferred-questions-design.md)。已有 `asker` 回调继续使用原来的等待语义。

## 开启与使用

```python
from dataclasses import replace
from pathlib import Path
from xiaoyu_agent_sdk import (
    QuestionAnswer, QuestionOptions, Session, SQLiteSessionStore,
)

options = replace(options, questions=QuestionOptions(),
                  session_store=SQLiteSessionStore(Path("sessions.sqlite")))
with Session(options) as session:
    session.run("通过 ask_user 询问输出格式，再处理不依赖格式的准备工作")
    for question in session.questions.list_pending():
        # 真实宿主应在 UI 中收集回答。这里只演示显式跳过。
        answers = tuple(QuestionAnswer(item.item_id, skipped=True) for item in question.items)
        receipt = session.questions.answer(question.question_id, answers, "ui-request-123")
        assert receipt.state == "queued"
    session.run("根据我的回答继续")
```

`QuestionOptions(foreground_timeout_seconds=0)` 默认立即待答；设为正数可开启
前台有界等待，例如 60 秒。接受有限的非负 int／float，拒绝布尔值、负数、NaN 和无穷。
仅支持显式 `SQLiteSessionStore`；内存、裸 JSONL 和其他 SessionStore 适配器暂不支持。
与 `SessionOptions.asker` 互斥；启用时 `ask_user` 由 SDK 持有，宿主工具／插件不能同名覆盖。
默认不开启，不影响已有会话。

模型继续使用现有 `ask_user(questions=...)` schema。SDK 为批次生成 question_id，
为每题生成 item_id。默认持久化后立即返回 pending 与问题 ID；开启等待时先持久化
open，等待及时回答或超时；pending 不是空答案或默认选择。
工具说明要求仅继续独立工作，依赖答案的任务应等待。首版只开放主会话提问，
不会给子 Agent 自动挂问答界面。

`AsyncSession.questions` 的 get、list_pending、answer、cancel 均须 await，内部以
线程执行存储操作，不阻塞事件循环。等待协程被取消不保证写入撤销；使用同一个
幂等键重试并查询状态。完整重启示例见
[deferred_questions.py](../examples/sdk/deferred_questions.py)，`--demo` 不联网。

## 只读快照与提交

| 类型／方法 | 内容 |
|---|---|
| `QuestionOption` | label、description |
| `QuestionItem` | item_id、question、options: tuple[QuestionOption, ...]、multi_select |
| `QuestionAnswer` | item_id、selected: tuple[str, ...] = ()、custom = ""、skipped = False |
| `QuestionSnapshot` | question_id、session_id、generation、tool_call_id、items、state、version、answers、answer_id、idempotency_key |
| `questions.list_pending()` | tuple[QuestionSnapshot, ...]，包含 open、pending 和 queued |
| `questions.get(question_id)` | 查询该会话中的问题，包括已接纳或已取消的终态 |
| `questions.answer(question_id, answers, idempotency_key)` | 校验并持久化回答，返回当前快照 |
| `questions.cancel(question_id)` | 取消 open／pending／queued，返回 cancelled；重复取消幂等 |
| `QuestionEvent` | kind、question: QuestionSnapshot；不可变的状态观察事件 |
| `questions.watch()` | 同步迭代器／异步生成器，先返回已有问题状态，再观察变化 |

以上数据类均为 frozen，嵌套集合为 tuple。首次版本为 1，状态变化递增；快照不会
随之后的操作改变。宿主可查询快照或订阅下述观察流，不自动发布唤醒通知。
不同浏览器客户端应请求持有 Session 的同一个宿主服务；不能在另一
进程同时打开同一会话来绕过独占写入锁。问题 ID 不赋予跨会话访问权。

回答必须包含每题各一次，按 item_id 匹配。拒绝遗漏、重复 ID、未知选项、单选题
多选、非法类型和空答复。每题可选择已展示标签、填写 custom，或显式 skipped；
skipped 不能同时携带选择或文字。选项标签不能重复；问题文字最多 4,000 字符，
标签 200、描述 1,000、每题自由回答 4,000，幂等键非空且不超过 200 字符。
批次题数及选项数沿用内核的 4／9 上限。

同题、同键、同内容的重试不新增回答或投递，返回最新状态及原 answer_id；选择
顺序按题目选项顺序归一。相同键换内容，或另换幂等键重复回答，抛
`QuestionConflictError`。未知问题抛 `QuestionNotFoundError`；非法载荷抛
`ConfigurationError`。这里的幂等键以问题为范围，不是跨会话的全局键。

## 前台有界等待

```python
options = replace(options, questions=QuestionOptions(foreground_timeout_seconds=60))
```

等待在原有会话工作线程中进行，释放会话互斥锁供宿主提交；不另建计时线程，
等待本身不发起模型请求。宿主需在另一个线程／协程通过 questions.watch() 或查询
展示问题并收集回答；不能先等待 run 返回才尝试及时回答。离线示例的
`foreground_demo` 展示 AsyncSession 的完整流程。

- 正数超时：问题从 open 开始，发出可合并的 question.opened 观察事件。超时按
  单调时钟计时，持久化转 pending 后工具返回；依赖答案的工作仍应等待。
- 及时回答与迟到回答统一先持久化 queued；及时回答唤醒工具等待，工具只返回
  queued 回执，完整答案仍在本批工具收尾后的安全边界进入普通用户历史。
  answered 不会提前于历史提交，也不会把完整答案重复写进工具结果。
- 回答与超时争用同一把锁。提交先完成则唤醒；超时先完成则转 pending，之后
  仍可提交。两种顺序都只保存一个 answer_id、最多接纳一次。
- questions.cancel() 取消问题并唤醒等待，工具返回 cancelled；它不等于取消整轮。
  interrupt()、取消异步 run、关闭会话则结束当前执行：未答的 open 转 pending，
  已提交的 queued 保留。存储失败时以关闭后重放查证的状态为准。
- 关闭观察者、收起 UI 不提交、不取消问题，也不重置倒计时；无回答则等到超时。
  显式跳过必须提交 QuestionAnswer(skipped=True)。运行中仍不能 reset 或 fork。
- 恢复同一会话时，遗留 open 持久化转 pending 且版本递增；不恢复旧进程的倒计时，
  不启动模型。重复恢复不重复递增版本。

及时回答仍受预算、中断和审批边界约束。这里的等待设置独立于基础 asker 的
question_timeout；前台超时正常转待答，不是工具异常，也不采用任何默认答案。

## 状态与接纳

```text
open ──超时／中断／恢复──> pending
  │                        │
  └────────提交回答─────────┴──> queued ──安全边界接纳──> answered
open / pending / queued ──显式取消／reset──> cancelled
```

`queued` 表示持久化成功，尚未进入模型历史。提交回答不启动模型，不计模型 token，
空闲会话不会自动启动新一轮。运行中的回答在整批工具结果写完后，或模型给出
收尾正文的 step 边界接纳；后者使当前轮继续一步。它不插入未配对的工具调用
中间，也不复用易失的 steer 队列。结构化结果提交等提前结束路径若没有到达该
边界，回答仍为 queued。错过边界的回答留待宿主下一次显式 run／stream。

下一轮的接纳仍发生在 UserPromptSubmit hooks 放行之后、用户输入入历史之前。
回答是一条带问题原文、question_id、answer_id 的普通 user 消息，不带 operator／
system 权限。`answered` 只证明已加入持久化历史，不保证下一次模型请求成功、
模型已经读取或遵从了它。中断、关闭或预算预检查已耗尽时保留 queued；新增上下文、后续网络
失败等仍可能使接纳后的模型请求失败，不应把 answered 当成远端消费回执。

提问回答不批准任何工具、权限升级或退出 plan；审批、deny 和模式约束保持原样。
默认选中、收起 UI、超时都不等于提交。内容属于宿主提交的用户输入，不走工具
返回文本变换；问题、选择和自定义文本会保存在宿主指定的数据库中。

## 带版本的问题观察

```python
# AsyncSession：由宿主安排观察协程，与 run/stream 一起运行。
async for event in session.questions.watch():
    question = event.question
    print(event.kind, question.question_id, question.version, question.state)
    # UI 按 (session_id, question_id) 保存最大 version，忽略旧版本。
```

同步 Session 使用 `for event in session.questions.watch()`；需要运行期间观察时，
由宿主安排观察线程。提前退出应 close／aclose 生成器（可用 contextlib 的
closing／aclosing），会话关闭也会唤醒等待者并结束流。

- kind 为 `question.opened`、`question.pending`、`question.reply_queued`、`question.answered`、
  `question.cancelled`。身份、代数、来源 tool_call_id 和 version 在 event.question 中。
- 首次订阅及重连先返回全部已有问题的最新状态，包含终态；空会话等待首次变化。
  后续只返回版本递增的问题，同键重试不产生新版本。reset 也会通知待答项取消。
- 每个观察者只保留一个唤醒信号，不按变化次数累积队列；读取时取最新快照。
  慢观察者可以从 pending 直接看到 answered，中间 queued 可能被合并。
  这是状态观察流，不是每一次状态转换必达的审计流，也不支持事件游标回放。
- 观察者独立于 run／stream 的 UIEvent 队列，不阻塞模型或持锁写入；多个观察者
  不互相消费。重连时可按版本去重，持久化记录仍是恢复的依据。
- 只发布已成功提交的内存状态；存储故障后应关闭并恢复会话查证。关闭会结束
  观察，不承诺排空最后的事件；再次恢复可读取最终持久化状态。

## 事务、恢复与生命周期

SQLite 会话独占写入租约与 Session 的互斥锁串行化提交、取消和接纳。每次状态
写入是一条原子记录，成功落盘后才更新内存、返回回执。接纳记录
`sdk.question.delivery` **同时包含 answered 状态和普通用户消息**，由现有会话
重放器还原；没有“先落历史、再改问题表”的第二次提交窗口。

写入成功但回执丢失时，会话按存储失败停止继续操作。关闭、恢复后从事务记录
查证结果；提交用同键重试，接纳不会重复注入。持久化一致性依赖参考适配器的
SQLite 事务与本地文件系统，不扩展为任意网络盘、硬件断电或自定义后端的保证。

- 恢复同一会话保留 pending／queued／终态，遗留 open 转 pending；关闭不取消问题。未开启 questions 的
  恢复会话不接纳队列，之后重新启用仍可查询；历史中已接纳的消息照常恢复。
- reset 的 clear 记录同时使待答／待投递问题失效、代数递增。旧问题不能向新一代
  提交答案；已接纳的历史清除后也不会重新投递。
- fork 不复制可回答的问题状态；已在历史中的回答仍作为背景复制。
- 首版不支持含持久化问题状态的会话做 conversation rewind，包括关闭功能后
  恢复同一日志的情况；它会抛 ConfigurationError。纯文件 rewind 保持可用，
  对话分支请用 fork，清空请用 reset。
- 已接纳答案不能用 cancel 撤回；需改正时提交新的普通用户消息。取消排队和
  接纳的竞争由同一把锁决定唯一胜者。

Python 问题服务不提供删除会话；HTTP 接口见 [serve 问答](serve-questions.md)。旧版本不认识
新增的接纳记录，不能用于恢复本功能产生的会话；部署宿主应使用匹配版本的双包。
