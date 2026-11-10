# 持久任务与跨会话记忆

本页能力通过 `xiaoyu-agent-sdk` 使用；记忆审核也提供 `xiaoyu memory` 命令。
使用标准库 SQLite，不增加运行依赖。任务和记忆使用各自独立的数据库文件。
模型不会自动开启这些能力，也没有新增常驻进程、后台学习任务或系统定时任务。

## 跨重启推进任务

`DurableTaskRunner` 由宿主串行调用。一次 `tick(id)` 最多执行一个回调；等待未到期、
已暂停或已结束时不调用模型。宿主可用自己的事件循环、服务或系统定时器周期调用。
示例：[草稿 → 人工审核 → 验收](../examples/sdk/durable_work.py)。

```python
from pathlib import Path
from xiaoyu_agent_sdk import DurableTaskRunner, WorkStep, WorkResult, TaskAcceptance

def execute(ctx):
    # 在这里驱动 Session 或 Session.tasks；上下文含上次 checkpoint 与验收 evidence。
    # 有外部副作用时，将 ctx.idempotency_key 传给支持幂等键的业务接口。
    return WorkResult("waiting", "CI 已提交", checkpoint="ci:42", fingerprint="running")

def probe(ctx):
    # 用 checkpoint 查询真实业务状态；探测器必须只读。
    return WorkResult("succeeded", "CI #42 已通过")

with DurableTaskRunner(Path("work.sqlite"), handlers={"build": execute},
                      probes={"ci": probe},
                      evaluators={"tests": lambda ctx: TaskAcceptance("passed", "CI #42 验收通过")}) as runner:
    run = runner.create((WorkStep("build", "build", probe="ci", evaluator="tests"),),
                        timeout=86400, max_calls=100)
    runner.tick(run.id)
```

上面演示接线，真实探测与验收由宿主实现；不要用固定的 `passed` 代替业务检查。
下次启动重新注册相同名称的回调，通过 `list()` 找回任务并调用 `tick()`。
`create()` 每次创建新任务；恢复时不要再次调用它。

### 三种回调与结果

`handlers/probes/evaluators` 是名称到同步函数的映射，不存入数据库。每次启动由宿主
重新绑定；找不到回调时任务进入 `paused`。回调接收不可变 `WorkContext`：
`run_id`、`step`、`attempt`、`checkpoint`、`evidence`、`idempotency_key`。

- 执行器负责业务动作，可调用现有 `Session.run` 或 `Session.tasks`。
- 探测器只读外部状态，等待期间只调用探测器，不重新调用执行器。
- 验收器只读检查业务结果，返回既有的 `TaskAcceptance`。

执行器／探测器返回 `WorkResult(status, evidence, checkpoint="", fingerprint="", next_check_at=None)`：

| status | 后续行为 |
|---|---|
| `succeeded` | 有验收器则进入 `verifying`，下次 tick 验收；否则该步骤完成 |
| `waiting` | 保存检查点和等待原因，按 `next_check_at` 或 interval 等待；必须配置 probe |
| `retry` | 消耗暂时性错误预算，指数退避；执行器未声明可安全重复则进入 `uncertain` |
| `actionable` | 外部状态需要修改，消耗修复预算，调度执行器；仅用于可重复的执行器 |
| `failed` | 任务失败，停止推进 |
| `blocked` | 暂停并保留证据，等待宿主介入 |

空的 checkpoint 保留旧值；非空值替换旧值。证据必须非空，字符串大小有界。
`next_check_at` 是未来的 Unix 秒；使用墙钟以便跨重启恢复。`fingerprint` 只描述实际
业务状态，不应包含当前时间。等待时相同指纹按 `max_unchanged` 计数，达到配置上限后
暂停；默认 0 不启用这项限制，仍受任务期限和调用总预算限制。

验收 `passed` 完成当前步骤；`failed` 为可重复的执行器安排有限修复；`unknown`
暂停等待宿主。未声明 `retry_safe=True` 的执行器不会自动修复重跑。
后续步骤只在前一步执行及配置的验收都完成后开始，成功步骤不会被后面的修复重跑。

