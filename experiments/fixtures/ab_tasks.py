"""同题对照的任务清单：本仓可复现的题目。task_ab.py 读这里，另可用 --tasks-file 加本机私有题。

每题的起点是本仓某个提交导出的一份**不带历史**的副本（另起一个只有一次提交的 git 仓）：
答案就在本仓的后续提交里，留着历史等于把答案放在手边。

- 定位问答（locate）：只读。判据是收尾正文里有没有那几个事实，外加工作区没被动过。
- 单文件修改（edit）：给一句话需求。判据是跑完后拷进去的验收用例（题目里不出现）。
- 多文件功能（feature）：题面是一份小规格，改动跨几个文件。判据同上，是题面里不出现的验收用例。
- 写文档 / 报告（doc）：交一个文件。判据是文件在、里面有那几个事实（要读代码或算数才写得出），
  且没有动范围之外的东西。事实对了不等于文档写得好，文字质量另看产物。
- 照失败输出排障（debug）：把一次真实修复的源码改动退回去、测试留着，题面就是
  失败输出本身。判据是那组测试通过，且测试文件没被改。

题面里的 {python} 换成跑测试用的解释器，{failure} 换成起点处真实跑出来的失败输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CHECKS = Path(__file__).resolve().parent / "ab_checks"
SEEDS = Path(__file__).resolve().parent / "ab_seed"
TEST = "PYTHONPATH=$PWD {python} -P -m unittest"


@dataclass
class Task:
    name: str
    category: str                      # locate / edit / debug / feature / doc
    prompt: str
    ref: str = ""                      # 导出本仓的哪个提交作起点
    cwd: str = ""                      # 或：直接在这个真实目录里跑（只给只读题用）
    restore: list[tuple[str, str]] = field(default_factory=list)   # (提交, 路径)：把文件退回那个提交的样子
    tests: list[str] = field(default_factory=list)                 # 验收时跑的 unittest 模块
    hidden: list[str] = field(default_factory=list)                # 验收前拷进 tests/ 的用例文件（ab_checks/ 下）
    frozen: list[str] = field(default_factory=list)                # 不许动的路径（相对起点提交）
    expect: list[str] = field(default_factory=list)                # 收尾正文里每条正则都要命中
    readonly: bool = False             # 工作区不该有任何改动
    seed: list[tuple[str, str]] = field(default_factory=list)      # (ab_seed/ 下的文件, 工作区里的路径)：起点前拷进去
    artifact: str = ""                 # 要交出的文件；给了它，expect 就对着这个文件判，不对着收尾正文
    scope: list[str] = field(default_factory=list)                 # 只许动这些路径（前缀）


TASKS = [
    Task(
        name="locate-wait-stall", category="locate", ref="30fe7b4", readonly=True,
        prompt="这个项目里的「空等检测」：一条 bash 命令里 sleep 合计至少多少秒才算等待？"
               "等完结果没变，第几次开始提示、第几次要求收敛？比对的是输出里的哪一行？只回答，不要改任何文件。",
        expect=[r"10\s*秒|10s|>=\s*10|≥\s*10", r"第?\s*[2二两]\s*次", r"第?\s*[3三]\s*次", r"12"],
    ),
    Task(
        name="edit-inspect-tokens", category="edit", ref="30fe7b4",
        prompt="`xiaoyu sessions inspect` 的 request 行摘要现在不带 token 数。请让它在记录里有 "
               "prompt_tokens / completion_tokens 时也按 `键=值` 的写法显示出来（放在现有那几项之后），"
               "并在 tests/test_session_inspect.py 里补用例。\n跑测试：" + TEST + " tests.test_session_inspect",
        tests=["tests.test_session_inspect", "tests.test_ab_inspect_tokens"],
        hidden=["test_ab_inspect_tokens.py"],
    ),
    Task(
        name="edit-compare-turn", category="edit", ref="0ed2286",
        prompt="给 experiments/session_compare.py 加一个 `--turn N` 选项（N 从 1 起）：只保留第 N 段用户输入，"
               "以及它之后、下一段用户输入之前的步骤；汇总表也只算这一段，时间仍以这一段的用户输入为 0 秒。"
               "N 超出范围时报错并以非零退出码结束。在 tests/test_session_compare.py 里补用例。\n"
               "跑测试：" + TEST + " tests.test_session_compare",
        tests=["tests.test_session_compare", "tests.test_ab_compare_turn"],
        hidden=["test_ab_compare_turn.py"],
    ),
    Task(
        name="debug-wait-tail", category="debug", ref="0e7b23a",
        restore=[("0e7b23a^", "xiaoyu/agent.py")],
        prompt="跑 `" + TEST + " tests.test_progress_guards` 挂了，输出如下。测试是对的，请修代码。\n\n{failure}",
        tests=["tests.test_progress_guards"], frozen=["tests"],
    ),
    Task(
        name="debug-explore-defaults", category="debug", ref="afbcfe7",
        restore=[("afbcfe7^", "xiaoyu/agent.py"), ("afbcfe7^", "xiaoyu/config.py"),
                 ("afbcfe7^", "xiaoyu/tools.py")],
        prompt="跑 `" + TEST + " tests.test_budget_pacing tests.test_user_config` 挂了，输出如下。"
               "测试是对的，请修代码。\n\n{failure}",
        tests=["tests.test_budget_pacing", "tests.test_user_config"], frozen=["tests"],
    ),
    Task(
        name="feature-explore-trace", category="feature", ref="f4f4474^",
        prompt="检索子 agent（explore）不落盘，父会话日志里只剩问题与结论，事后看不出一次检索每一步"
               "做了什么。请把它的逐步事实记进**父会话**的日志（`session_log.event(种类, **字段)`）：\n"
               "- 开始记一条 `explore_start`（字段 question）。\n"
               "- 子 agent 每次模型请求记一条 `explore_request`：step（从 1 起）、prompt_tokens、"
               "completion_tokens。\n"
               "- 每次工具调用记一条 `explore_tool`：step（所属请求的序号）、tool（工具名）、ok（成败）。"
               "工具输出的正文不许进日志。\n"
               "- 收尾记一条 `explore_end`：outcome（done / interrupted / error）、requests、tools、"
               "failed_tools。用户中断（KeyboardInterrupt 照常往上抛）和子 agent 出错时也要写。\n"
               "- 同一次检索的所有记录带同一个 run 字段。\n"
               "- 会话日志可能在 agent 建好之后才挂上或被换掉，用检索当时的那个；没有日志时照常检索；"
               "写日志出错不能连累检索。\n"
               "- `sessions inspect`：`kinds=(\"explore\",)` 能筛出这一整组；`errors_only` 要包含失败的"
               "工具调用、带 error 字段的请求、outcome 不是 done 的收尾；这些请求不计入父会话的请求数。\n"
               "补上用例，并在 docs/session-inspection.md 里写一段说明。\n"
               "跑测试：" + TEST + " tests.test_session_inspect tests.test_subagent_knobs",
        tests=["tests.test_session_inspect", "tests.test_subagent_knobs", "tests.test_ab_explore_trace"],
        hidden=["test_ab_explore_trace.py"],
    ),
    Task(
        name="feature-delegated-rows", category="feature", ref="81c0939",
        prompt="experiments/session_compare.py 的汇总只算了主链的请求。小羽会话日志里检索子 agent 另发的"
               "请求是 `explore_request` 事件（带 prompt_tokens / completion_tokens），现在被漏掉了。"
               "请把它们单列出来，不要并进主链各项：\n"
               "- `summarize()` 多三个键：delegated_requests、delegated_context_total、"
               "delegated_output_total（没有子 agent 时都是 0）。\n"
               "- 单场会话的汇总表加一行，标题里带「子 agent」，值写成 `请求数 / 输入 token`"
               "（数字带千分位）。\n"
               "- experiments/task_ab.py 的跑批汇总表也加上子 agent 的请求数与输入 token 合计"
               "（标题里带「子 agent」，取各次运行的中位）。\n"
               "两个脚本的测试都补上用例，脚本开头的口径说明也补一句。\n"
               "跑测试：" + TEST + " tests.test_session_compare tests.test_task_ab",
        tests=["tests.test_session_compare", "tests.test_task_ab", "tests.test_ab_delegated_rows"],
        hidden=["test_ab_delegated_rows.py"],
    ),
    Task(
        name="doc-wait-stall", category="doc", ref="30fe7b4",
        prompt="给用户写一篇说明文档 docs/wait-stall.md，讲这个项目的「空等检测」：它在防什么、"
               "什么样的命令算一次等待、怎么判断「结果没变」、第几次提示、第几次要求收敛、"
               "提示出现后用户会看到什么。数字以代码为准，不要凭印象写。只新建这一个文件，不改代码。",
        artifact="docs/wait-stall.md", scope=["docs/wait-stall.md"],
        expect=[r"10\s*秒|10s|>=\s*10|≥\s*10", r"第?\s*[2二两]\s*次", r"第?\s*[3三]\s*次", r"12",
                r"最后一行|末尾.{0,6}行|末行"],
    ),
    Task(
        name="doc-batch-report", category="doc", ref="81c0939",
        seed=[("results.json", "data/results.json")],
        prompt="data/results.json 是一批对照运行的原始结果（两道题、三列、每格 5 次）。请写一份报告 "
               "report.md 给没看过数据的人：一张表列出每题每列的办成次数和耗时中位数（秒），"
               "再用几句话说清哪一列超时最多、一共超时几次、全批办成了多少次。数字要算出来，不要估。"
               "只新建 report.md，不改别的文件。",
        artifact="report.md", scope=["report.md"],
        expect=[r"\b47\b", r"\b63\b", r"\b131\b", r"\b29\b", r"\b211\b", r"\b395\b",
                r"delta", r"23\s*/\s*30|23\s*次", r"\b3\s*次|共\s*3\b|3\s*runs"],
    ),
]
