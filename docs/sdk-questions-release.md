# 持久化问答首版：验收与发布说明草稿

记录日期：2026-10-05。随 0.62.0 发布。
本记录收口 P6.4 的 SDK 与 HTTP 首版，不代表完整可靠性矩阵已通过。

## 发布说明草稿

宿主现在可以保存模型提出的问题，让用户稍后回答，并在运行中的安全边界或
下一次显式轮次把回答送入会话历史。关闭、重启和重新连接后，宿主可以查询
问题、提交回执和最终接纳状态，不必依赖一条持续在线的连接。

- SDK 显式配置 `QuestionOptions` 与 `SQLiteSessionStore` 开启；支持同步／异步
  提交、取消和版本观察。与基础 asker 回调互斥，默认立即转待答。
- 可配置有限的前台等待。超时后保留 pending，及时与迟到回答使用同一投递路径。
- HTTP 创建会话时传 `questions` 开启；查询、幂等回答、取消以及现有 long-poll／
  SSE 均可使用。新增标准库宿主示例、离线演示和断线重连流程。
- queued 表示已持久化，answered 表示已进入持久化用户历史。重复提交使用原
  幂等键与原内容，不重复投递；空闲提交不会自动运行模型。

## 存储、权限与兼容边界

| 项目 | SDK | serve HTTP |
|---|---|---|
| 首版存储 | 显式 SQLiteSessionStore | serve 自有 JSONL，要求独占写锁与 fsync 成功确认 |
| 一致性 | 状态与用户消息在同一事务记录提交 | 状态与用户消息在同一日志记录提交 |
| 观察 | watch 初始快照与可合并的版本变化 | 有界事件缓冲；重连先查询完整快照与游标，按版本去重 |
| 恢复 | 同一会话 ID，重新启用 questions 接纳队列 | 服务启动恢复清单与日志，不自动执行模型 |
| 停止 | 关闭保留 pending／queued | abort／优雅停机保留；删除会话取消未接纳问题 |

两端共用内核内部状态机，serve 不依赖 SDK 包；不新增内核运行期依赖或顶层
公开导出。存储保证限定于已验证的本地文件系统与独占写入模式，不扩展为网络盘
或分布式写锁。SDK 的其他 SessionStore 尚不支持问答。

问题回答是普通用户输入，不批准工具、退出规划或提升权限。HTTP 沿用实例 token
与本机 Host／Origin 检查，没有用户级租户隔离。answered 不证明模型请求成功或
模型已经遵从答案。含问答状态的 SDK 会话不支持 conversation rewind，纯文件
回滚仍可用；fork 不继承可回答状态。

旧版本无法正确恢复新增接纳记录。部署时使用包含该功能的匹配版本内核／SDK，
不得用旧运行时打开新问答日志。发布前按现有双包构建与隔离安装流程验收。

## 验收记录

| 验收项 | 当前证据 |
|---|---|
| SDK 持久化、运行中接纳、前台等待 | 既有 deferred／foreground 问答专项覆盖 |
| HTTP 控制入口、鉴权、恢复与日志故障 | 既有 16 项 HTTP 问答专项覆盖 |
| 可运行宿主 | 标准库真实 HTTP 客户端；离线 TestClient 使用同一宿主操作 |
| 新增示例契约 | 5 项通过：及时／迟到回答、同键重试、淘汰／重启重连、截断字段恢复、旧版本去重与传输编码 |
| 拟提交内容独立回归 | 从基线加本系列改动生成临时副本：3,725 项，跳过 1 项，OK，227.235 秒 |
| 当前工作区兼容回归 | 含其他尚未提交改动：3,786 项，跳过 1 项，OK，229.835 秒 |
| 类型与示例 | mypy 分两组检查 28 + 4 个源文件通过；本系列 9 个离线示例全部通过 |
| 多平台 | Linux／macOS／Windows × Python 3.11／3.14：候选提交 `1cbcf05` 上内核与 SDK 两套工作流各 7 个任务全部通过 |
| 真实模型／长时负载 | 未执行；沿用独立 P4-R 待办，不由离线结果替代 |

仓库根目录的复现命令（需安装源码包、SDK 与相应可选依赖）：

```sh
python -m unittest tests.test_serve_questions_example tests.test_serve_questions tests.test_sdk_deferred_questions tests.test_sdk_foreground_questions -v
python examples/serve/questions.py --demo
python -m mypy --follow-imports=silent packages/xiaoyu-agent-sdk/src examples/sdk
python -m mypy --follow-imports=silent examples/serve xiaoyu/questions.py xiaoyu/serve_questions.py xiaoyu/_observation.py
python -m unittest discover -s tests -t .
```

仓库 venv 验证按 AGENTS.md 设置仓库绝对路径 PYTHONPATH 并使用 `python -P`。
CI 保留已钉定的 action SHA，新增示例进入同一六组契约矩阵，相关路径变化也能触发。
TestClient 所需的 `httpx==0.28.1` 在测试工作流中显式安装，不加入内核运行期依赖。
两次全量通过结果均来自本机 macOS；最初受限环境的端口／系统沙箱用例失败后，
使用获准的沙箱外执行方式复跑。独立副本不含其他搜索、工具进度和会话诊断改动，
因此两组测试数量不同；离线验证没有调用真实模型。

## 发布前待办与后续范围

- 多平台 CI 已在候选提交上实际运行并通过（见上表）。
- 版本号定为 0.62.0；第一人称模型自测通过，正式发布说明见 `docs/releases/0.62.0.md`；
  双包构建与干净安装由发版工作流在发布时验收。
- P4-R 的真实模型持续集成、24 小时混合负载与平台未覆盖分支单独验收。
- 问答前端、MCP 问答工具、自动唤醒和其他存储适配器按需求另立项；不计入首版。

使用契约：[SDK 问答](sdk-deferred-questions.md)、[HTTP 问答](serve-questions.md)、
[可靠性矩阵](sdk-reliability.md)、[双包发布](sdk-release.md)。