### 预算与恢复边界

`WorkStep` 字段：`name`、`handler`、`payload=""`、`probe=""`、`evaluator=""`、
`retry_safe=False`、`max_retries=3`、`max_repairs=2`、`interval=60`、`max_unchanged=0`。
临时错误、验收修复、进程中断恢复分别计数；中断恢复上限也使用 `max_retries`。
计数、下次检查时间、任务 deadline 和 `max_calls` 均持久化，重启／resume 不重置。

执行回调前先提交运行状态，结果与推进状态随后在一个事务中提交。
重启遇到未完成回调：只读探测／验收可在恢复预算内重做；执行器须明确声明
`retry_safe=True` 才可重做，否则状态为 `uncertain`。**独立 worktree 不等于外部副作用安全**。
执行器抛异常或返回无效结果也进入 uncertain，不能据此推断动作没有发生。
探测异常走临时错误预算；验收异常记 unknown 并暂停。异常正文不存入证据。

`resume(id)` 只恢复 paused；`resume(id, allow_uncertain=True)` 表示宿主已核对并承担
不确定动作重做的风险。`cancel(id)` 持久停止任务。幂等键在同一步的崩溃恢复和临时
重试间保持不变；有依据的修复进入新一代键。幂等键本身不能使任意业务接口变得幂等。

一个数据库同时只有一个运行器拥有 OS 文件锁。所有变更接口串行，不在回调运行时
从另一线程强行 cancel/close；忙时会明确报错。宿主须为回调内的模型与网络 I/O 配置
超时；deadline 只阻止启动下一次回调，不能抢占已开始的同步函数。模型请求数和费用
继续用 `SessionOptions.budget` 限制，`max_calls` 统计包括探测在内的宿主回调次数。

`get(id)` 返回 `WorkRun`，包含步骤快照、预算与原因；`history(id)` 返回脱离内部状态
的转换摘要。运行器不改变原有 `TaskManager` 的失联／重试语义，也不另造并行任务图。
需要依赖图的步骤可在执行器中调用 `Session.tasks`，把 session ID／业务回执写入 checkpoint。

## 记忆作用域与审核

```python
from xiaoyu_agent_sdk import MemoryStore, MemoryScope, MemoryCandidate

store = MemoryStore(Path("memory.sqlite"))
scope = MemoryScope(owner="alice", project="project-a")
candidate = store.propose(scope, MemoryCandidate(
    key="test-runner", text="此项目使用 unittest", kind="lesson",
    source_ref="session-123#message:4", evidence="这个项目请用 unittest",
))
# 在宿主自己的审核界面展示候选与证据，经确认后再调用：
accepted = store.accept(scope, candidate.id, reviewer="alice")
```

`MemoryScope(owner, project="")` 按精确身份分区；个人作用域与项目作用域不自动混查，
同名项目也不会跨 owner 检索。作用域是宿主路由，不是抵御同一操作系统用户的安全边界；
多用户宿主须先认证，再选择作用域，不把客户端提交的 owner 直接当成授权。

记忆类型为 `preference/fact/episode/lesson/skill`。候选包含 key、正文、来源、原文证据、
`base_revision=0` 和可选 `expires_at`（Unix 秒）。`MemoryRecord` 保存候选、作用域、
id、status、revision、created_at 和 reviewed_by。

`propose()` 只入 pending，不参与检索。`accept()` 检查 base_revision 与当前版本；
首次写入为 0，更正须填写当前 revision。并发纠正或过期候选会抛 `MemoryConflict`，
不会覆盖新事实。被替代的版本保留为 superseded，`list(scope, key=...)` 查看历史。
没有凭模型置信度自动批准、自动覆盖人工约定的通道。当前指令和权限始终优先于记忆。

`reject()` 拒绝待审候选。`forget(scope, key)` 清空该 key 的所有候选和历史版本正文、
证据与来源引用，停止检索；保留身份、修订号和提取回执，防止旧提取重放把内容复活。
这是逻辑删除，不保证清除 SQLite 空闲页、宿主备份或已经写入会话日志的引用。

CLI 示例（相同作用域参数适用于所有子命令）：

