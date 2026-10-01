# SDK 分批验收记录

每节记录该批次当时的源码及发布状态；后续批次不会覆盖早期失败或扩大早期结论。

## 2026-09-30：首版发布前本机验收

环境：macOS 27.0 arm64，Python 3.14.3。
此节测试运行时源码尚未提交、推送或发布；随后 0.58.0 已完成远端 CI 和发布。

| 验证 | 结果 |
|---|---|
| 原有功能与 SDK 全量 unittest | 3,249 项通过；219.913 秒 |
| 最后 MCP 状态目录隔离修订后的 MCP 回归 | 190 项通过；62.382 秒 |
| 最后 SDK/关闭/发布契约定向回归 | 43 项通过；其中 SDK 契约用例 34 项 |
| SDK 包和示例 mypy | 8 个源文件通过 |
| 安装版 SDK 的示例类型检查 | 4 个示例源文件通过 |
| 内核与 SDK wheel/sdist | 均构建成功；wheel 从各自 sdist 重建 |
| namespace、版本、精确依赖、py.typed | 构建脚本检查通过 |
| 制品 SHA-256 与提交清单 | release-manifest.json 生成和离线验证通过；本地工作区标记 dirty |
| 工作流 YAML | ci、sdk、release 三份均解析通过；未计作远端运行成功 |
| 新建虚拟环境，仅以 SDK wheel 为安装入口 | 安装成功，`pip check` 通过 |
| 安装制品的隔离调用 | `python -I tests/sdk_wheel_smoke.py` 通过 |
| 未选 extras | 干净环境未安装 fastapi、playwright、prompt_toolkit、rich |
| 同步一次性、多轮、异步、流式示例 | `--demo` 均通过 |
| 业务示例及独立进程恢复 | 审批、工具、一次修复、有效结果、关闭、恢复均通过 |
| 真实本地 stdio MCP（脚本模型） | 独立会话目录、服务发现、search_tool → 宿主审批 → use_tool、结果返回、关闭均通过 |
| 真实模型冒烟 | deepseek-flash，一次请求，结构化整数 42，`valid` / `done` |

真实请求使用项目已经配置的端点与凭据，仅发送“返回整数 42”的任务和标准系统提示，
工作区为临时空目录，内置工作区工具为空。耗时 2.852 秒；prompt 1,452 token，
completion 38 token。此结果只证明该模型的一次基础调用，不代表模型质量或生产可靠性。
凭据及端点地址不写入本记录。

主要失败分支覆盖：默认审批拒绝、参数改写后的 deny 检查、审批超时/取消、业务异常
与 TimeoutError、模型异常交给宿主、中断后继续、背压下提前关闭流、关闭超时后重试、
合法 null、无输出、非法 JSON、schema 失败后修复、耗尽、迭代/token 预算中止、
修复中中断、不重跑已完成业务调用、usage 计入修复、日志锁、恢复/分叉、回滚冲突、
显式技能来源、显式插件选择、静态 MCP 状态与资源所有权、强制 worktree 不可降级、
多工作区审批/事件/工具/用量隔离、类型化回滚的 conflict/partial/unavailable、MCP
缓存/声明/日志目录隔离，以及制品篡改和部分发布的重试判断。

全量测试之后增加了 MCP 会话独立状态目录及其用例；对最终代码重跑上述 190 项
MCP 回归、43 项相关契约、mypy，并重新构建双包、重新安装到隔离环境运行 pip check
与安装制品冒烟。未把不同批次的测试数相加当作覆盖率。

复现：

```sh
python -m pip install -e '.[sdk]'
python -m pip install -e packages/xiaoyu-agent-sdk
python -m unittest discover -s tests -t . -q
python -m unittest tests.test_sdk tests.test_output_schema tests.test_embedding_hardening tests.test_user_config tests.test_budget_pacing -q
python -m unittest tests.test_sdk_release tests.test_mcp tests.test_mcp_failure_paths -q
python -m mypy --follow-imports=silent packages/xiaoyu-agent-sdk/src examples/sdk
python scripts/build_sdk.py
```

