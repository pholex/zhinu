# 多 agent 协同：声明式 subagent · 七襄 · 宸枢

小羽提供三种多 agent 织造模式：

- **七襄 · 并行织造模式**（Qixiang · Parallel-Weave Mode）：召集多名织手
  横向并行，各织各的纬线——适合大量互不依赖的子任务。
- **宸枢 · 统筹织造模式**（Chenshu · Sovereign-Weave Mode）：总枢坐镇其上，
  专职规划、分派、监督、汇总织手的产出——适合层层有序推进的巨型工程。
- **斗巧 · 竞争织造模式**（Douqiao · Contest-Weave Mode）：源起七夕斗巧
  之俗。令多名织手互不相通，独立织造同一幅锦段；待各方完工，比对工巧
  优劣，选取最优成果——以多重织造的冗余投入，换取代码天章质量上限。

加上底层的声明式 subagent，四层能力各司其职：

| 层 | 形态 | 换来什么 | 适合 |
|---|---|---|---|
| 声明式 subagent | 一个 TOML = 一个可委托的子 agent | 省上下文 | 单个独立子任务 |
| **七襄** · 并行织造 | N 个任务各跑一次 | 产能 | 批量迁移 / 批量审查 / 批量调研 |
| **宸枢** · 统筹织造 | mission 分区 + 评审 + 合并 | 秩序 | 跨子系统的大工程 |
| **斗巧** · 竞争织造 | 一个任务跑 N 次，判官择优 | 质量上限 | 架构方案 / API 定稿 / 硬 bug |

命名典故：七襄出自《诗经·小雅·大东》"跂彼织女，终日七襄"——织女星一日
七次移位，喻多路并行轮转（原诗"虽则七襄，不成报章"是反讽，小羽的七襄
把 report 织出来）；宸枢=帝居之枢，编排总控坐镇其上；斗巧=七夕乞巧节
竞赛织艺之俗，为织女而比试工巧。

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
effort = "low"                    # 可省：推理深度，默认随主会话（只读探索给 low 省钱）
max_iterations = 30               # 可省：默认 20
inherit = "distilled"             # 可省：none（默认）/ distilled（精简副本）/ fork（完整上下文）
```

要点（详见 `xiaoyu/agents.py` 模块说明）：

- **权限不因声明放大**：只读子集免确认；写/执行/MCP 复用父会话的审批与
  deny 规则——工作区级 spec 是安全的，clone 一个仓库不会静默多出放行。
- **worktree 隔离**：`isolation = "worktree"` 时改动落在独立 git worktree，
  跑完没改动自动删、有改动保留并把路径写进结论（`git -C <路径> diff` 查看，
  `git apply` 取回）。
- **resume 续跑**：每次委托的结论尾部有 `resume_from` 句柄，带上它就在
  那次委托的完整上下文上继续（多阶段委托的正确姿势）。
- **精简继承**：`inherit = "distilled"` 时子 agent 以父会话的精简副本起步
  ——只有用户原话与每轮最终答复，工具过程、推理、压缩摘要都不带，按子
  窗口的 30% 从最新一轮往回整轮装。适合"接着聊的那件事去办"的委托：子
  agent 拿到用户原意而不是父 agent 的转述。只作用于新开委托；七襄/斗巧
  的批量扇出不带（扇出项应自足）。
- **完整继承（fork）**：`inherit = "fork"` 时子 agent 逐字带走父会话的**完整**
  上下文（工具过程、结果、推理都在，故名 fork）。要精确接着父会话
  干、细节不能丢时用；代价是可能撑爆子窗口（子 agent 首轮自动压缩兜底）。
- **嵌套深度**：默认**不套娃**——子 agent 不能再派子 agent（单写者纪律）。
  `XIAOYU_SUBAGENT_MAX_DEPTH`（默认 1）显式放开有界嵌套：设 2/3 时子 agent
  可再委托，逐层 +1、到顶即止，不会失控递归。宸枢多层编排才需要。

`XIAOYU_ENABLE_AGENTS=0` 一键关闭。

## 七襄：并行织造模式（Parallel-Weave Mode）

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

## 斗巧：竞争织造模式（Contest-Weave Mode）

有可委托的 spec 时自动出现 `douqiao` 工具。相同任务、独立作战、多方
方案比拼、择优选用：

```
douqiao(
  spec="architect",
  task="为 X 模块设计缓存失效策略，给出完整方案与取舍理由",
  models=["deepseek-v4-pro", "kimi-k3", "claude-sonnet-5"],   # 异构竞争，每模型一席
  criteria="正确性优先；其次是实现复杂度"                       # 可省
)
```

- **严格隔离**：席位之间互不相通（各自独立上下文；写型 spec 每席独立
  worktree，建不出来该席弃权、绝不退回主工作区）——互通会让多样性塌缩
  成趋同。
- **判官制、赢者全拿**：全部完工后由判官（只读委托，`judge_model` 可
  指定，建议用最强模型）逐席评估、横向比对、裁决胜者；不做方案合成，
  败者亮点以"值得胜者吸收"的形式列出。判官中途失败或裁决解析不出都
  不作废比赛——各席成果与 resume 句柄照常返回，自行定夺。
- **异构模型竞争**：`models` 让不同厂商模型各织一匹——多样性来自模型
  本身，结构性优于同一模型重采样 N 次（错法都一样）。省略则各席随
  spec/主模型。
- **成本明码**：2–6 席（默认 3），N 倍投入买质量**上限**而非均值——
  只用于值得的任务；并发与单席超时沿用七襄的旋钮。

## 宸枢：统筹织造模式（Sovereign-Weave Mode）

`chenshu_init` 启动（需要 git 仓库且至少一个 commit）。主 agent 化身唯一
的编排者（总枢），工作流：

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
  （总枢是协调者不是内容中继）；scope 外的发现用 `chenshu_finding` 归档，
  由总枢分派——**发现不等于授权**，worker 越出自己 worktree 的写操作会被
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
- 一批"同一个模板、互不依赖"的任务 → 七襄（拼产能）。
- 一个"值得为质量上限付 N 倍钱"的任务 → 斗巧（拼质量）。
- 要隔离、要评审、要按依赖顺序合并的大工程 → 宸枢（拼秩序）。
- 宸枢的 worker 内部不能再开七襄/宸枢/斗巧（刻意不套娃）；七襄与斗巧
  的每一席都是一次普通委托，享受同一套审批与隔离语义。