```bash
xiaoyu memory --owner alice --project project-a propose test-runner '使用 unittest' --kind lesson --source 'chat#4' --evidence '这个项目请用 unittest'
xiaoyu memory --owner alice --project project-a list --status pending
xiaoyu memory --owner alice --project project-a show <id>
xiaoyu memory --owner alice --project project-a accept <id> --reviewer alice
xiaoyu memory --owner alice --project project-a recall '测试'
xiaoyu memory --owner alice --project project-a history test-runner
xiaoyu memory --owner alice --project project-a forget test-runner
```

默认数据库为用户配置目录的 `memory.sqlite3`，`--db` 可显式选择。`reject <id>` 拒绝候选。
审核操作是宿主／用户入口，不应注册成免确认的模型工具。

## 检索与会话接入

`store.recall(scope, query, limit=8, char_budget=4000)` 用关键词及中文双字匹配返回
已接受、未过期的完整记录。空查询只选稳定偏好与经验；不会在检索时触发模型、写入
访问计数或自动修复数据库。超出预算的单条记录不截断，避免丢掉规则中的否定条件。
`store.context(...)` 返回含来源的有界参考文本，空结果返回空串。

SDK 显式开启首轮接入：

```python
options = SessionOptions(
    model=model, workspace=workspace,
    memory_store=store, memory_scope=scope, memory_char_budget=4000,
)
```

每个 Session 实例的首轮先检索稳定偏好／经验，再按本轮输入检索相关事实；总字符数
受预算约束，以普通参考通知进入上下文，不成为 system 权限指令。后续不逐轮重复注入。
恢复会话时由宿主当前 options 决定作用域，日志不会偷偷开启记忆或指定数据库。
未设置 store/scope 就不读取记忆。数据库读取失败明确报错，不当成“没有记忆”。

随时可在空闲时调用 `session.recall_memory(store, scope, query)` 得到参考文本，再由
宿主选择 `session.notify(text, wake=False)` 投递。异步会话对应方法需要 await。
更正／遗忘不会回溯擦掉已经送入模型的旧上下文；需要完全排除旧引用时开启新会话。

## 从会话提取经验候选

`session.extract_memory(store, scope, extractor, start=0, max_chars=16000, timeout=30)`
显式触发；不会在结束会话、启动 CLI 或定时检查时偷偷调用模型。
示例：[有请求预算的候选提取](../examples/sdk/memory_learning.py)。

extractor 接收不可变 `MemorySpan(session_id, start, end, messages)`；每条
`MemoryExcerpt(source_ref, role, text)` 有可核验的原始消息引用。返回至多 20 条
`MemoryCandidate` 元组，每条 evidence 必须是相应消息的真实原文。此核验只确认出处，
不证明结论为真，所以全部进入 pending。长历史按完整消息取有界前缀，end 可供宿主
安排下一次显式提取；首条消息超预算时报错，不悄悄略过。角色与原文不被模型改写成授权。

候选和来源片段摘要回执在同一 SQLite 事务提交：失败全部回滚；同一片段重试返回
已有记录，不再次调用 extractor；相同片段身份但原文变化会报冲突。回执不再复制原文。
回调异常／超时不写回执，稍后可重试。超时的同步回调无法被强杀，Session 在它实际退出前
继续持有资源并拒绝新工作；迟到的结果不会写入候选。AsyncSession 可使用 async extractor。
提取回调的模型费用由宿主限制，示例使用独立 Session 和 `max_requests=1`。

kind=skill 的候选可包含完整 SKILL.md 正文。接受后通过
`store.export_skill(scope, id, path)` 或 `memory ... export-skill <id> <path/SKILL.md>`
显式写入新文件，校验 frontmatter，不覆盖现有技能，不生成或执行辅助脚本。
导出的文件是独立副本；撤销其使用需删除／禁用该技能文件，forget 只影响记忆库。
向量检索、自动技能重写和自动淘汰暂不提供。

机制接线后的验证入口见[持续运行与记忆验证](continuity-validation.md)：
独立进程故障矩阵、跨会话对照与可恢复的连续运行实验，默认不调用模型。