全量测试需要允许启动本机 HTTP 服务和运行操作系统沙箱测试。第一次在受限的嵌套
沙箱内执行时，这些操作被系统拒绝；在允许这些测试操作的环境重跑后全部通过。

尚未完成的发布门禁：GitHub 的 Linux/Windows/macOS × Python 3.11/3.14 矩阵实际
运行，PyPI 包名与发布权限核验、内核先发/SDK 后发的真实远端流程。顺序发布与
索引哈希核对已接入现有 release.yml，见 [发布操作](sdk-release.md)，尚未执行上传。
长任务、宿主崩溃与故障压力等完整可靠性矩阵归 P4-R，不计入首版已验证范围。
具体场景、负载、通过阈值和当前预检范围见 [P4-R 可靠性验收矩阵](sdk-reliability.md)。
新工作流已提供跨平台验证，但本机没有把它记作已通过。
资源限制与 SDK 契约见 [SDK 指南](sdk.md)。

## 2026-10-01：P4 外置存储第一批验收

本次工作区新增 `SessionStore` / `SessionWriter` 契约、SQLite 参考适配器、按会话
ID 恢复、存储契约检查器及同步/异步示例。详细边界见 [存储说明](sdk-storage.md)。
这些新增接口尚未发布；下列 0.58.0 制品为本地候选，不能据此判断 PyPI 版本已包含它们。

| 验证 | 结果 |
|---|---|
| 最终源码全量 unittest | 3,263 项通过；218.724 秒 |
| 新增存储用例 | 11 项通过，已包含在全量测试中 |
| SDK 包及示例 mypy | 11 个源文件通过 |
| 存储离线示例 | `examples/sdk/storage.py --demo` 通过 |
| 最终源码双包构建 | 各自从 sdist 重建 wheel，包边界及精确依赖检查通过 |
| 最终制品清单 | SHA-256 验证通过；基线 `62d3457`，工作区为 dirty |
| 干净虚拟环境安装最终双包 | `pip check` 和 `python -I tests/sdk_wheel_smoke.py` 通过 |

存储用例包括独占所有权、跨进程锁竞争、进程异常退出后的锁释放与恢复、写入失败
停止后续运行、重复写幂等与冲突、关闭失败后重试、损坏/不兼容记录拒绝恢复、
已完成工具不自动重跑，以及未知工具结果的显式恢复。它们不证明业务副作用 exactly-once。

最终制品位于 `/tmp/xiaoyu-sdk-p4-final-20261001`；全量测试日志位于
`/tmp/xiaoyu-p4-full-tests-final.log`。本次未执行新增代码的远端 CI、提交、推送或发布。
此存储批次当时尚未实现子 Agent 状态持久化、高级 MCP 管理和复杂编排；
后续实现和验证见下一节，不倒填为本批验收结果。

## 2026-10-01：高级 MCP、编排及平台接口

工作区候选，尚未发布；已发布的 0.58.0 不含新增接口。基线 `62d3457`，保留
现有工作区改动。本轮实现 OAuth/动态 MCP、持久化依赖任务与子历史、按 ID 取消、
共享请求/费用预算、扩展 Hooks 和可选 OpenTelemetry；详见 [平台指南](sdk-platform.md)。

| 验证 | 结果 |
|---|---|
| 最终源码全量 unittest | 3,305 项通过；232.245 秒 |
| SDK 包与示例 mypy | 18 个源文件通过 |
| macOS Python 3.11.15 SDK 契约 | 104 项通过；9.579 秒 |
| Linux arm64 容器 Python 3.11.15 SDK 契约 | 104 项通过；8.947 秒 |
| Linux arm64 容器 Python 3.14.3 SDK 契约 | 104 项通过；9.059 秒 |
| 两个 macOS Python 版本与两个 Linux 容器安装检查 | `pip check`、已安装 wheel 的隔离平台 API 冒烟通过 |
| 编排与动态 MCP 示例 | 两个 macOS Python 版本及两个 Linux 容器通过 |
| CLI JSON 事件兼容性及 SDK 关联 ID | 修复后 24 项定向用例通过；3.250 秒 |
| 最终修复后 Linux Python 3.11.15 / 3.14.3（增加 CLI/golden） | 各 169 项通过；11.094 / 11.637 秒；最终 wheel 隔离安装通过 |

