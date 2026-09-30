# SDK 0.58.0 本机验收记录

日期：2026-09-30。环境：macOS 27.0 arm64，Python 3.14.3。
源码为本次工作区修改，未提交、未推送、未上传 PyPI。

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
新工作流已提供跨平台验证，但本机没有把它记作已通过。
资源限制与 SDK 契约见 [SDK 指南](sdk.md)。
