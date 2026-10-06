# P4 外置会话存储与任务恢复

日期：2026-10-01。本文描述 0.59.0 的存储接口，0.58.0 不包含这些新接口。

```python
from pathlib import Path
from xiaoyu_agent_sdk import ModelOptions, Session, SessionOptions, SQLiteSessionStore

store = SQLiteSessionStore(Path("app-state/sessions.sqlite"))
options = SessionOptions(
    model=ModelOptions(model="your-model", api_key="your-key"),
    workspace=Path.cwd(),
    session_store=store,
)
with Session(options) as session:
    session.run("记住订单编号 42")
    session_id = session.session_id

# 可以在另一个进程中重新构造 store、options，并使用同一工作区恢复。
with Session(options, resume_id=session_id) as restored:
    restored.run("继续处理这个订单")
```

同步、异步会话均支持 `resume_id` 和 `session_id`。本地 `session_dir` / `resume_from`
继续可用，但不得与 `session_store` 混用；外置存储的 `session_path` 为 None。
`fork` 复制当前有效对话、生成新的 ID，并用子会话 options 中的后端持久化。
工作区改变需显式 fork；resume 要求与存储 metadata 相同的工作区。
凭据、模型配置、工具和回调由宿主重新提供，不存入会话 metadata。

## 存储契约

`SessionStore.open(session_id, *, metadata, resume=False)` 返回独占的 `SessionWriter`。
adapter 由宿主持有，SDK 只关闭每会话 writer；多个会话可共享 adapter。

- `read()` 返回 detached 的有序记录，开头恰好一条 meta；缺失会话不能恢复，
  新建不能覆盖已有 ID。metadata 包含格式版本、ID、模型名、工作区及开始时间。
- `append(record_id, record)` 原子追加；同 ID/内容重复提交不重复写入，同 ID
  不同内容报错。不同会话的记录 ID 互不冲突。SDK 不自动重试不确定的业务动作。
- `close()` 幂等，释放写者所有权。关闭后的读写必须拒绝。连接/锁未释放时不得
  假称 writer 已关闭；close 失败允许重试。
- 所有权竞争抛 `SessionLockedError`；其他存储故障抛 `SessionStorageError`，
  原始原因在 cause。SDK 不向公开错误正文复制 backend 的错误详情。
- 契约为同步 I/O，调用可来自不同线程，由 SDK 串行执行；adapter 需自行限定 I/O
  超时。同步 I/O 无法强杀，SDK 的 close timeout 不等于 backend 请求总时限。
  `AsyncSession` 的构造仍是同步操作，`await AsyncSession.open(...)` 把它放到线程里
  （当前源码新增，尚未发布）；业务运行与 close 复用已有 worker/线程桥。

日志记录沿用内核格式，包含对话、压缩/清空/回滚 replacement 和其他事件，复用
现有重放规则。写入失败令会话拒绝后续执行，不返回成功终结；宿主关闭会话后核对
后端再恢复。关闭会释放 writer，但不会把之前未确认持久化的记录补报为成功。

`SQLiteSessionStore(path, timeout=5.0)` 是本地持久化参考实现：事务保护记录，
每会话 OS 锁保护生命周期，进程死亡自动释放锁；不同会话分别持有连接。
同一 adapter 的初始化、读取和写事务按先到先得排队，避免多个本机连接反复抢占
SQLite 锁；连接的同步设置也在协调范围内。
排队与 SQLite 锁等待各受 `timeout` 限制；超时显式报错，未放宽 `FULL` 同步写入。
多个 adapter 或进程之间仍依赖 SQLite 的锁等待机制；共享 adapter 的排队不是
跨进程或分布式调度保证。失败日志保留原始异常链，后续错误不会抹去首次写入原因。
`store.list_sessions()` 返回 `StoredSessionInfo(session_id, metadata)`，不是已有
`list_sessions(directory)` 的路径列表。数据库新建为 0600；既有权限由宿主管理。
日志含工具结果等业务内容，SQLite 不是加密存储。
支持本机本地文件系统，不保证网络盘/多机部署的锁语义或所有存储设备的断电持久性。
跨机器后端必须实现自己的租约、续期和 fencing，不能复用 SQLite 的本机锁保证。

## 适配器契约验收

存储恢复使用日志重放；工具副作用与存储幂等分别验证，对话恢复不代表执行状态恢复。

契约助手位于源码仓库的 `tests/sdk_store_contracts.py`，不随 PyPI 包分发。
适配器作者可在仓库开发检出中对隔离后端运行：

```python
from tests.sdk_store_contracts import check_session_store
check_session_store(store, Path.cwd())
```

此函数创建并保留两个测试会话，验证顺序、detached 读、幂等追加、ID 冲突、单写者、
跨会话隔离、关闭后拒绝写入及重新打开；仅为功能契约，不认证跨机器 fencing。
`tests/test_sdk_storage.py` 另验证 SQLite/进程退出、未知工具结果补齐、已完成动作
不自动重放、损坏/新格式拒绝、失败后不报告完成、同步/异步与 fork/resume。
本机压力批次和未完成的可靠性验收项见 [P4-R](sdk-reliability.md)。

## 与高级 MCP 和子 Agent 编排的接入边界

后续批次已在同一 journal 增加 `sdk.tasks`、`sdk.subagent`、`sdk.cost` 记录，保存
稳定 session/run/task ID、依赖、尝试次数、终态、结果、已收尾子历史和请求账目。
恢复时未完成任务为 `lost`，未完成费用为未知，不自动启动执行。成功结果不重新
运行；fork 复制子历史，任务调度使用新会话身份重新建立。

worktree 文件、后台进程、MCP 活连接和 OAuth 令牌仍不随对话数据库恢复。
既有文件 rewind 只覆盖本进程内核记录的编辑；重启不恢复文件检查点。
MCP 动态清单和授权由宿主重新提供，令牌使用专用凭据存储。完整接口、恢复与
副作用边界见 [平台能力](sdk-platform.md)，故障证据见 `tests/test_sdk_tasks.py`。