本轮保留以下失败和修复记录：

- 慢遥测关闭重试曾重复抛出已完成清理 Future 的旧异常，已修正后复测。
- MCP HTTP 封装最初不符合仓库 HTTP 错误释放检查，已改为原调用点控制重定向。
- 全量 3,305 项的一次运行发现 CLI JSON golden 多出工具关联 ID；已保持 CLI
  序列化兼容，同时保留 SDK 事件、成本与 trace 关联，复跑全量 3,305 项通过。
- 一次全量命令遗漏 `-t .` 造成相对导入错误；Linux 初次夹具漏复制
  `docs/embedding.md` 导致两个文档契约失败。源码 golden 子进程还需统一 PYTHONPATH，避免源码/安装包的文档路径混用。
  均纠正命令/夹具后重跑，未修改门禁。
- 30 秒、无预热的持续测试预检未通过 RSS 斜率；保留为失败，不替代正式 1 小时
  含 10 分钟预热的固定配置。

Linux 为本机 Docker arm64 容器，不等同于 GitHub 的三平台六组 CI。当前候选的
Windows、远端 CI、真实模型持续任务及完整故障矩阵尚未全部验证。
本地压力批次与复现命令见 [可靠性报告](sdk-reliability.md)。

最终源码全量日志：`/tmp/xiaoyu-p4-platform-full-final-pass.log`。
最终双包（各自从 sdist 重建）及哈希清单：`/tmp/xiaoyu-sdk-platform-jsonfix-20261001`。
SDK wheel SHA-256：`89dba6367138e602e2248f301b35ae320b8b810f8f28f804bcdcc3d3da3178b5`。
内核 wheel SHA-256：`7feb924fa1d0bd0a2bc3c4baae456dd66de69fdd8801cf1e7f73f97713c3cc1d`。

### 协议故障验证中发现的修复及最终补验

三协议真实 HTTP 注入发现 Anthropic 缺少 `message_stop` 时可能落入空回复路径；
适配器现要求终结标志，未完成流明确失败。对应两个旧单元夹具补齐合法终结事件，
保留“正常 max_tokens 截断”与“传输未完成”的区别，没有放宽既有断言。

同时保留 Responses 缓存读取及 Anthropic 缓存读取/写入细分，缺失用量不再合成
可计费的零值。缓存写入缺价记为未知，美元闸门阻止后续请求。

| 修复后验证 | 结果 |
|---|---|
| 源码全量 unittest | 3,312 项通过；281.885 秒 |
| 三协议 HTTP、预算、Messages/Responses 定向 | 117 项通过；12.196 秒 |
| macOS Python 3.11 SDK 与协议适配器 | 211 项通过；23.232 秒 |
| Linux Python 3.11.15 / 3.14.3 SDK、CLI/golden、协议及新故障时点 | 各 276 项通过；26.114 / 26.764 秒 |
| Windows 11 ARM64 / 原生 Python 3.11.15、3.14.3 同组契约 | 各 276 项通过；138.930 / 137.615 秒；隔离 wheel 冒烟与示例通过 |
| MCP 阻塞停止与 Session.close 竞态 | 在全量发现测试之后追加；单独及 Linux 验证通过，另纳入 100 次重复批次 |
| 类型检查、双包重建、两个 macOS/两个 Linux 安装制品冒烟 | 全部通过 |

最终制品已更新为 `/tmp/xiaoyu-sdk-platform-verified-20261001`，其
`release-manifest.json` 和 [机器可读记录](sdk-reliability-local-results.json) 保存最新哈希。
最新全量日志为 `/tmp/xiaoyu-sdk-platform-verified-final.log`。

持续负载未开启预算，也未调用真实协议适配器。协议与缓存修复涉及的
`xiaoyu/messages.py`、`xiaoyu/responses.py`、`xiaoyu_agent_sdk/budget.py` 在该批次冻结
后改变，因此长期负载按其原始源码快照记录；修复由上述全量、安装包和三协议
100 次故障批次另行验证。不把修改前的长期结果称为最新完整源码验收。

