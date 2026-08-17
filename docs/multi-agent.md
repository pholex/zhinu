# 多 agent 协同：声明式 subagent · 七襄 · 宸枢

小羽的多 agent 能力分三层，动态性递减、编排能力递增：

| 层 | 形态 | 适合 |
|---|---|---|
| 声明式 subagent | 一个 TOML = 一个可委托的子 agent | 单个独立子任务 |
| **七襄**（qixiang） | 批量并行委托：一个模板 × N 份材料 | 批量迁移 / 批量审查 / 批量调研 |
| **宸枢**（chenshu） | 编排总控：mission 分区 + 评审 + 合并 | 跨子系统的大工程 |

命名典故：七襄出自《诗经·小雅·大东》"跂彼织女，终日七襄"——织女星一日
七次移位，喻多路并行轮转（原诗"虽则七襄，不成报章"是反讽，小羽的七襄
把 report 织出来）；宸枢=帝居之枢，编排总控坐镇其上。

## 声明式 subagent（`agents/*.toml`）

放一个 TOML 就多一个可委托的子 agent，不用写代码：

```toml
# <用户配置目录>/agents/tester.toml 或 <工作区>/.xiaoyu/agents/tester.toml
description = "写并跑单元测试的子 agent"     # 必填：也是给模型看的工具说明
system_prompt = "你是测试工程师……工作区根目录：{workspace}"   # 必填
tools = ["read_file", "grep", "list_files", "write_file", "bash"]
capability_mode = "read-write"    # 可省：粗粒度档位，与 tools 二选一或叠加
isolation = "worktree"            # 可省：默认在独立 git worktree 里跑
mcp = ["github"]                  # 可省：继承父会话的哪些 MCP server
model = "deepseek-v4-pro"         # 可省：默认随主模型
max_iterations = 30               # 可省：默认 20
```

要点（详见 `xiaoyu/agents.py` 模块说明）：

- **权限不因声明放大**：只读子集免确认；写/执行/MCP 复用父会话的审批与
  deny 规则——工作区级 spec 是安全的，clone 一个仓库不会静默多出放行。
- **worktree 隔离**：`isolation = "worktree"` 时改动落在独立 git worktree，
  跑完没改动自动删、有改动保留并把路径写进结论（`git -C <路径> diff` 查看，
  `git apply` 取回）。
- **resume 续跑**：每次委托的结论尾部有 `resume_from` 句柄，带上它就在
  那次委托的完整上下文上继续（多阶段委托的正确姿势）。

`XIAOYU_ENABLE_AGENTS=0` 一键关闭。

## 七襄：批量并行委托

有可委托的 spec 时自动出现 `qixiang` 工具。模型（或你在指令里点名）用它
把**同构且互不依赖**的一批子任务扇出给同一个 spec 并行执行：

```
qixiang(
  spec="tester",
  prompt_template="给 {{item}} 补单元测试，跑通后报告覆盖的分支",
  items=["src/auth.py", "src/routes.py", "src/db.py", …]   # 最多 64 项
)
```

- **并行**：默认并发 4（`XIAOYU_QIXIANG_CONCURRENCY` 调节，1–16），
  首波错峰起步；单项可设墙钟超时（`XIAOYU_QIXIANG_TIMEOUT`，从实际启动
  起算，排队不计）。
- **隔离**：非只读 spec 每项默认跑在独立 worktree 里——并行写物理不冲突；
  确认各项互不相交且要直接落主工作区时传 `isolation="none"`。
- **report**：全部收束后按**输入顺序**聚合（完成/失败/中止逐项列明，每项
  带结论与 `resume_from` 句柄）。失败、超时、甚至 Ctrl-C 打断都不白跑——
  已完成的存档还在，`resume` 参数批量续跑：
  `qixiang(spec="tester", resume={"ab12cd34": "接着修剩下的用例"})`。
- **质量闸**：子 agent 结论短于 200 字符会被自动追问一轮，逼出完整交接。
- 任务之间有依赖或要共享中间结果时**不要用七襄**——改为顺序委托或上宸枢。

## 宸枢：编排总控模式

`chenshu_init` 启动（需要 git 仓库且至少一个 commit）。主 agent 化身唯一
的编排者（塔），工作流：

1. **chenshu_plan** 把目标拆成 mission：`build` 必须给 `scope`（目录/glob，
   **两两不相交**，共享文件归属唯一一个 mission）；`survey` 是只读调研；
   `deps` 声明合并顺序。
2. **chenshu_spawn** 逐个起成员：worker 绑 mission（build 自动创建
   `feat/<slug>` 分支 + 独立 worktree），reviewer 绑评审目标。把依赖已
   解锁的 mission 一口气发满（上限 `XIAOYU_CHENSHU_MAX_WORKERS`，默认 4）。
3. **chenshu_wait** 阻塞等成员事件（完成/失败，最长 600s）——不轮询。
4. 评审过闸后 **chenshu_merge** 收回主干。
5. 全部合并后 **chenshu_teardown** 收枢（干净 worktree 删除，审计轨迹
   永久保留在 `.xiaoyu/chenshu/`）。

协议由代码而非提示词强制：

- **通信**：成员之间 `chenshu_send` / `chenshu_inbox` 点对点或广播直连
  （塔是协调者不是内容中继）；scope 外的发现用 `chenshu_finding` 归档，
  由塔分派——**发现不等于授权**，worker 越出自己 worktree 的写操作会被
  审批层直接拒绝。
- **merge 五道闸**：deps 已合 → 有评审且最新一轮 `clean` → 评审盖的
  commit 等于分支当前 tip（**分支一动 clean 自动作废**）→ diff 文件全部
  命中 mission scope → 主 checkout 停在 base 分支。被拒的合并也记审计
  日志——拒绝是一个带理由的决策。
- **审计**：所有协作产物（消息/发现/评审/mission 状态/活动日志）是
  `.xiaoyu/chenshu/` 下的明文 markdown + JSON，永久保留、随时可查。

重启后重新 `chenshu_init` 会**收养**既有工作区：mission、worktree、审计
轨迹全保留，上个会话的成员退役，重新 spawn 即可接着干。

已知边界（诚实记录）：worker 的 bash 不做命令级审查（macOS 有 Seatbelt
沙箱兜写越界，其它平台靠 briefing 纪律）；reviewer/survey 的只读性是
工具集级的（没有写工具），reviewer 的 bash 同样只有纪律约束。

`XIAOYU_ENABLE_CHENSHU=0` 一键关闭。

## 怎么选

- 一个独立子任务 → 直接调 spec 工具（或让模型自己委托）。
- 一批"同一个模板、互不依赖"的任务 → 七襄。
- 要隔离、要评审、要按依赖顺序合并的大工程 → 宸枢。
- 宸枢的 worker 内部不能再开七襄/宸枢（刻意不套娃）；七襄的每一项就是
  一次普通委托，享受同一套审批与隔离语义。
