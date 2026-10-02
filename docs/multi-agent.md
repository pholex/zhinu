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
model = "deepseek-flash"          # 可省：默认随主模型
effort = "low"                    # 可省：推理深度，默认随主会话（只读探索给 low 省钱）
max_iterations = 30               # 可省：默认 20
inherit = "distilled"             # 可省：none（默认）/ distilled（精简副本）/ fork（完整上下文）
```

要点（详见 `xiaoyu/agents.py` 模块说明）：

- **权限不因声明放大**：只读子集免确认；写/执行/MCP 复用父会话的审批与
  deny 规则——工作区级 spec 是安全的，clone 一个仓库不会静默多出放行。
  子 agent 逐工具的确认**跟发起委托的那个会话同一档**（你在确认档，它的
  写文件、跑命令也逐条问；你在 auto 档，它同样只放行沙箱兜得住的那部分），
  你挂在工具调用上的钩子（`PreToolUse` / `PostToolUse` / `ToolFailed`）在子 agent 里照样触发
  （会话类的 `SessionStart` / `SessionEnd` / `UserPromptSubmit` / `Stop` 不带下去）。
  检索子 agent（explore）读文件同样过你的 deny / ask 规则。
- **worktree 隔离**：`isolation = "worktree"` 时改动落在独立 git worktree，
  跑完没改动自动删、有改动保留并把路径写进结论（`git -C <路径> diff` 查看，
  `git apply` 取回）。
- **resume 续跑**：每次委托的结论尾部有 `resume_from` 句柄，带上它就在
  那次委托的完整上下文上继续（多阶段委托的正确姿势）。存档同时落在会话日志
  旁的 `<日志名>.runs/` 里（仅本人可读，每场会话滚动保留最近 64 份），进程重启
  或 `xiaoyu resume` 之后句柄照样接得上；没有会话日志时只在内存里。
  上次在 worktree 里跑的，续跑沿用隔离：目录还在就复用，已回收或丢失就
  重建一个（丢失时其中未提交的改动不保留，结论里会写明）。
  单发委托跑到一半被 Ctrl-C 打断时也一样：存档照落、有改动的 worktree 留着，
  句柄与路径写进这次调用的结果里。
- **结论过长不丢后半截**：结论留在父上下文里的量有上限（约 4000 字符），超出的
  部分落盘、内联留头尾预览和召回 id，需要中段时用 `recall` 取——检索（explore）
  与联网搜索的结论同理。
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
- **report**：全部收束后按**输入顺序**聚合（完成/未做完/失败/中止逐项列明，每项
  带结论与 `resume_from` 句柄）。失败、超时、甚至 Ctrl-C 打断都不白跑——
  已完成的存档还在，`resume` 参数批量续跑：
  `qixiang(spec="tester", resume={"ab12cd34": "接着修剩下的用例"})`。
  续跑项与新开项同一条底线：隔离建不出来该项就不执行，不会退回主工作区
  并行写。
- **质量闸**：子 agent 结论短于 200 字符会被自动追问一轮，逼出完整交接。
- **未做完**单列：撞轮数上限或 token 预算被叫停的项交的是进度不是结论，
  report 里不算完成、自动进续跑清单。单发委托与宸枢的成员事件同一口径
  （宸枢还会附上量出来的实况：mission 状态、分支上几个提交、有没有没提交的改动）。
- **模型与深度**：`model` / `effort` 给本批全部委托单独指定（缺省随 spec
  声明/主会话）——批量迁移、批量调研这类活用便宜模型、只读的给 `low`，
  主会话留着强模型做统筹。模型名开工前过 provider 校验，不认的名字整批
  不起；resume 项钉住上次的模型（上下文是按它长的），effort 照常可改。
- 任务之间有依赖或要共享中间结果时**不要用七襄**——改为顺序委托或上宸枢。

## 斗巧：竞争织造模式（Contest-Weave Mode）

有可委托的 spec 时自动出现 `douqiao` 工具。相同任务、独立作战、多方
方案比拼、择优选用：

```
douqiao(
  spec="architect",
  task="为 X 模块设计缓存失效策略，给出完整方案与取舍理由",
  models=["deepseek-flash", "kimi-k3", "claude-sonnet-5-5"],   # 异构竞争，每模型一席
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
   `model` / `effort` 可给成员单独定模型与推理深度：总枢用强模型规划，
   build worker 用便宜模型，survey / reviewer 给 `low`；缺省随主会话。
3. **chenshu_wait** 阻塞等成员事件（成员发给总枢的来信、完成/失败，按发生顺序，最长 600s）——不轮询。
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
- **存档**：成员收工时整份 transcript 落盘到 `.xiaoyu/chenshu/archives/`，
  `chenshu_spawn` 同名 `resume=true` 就在它的上下文上续跑（追加评审轮、
  接着修遗留），模型钉住存档那一个，要换模型请换名新开。

重启后重新 `chenshu_init` 会**收养**既有工作区：mission、worktree、审计
轨迹、存档全保留，上个会话的成员退役；有存档的成员 `resume=true` 接着
原上下文续跑，中途断掉没走到收工的直接重新 spawn。

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