### Windows 补验中的环境与报告修正

使用本机现有 Windows 11 虚拟机，测试副本、Python 和便携 Git 放在来宾临时目录。
先前 x64 仿真环境的 Python 3.14 同组 276 项通过（184.676 秒）；增加并行批次后，
崩溃测试出现 15 秒宿主进程守护超时。分阶段探针测得依赖导入耗时 18.234 秒，
会话初始化与运行约 0.4 秒；串行对照导入降至约 5–8 秒。原生 ARM64 环境预热后
导入约 4–5 秒，双版本契约复测通过。上述是本机条件下的观察，不推导原生架构
与仿真架构的普遍性能差异；环境、并发和预热均发生了变化。

首次 Windows 示例因源码快照未包含示例文件而失败，补齐独立哈希清单后通过。
验收脚本遇到 POSIX 专用测试被跳过时，也暴露了直接序列化 TestCase 对象的问题。
报告现保存测试 ID 与原因，跳过项使批次保持 `blocked`，实际失败仍优先标为
`failed`；新增两个回归用例在 macOS 和 Windows 双版本通过。后续源码归档同时
包含 `examples/sdk`。这些修正不涉及 SDK/内核运行时代码，首次失败和中断记录保留。

Windows POSIX 信号/FIFO 用例不计入已通过范围；完整可靠性批次结果另见
[可靠性矩阵](sdk-reliability.md)。

### 常驻宿主工作区缓存修复

重复契约发现，`sandbox.note_workspace` 每登记一个工作区，会重新解析此前所有
工作区的默认可写路径，累计成本随工作区数量呈平方增长。独立的 15 轮 R11 CPU
诊断中，`default_writable_roots` 被调用 11,325 次，路径解析成为主要热点。

修复保持历史工作区的不信任规则：环境未变时只解析新登记的工作区，并合并被父
目录完全覆盖的拒绝路径；环境变化时重建缓存。新用例验证 200 个工作区只解析
200 次，历史路径仍被拒绝，父子覆盖和相似路径前缀不会混淆。相同的诊断负载从
8.663 秒降至 2.277 秒；这是带 CPU 分析器的本机对照，不是生产延迟承诺。

- 沙箱与宿主程序查找回归：55 项通过，2.294 秒。
- 完整内核/SDK 回归：3,318 项通过，207.427 秒。
- SDK/示例类型检查：18 个源文件通过。
- 修复后的双包从 sdist 重建，macOS 双版本 `pip check`、隔离安装冒烟通过；
  各 88 个已安装 Python 源文件与 wheel 内容一致。

当前候选制品为 `/tmp/xiaoyu-sdk-platform-cachefix-20261001`；全量日志为
`/tmp/xiaoyu-sdk-platform-cachefix-full.log`。此前四组 macOS/Linux 一小时、10,000
轮持续负载均通过所有资源门禁，但对应缓存修复前的源码；它们保留为旧候选证据。
修复后的持续与重复批次以新冻结快照另行执行，结果见机器可读记录及可靠性矩阵。


### 缓存修复候选的同组平台补验（历史批次）

以下均针对当时的 `cachefix` 运行时快照；本机安装包隔离冒烟、`pip check` 和动态
MCP/任务编排示例通过。完整源码回归为 3,318 项，不能与各平台的定向子集相加。

| 本机环境 | 同组 SDK / 协议 / CLI / 缓存及报告回归 |
|---|---|
| macOS ARM64 / Python 3.11.15 | 281 项通过；27.351 秒 |
| Linux ARM64 / Python 3.11.15 | 281 项通过；31.759 秒 |
| Linux ARM64 / Python 3.14.3 | 281 项通过；33.411 秒 |
| Windows 11 ARM64 / Python 3.11.15 | 281 项通过；80.403 秒 |
| Windows 11 ARM64 / Python 3.14.3 | 281 项通过；78.573 秒 |

macOS Python 3.14.3 使用上述全量回归。macOS 3.11 首次同组补验的四个 CLI
快照失败是测试父/子进程源码加载路径不一致，统一 `PYTHONPATH` 后复跑通过；
首次日志保留在 `/tmp/xiaoyu-sdk-macos311-cachefix-contracts.log`，复测日志为
`/tmp/xiaoyu-sdk-macos311-cachefix-contracts-recheck.log`。没有修改快照或放宽断言。

六组重复故障、生命周期、慢消费者与持续负载的最终结论以
[可靠性报告](sdk-reliability.md) 和 [批次清单](sdk-reliability-local-results.json) 为准。
这些工作区候选尚未发布，不包含在已发布的 PyPI 0.58.0 中。


### SQLite 并发写入修复（`sqlfix` 历史批次）

补查旧 Windows x64 生命周期失败时，用仅记录底层异常的包装复现了
`sqlite3.OperationalError: database is locked`；原生 ARM64 同负载也复现，不能归因
为仿真环境。两次诊断分别在 76.026 / 44.814 秒失败，原始异常链与快照均保留。

同一 SQLite adapter 的写事务现按 FIFO 排队，排队等待受现有 `timeout` 限制，
SQLite 锁等待与 `FULL` 同步强度保持不变。不同 adapter/进程仍使用 SQLite 的锁
机制。日志另保留首次写入错误链，避免后续“writer unavailable”掩盖原始原因。
三个新回归覆盖并发记录完整性、等待超时后的继续写入、原始错误链保留。

修复后，Windows 原生 ARM64 的同负载诊断在 171.205 秒通过：内存/SQLite 各
1,000 会话 × 10 轮、8 路并发，另各有三次 20 会话串行基线；FD/句柄、子进程和
SDK 线程清理门禁通过。原始 x64 环境的同负载修复复验也在 242.278 秒通过，所有清理门禁通过。
这两次诊断有异常记录包装，正式矩阵使用未包装的冻结源码。

| `sqlfix` 阶段候选验证 | 结果 |
|---|---|
| 完整项目环境全量 unittest | 3,321 项通过；255.982 秒；无跳过 |
| SDK/示例 mypy | 18 个文件通过 |
| macOS ARM64 / Python 3.11.15 同组回归 | 284 项通过；28.806 秒 |
| Linux ARM64 / Python 3.11.15、3.14.3 | 各 284 项通过；30.324 / 29.331 秒 |
| Windows 11 ARM64 / Python 3.11.15、3.14.3 | 各 284 项通过；57.559 / 54.160 秒 |
| 六组安装包依赖、隔离冒烟和编排/MCP 示例 | 通过；macOS 双版本各 88 个已安装源文件与 wheel 哈希一致 |

一次全量命令误用了仅安装 SDK 依赖的 wheel 环境，缺少 rich/prompt_toolkit 等
测试依赖，导致 1 个失败、5 个错误、228 个跳过；已更正为项目完整环境后复跑。
日志 `/tmp/xiaoyu-sdk-platform-sqlfix-full.log` 保留，成功复测为
`/tmp/xiaoyu-sdk-platform-sqlfix-full-recheck.log`，断言和门禁未改。
一次构建命令使用缺少 setuptools 的系统解释器，改用项目构建环境后成功；
失败与复测日志同样保留。

最终制品为 `/tmp/xiaoyu-sdk-platform-sqlfix-20261001`：两个 wheel 均从 sdist 重建，
`release-manifest.json` 保存哈希。构建脚本只保留 wheel，不保留临时 sdist。
这批制品尚未发布。此前 `cachefix` 重复矩阵因该存储修复中断，部分试验和中断原因
保留；旧持续批次按其原源码记录。最终六组正式重复/持续结果见可靠性报告。


### SQLite 初始化协调补验（最终 `sqlinit` 源码）

后续 Linux 3.11 生命周期压力在 `PRAGMA synchronous=FULL` 处发现另一个锁竞争
窗口：连接初始化尚未进入排队区。Linux 3.14 同批通过，两个原始结果均保留。
最终版本将初始化、读取及列表查询也纳入同一 adapter 的协调范围；超时和同步
持久化设置不变。新增实锁用例覆盖排队超时、失败初始化的租约释放与后续继续使用。

- 最终源码全量：3,322 项通过，241.163 秒，无跳过。
- 存储定向：15 项通过；SDK/示例 mypy：18 个文件通过。
- macOS Python 3.11 同组：285 项通过，29.580 秒。
- Linux Python 3.11 / 3.14 同组：各 285 项通过，29.069 / 30.513 秒。
- Windows 11 ARM64 Python 3.11 / 3.14 同组：各 285 项通过，71.741 / 50.480 秒。
- macOS、Linux 双版本 R12/R14/R15/R16 各 100 次重复与两种模式生命周期负载通过。

最终制品为 `/tmp/xiaoyu-sdk-platform-sqlinit-20261001`，从 sdist 重建的两个 wheel
及哈希清单在该目录；全量日志为 `/tmp/xiaoyu-sdk-platform-sqlinit-full.log`。
最终版本相对上一 `sqlfix` 候选只改动运行时 `storage.py`。持续与传输批次保留自身
快照；存储改动通过最终全量、相关重复与六组生命周期补验验证，各组完成状态见
可靠性报告。不能把前一快照的持续测试说成最终完整源码的一小时验收。

### Windows 生命周期采样修正

Windows 3.11 最终存储批次已完成两种模式各 1,000 会话 × 10 轮，但内存模式
句柄从 195 增至 208，未通过原来的增长不超过 2 个门禁。所有会话返回正确，
无 SDK 线程或子进程残留；原失败报告仍保留。

独立对照证明测试脚本保留了已关闭线程池：不调用 SDK 的 8 工作线程池使句柄
从 199 增至 211，等待五秒不下降，释放线程池引用后即回到 199。SDK 诊断中
同样释放引用后从 213 降至 202；该诊断相对自身 199 基线仍增长 3，不能作为
正式通过结果。修正脚本在最终采样前释放自己的线程池，并以新的
`windows{311,314}arm-handles-lifecycle` 批次按原负载、原阈值复验；结果见可靠性
报告。SDK 运行时代码未改，无需重建制品。

新增行为回归通过弱引用确认两种存储模式的最终资源采样不再保留脚本线程池。
脚本 3 项测试通过，0.052 秒；这是最后一次全量 3,322 项之后的定向验证，不与
全量结果相加。Windows 3.11/3.14 的同组脚本 3 项回归也分别通过。
控制实验和脚本版本均包含在证据包中。

修正采样后，两组 Windows 同负载正式复验均通过：3.11 用时 197.406 秒，3.14
用时 156.016 秒，所有清理门禁通过。原失败保留，尚未启动的旧快照生命周期
阶段记录为 `superseded_before_start`，引用最终源码的同配置通过批次；不计作
另外一次通过。跨平台契约和慢消费者的既定批次均已结束，具体结果见可靠性报告。

队列替代记录脚本曾把 Windows 自动生成的 108 个 `__pycache__` 文件计入源码
总数，触发 200/92 断言；改为按上游冻结清单的 92 个源文件逐个核对记录及实际
文件哈希后补记成功。此次只影响记录步骤，未启动旧批次，原脚本错误日志保留。

### 本地批次收尾

最终核对 36 个选定批次：32 个通过，4 个 Windows 契约/MCP 批次仅因 POSIX
专用用例跳过而保持 `blocked`，未发现实际失败。这里采用最终版本的存储补验，
历史 Linux 初始化失败、Windows 采样失败及修复过程仍保留在原报告中。
六组一小时持续负载各完成 10,000 轮和 100 次压缩；六组慢消费者三种容量全部
通过。Windows 3.11/3.14 的慢消费者整批分别耗时 753.232 / 679.110 秒。

92 个运行时文件与最终快照一致；新增脚本的定向回归、工作流 YAML 和补丁格式
检查通过。完整 P4-R 仍缺 R13 真实模型长测、主部署环境 24 小时混合负载及
已列的平台分支/远端验证，不以这些本地结果替代。

测试专用 Windows 临时目录与宿主共享暂存目录已清理，归档前逐个验证了 4,707
个文件的复制哈希。既有 Windows 虚拟机已恢复为任务开始前的关闭状态，空闲暂停
设置恢复为开启；未删除虚拟机。清理记录也包含在本地证据包中。
