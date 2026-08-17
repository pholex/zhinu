"""七襄（qixiang）：并行织造模式（Parallel-Weave）——召集多名织手横向并行、
分区同步织造。

典出《诗经·小雅·大东》「跂彼织女，终日七襄」。把同构且互不依赖的一批
子任务（prompt_template + items 逐项展开）扇出给同一个声明式 subagent
spec 并行执行，全部收束后按**输入顺序**聚合成一份 report 带回主上下文。
适合批量迁移、批量审查、批量调研这类"一个模板、N 份材料"的活。

关键设计决策：
- 失败/中止也返回带 resume 句柄的完整报告——用户打断不白跑，任何时刻
  已完成的部分都可批量续跑；超时从**实际启动**起算，排队等槽位不计。
- 结果按输入顺序聚合（与完成先后无关），report 定长可读。
- 短结论追问一轮（min-summary 质量闸）：批量模式下父 agent 无法逐个便宜
  追问，收束前借 resume 机制把太短的交接补全，零新增通道。
- 并发用「上限式」（默认 4，XIAOYU_QIXIANG_CONCURRENCY 调节）+ 首波错峰
  起步：单机直连场景不做无上限扇出与 429 自适应容量治理——后者依赖统一
  的限流上报通道，等有真实需求再做。也不做「批量调用必须独占一轮响应」
  的限制：xiaoyu 的工具调用本就逐个串行执行，没有并发语义冲突要防。
- 非只读 spec **默认强制 worktree 隔离**（isolation="none" 可显式退出）：
  N 个写型子 agent 共享同一工作区只靠提示词划界是靠不住的，
  worktree-per-delegation 让并行写从"祈祷不冲突"变成"物理不可能冲突"。

并发安全依赖三处既有基建：Usage 记账内置锁、Registry 惰性建 client
内置锁、RunStore 存档锁（见 agents.execute_delegation）。审批回调用
锁串行化：并行的多个确认框一次只弹一个，其余工作线程排队等。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from . import ui
from .agents import (
    AgentSpec,
    DelegationResult,
    SubagentRun,
    _clean_param,
    execute_delegation,
)
from .config import Config
from .events import Notice, UISink
from .fanout import Attempt, run_attempts
from .tools import Tool

#  一批最多多少项（新建 + resume 合计）。单机直连场景 64 已远超实际并发
#  消化能力，再大的活该分批
MAX_ITEMS = 64
#  没有 resume 时至少两项：单个任务直接用同名 subagent 工具，不必过七襄
MIN_NEW_ITEMS = 2
#  短结论追问阈值（引擎默认值的本地绑定：测试可 patch 本模块的这个名字）
MIN_ANSWER_CHARS = 200
_CONTINUATION_TASK = (
    "你上一条结论太简短。父 agent 只能看到你的最后一条消息，它就是全部交接。"
    "请补全成完整交接：做了什么、动过哪些文件（给出路径）、怎么验证的、"
    "遗留事项或不确定点。"
)
_PLACEHOLDER = "{{item}}"
#  报告的总结论预算（字符）：按项数均分，下限保住可读性
_REPORT_BUDGET = 24_000
_PER_ITEM_FLOOR = 600


class _NullSink:
    """批量运行时子 agent 的静默 sink：N 个 worker 的工具刷屏毫无可读性，
    进度由七襄自己在主线程逐项播报。"""

    def emit(self, event: Any) -> None:  # noqa: ARG002
        pass


@dataclass
class _ItemState:
    """一项任务的全程状态（调度、超时、聚合共用）。"""

    index: int
    label: str
    task: str
    resume_from: str | None = None
    result: DelegationResult | None = None
    crash: str = ""  # runner 自身炸了（execute_delegation 之外的异常）
    timed_out: bool = False
    started_at: float | None = None
    never_started: bool = False


def _status_of(state: _ItemState, cancelled: bool) -> str:
    """收束分类（三态 + error）：
    error = 参数校验失败没执行；aborted = 超时/用户中止；failed = 执行异常。"""
    if state.crash:
        return "failed"
    if state.result is not None and state.result.error:
        return "error"
    if state.timed_out:
        return "aborted"
    if state.never_started or (cancelled and state.result is None):
        return "aborted"
    if state.result is not None and state.result.failure:
        return "aborted" if cancelled else "failed"
    if state.result is None:
        return "aborted"
    return "completed"


def make_qixiang_tool(
    specs: list[AgentSpec],
    config: Config,
    registry: Any,
    usage: Any,
    sink: UISink,
    approver: Any,
    permissions: Any,
    runs: dict[str, SubagentRun],
    mcp_manager: Any = None,
) -> Tool:
    """七襄工具：与 make_subagent_tool 同一套依赖（同一本账、同一存档）。"""
    spec_map = {spec.name: spec for spec in specs}

    #  审批串行化：多个 worker 同时要确认时一次只弹一个框
    approve_lock = threading.Lock()

    def locked_approver(name: str, args: dict[str, Any]) -> Any:
        with approve_lock:
            return approver(name, args)

    def run(
        spec: str,
        prompt_template: str | None = None,
        items: Any = None,
        resume: Any = None,
        capability_mode: str | None = None,
        isolation: str | None = None,
    ) -> str:
        #  ---------- 校验（全部在任何子 agent 启动之前：半途报错=白烧钱） ----------
        spec_name = _clean_param(spec)
        if spec_name is None or spec_name not in spec_map:
            return (
                f"ERROR: spec 必须是已加载的子 agent 名，可选：{', '.join(spec_map)}。"
            )
        target = spec_map[spec_name]

        item_list: list[str] = []
        if items is not None:
            if not isinstance(items, list) or not all(
                isinstance(item, str) for item in items
            ):
                return "ERROR: items 必须是字符串数组（每项展开成一个子任务）。"
            item_list = [item for item in items if item.strip()]

        resume_map: dict[str, str] = {}
        if resume is not None:
            if not isinstance(resume, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in resume.items()
            ):
                return "ERROR: resume 必须是 {resume_id: 续跑指令} 的对象。"
            resume_map = dict(resume)

        total = len(item_list) + len(resume_map)
        if total == 0:
            return "ERROR: items 与 resume 至少给一个。"
        if not resume_map and len(item_list) < MIN_NEW_ITEMS:
            return (
                f"ERROR: 纯新建至少 {MIN_NEW_ITEMS} 项——单个任务直接调 "
                f"{spec_name} 工具，不必过七襄。"
            )
        if total > MAX_ITEMS:
            return f"ERROR: 一批最多 {MAX_ITEMS} 项（当前 {total}），拆成多批。"
        if item_list:
            template = str(prompt_template or "")
            if _PLACEHOLDER not in template:
                return (
                    f"ERROR: 有 items 时 prompt_template 必填且必须包含 "
                    f"{_PLACEHOLDER} 占位符。"
                )
            expanded = [template.replace(_PLACEHOLDER, item) for item in item_list]
            seen: dict[str, int] = {}
            for index, prompt in enumerate(expanded):
                if prompt in seen:
                    return (
                        f"ERROR: 第 {seen[prompt] + 1} 与第 {index + 1} 项展开后的"
                        "任务完全相同——items 有重复或模板没用上 item。"
                    )
                seen[prompt] = index
        else:
            expanded = []

        #  隔离决策：显式参数优先；缺省时非只读 spec 强制 worktree（防并行写
        #  冲突——这是与单发工具语义的刻意分叉，见模块 docstring"反超"一节）
        iso_param = _clean_param(isolation)
        if iso_param is not None:
            iso_value = iso_param.lower().replace("_", "-")
            if iso_value == "work-tree":
                iso_value = "worktree"
            if iso_value not in ("none", "worktree"):
                return f"ERROR: isolation 只能是 none 或 worktree，不认识 {iso_param!r}。"
        else:
            iso_value = "none" if target.readonly else "worktree"

        #  ---------- 组装任务列表：resume 在前（续跑的活最急），编号连续 ----------
        states: list[_ItemState] = []
        for rid, prompt in resume_map.items():
            states.append(
                _ItemState(
                    index=len(states),
                    label=f"resume:{rid}",
                    task=prompt,
                    resume_from=rid,
                )
            )
        for item, prompt in zip(item_list, expanded):
            states.append(
                _ItemState(index=len(states), label=item, task=prompt)
            )

        concurrency = max(1, min(int(config.qixiang_concurrency), 16))
        timeout_s = max(0, int(config.qixiang_task_timeout))
        per_item_cap = max(_PER_ITEM_FLOOR, _REPORT_BUDGET // len(states))
        #  存档容量按批量抬高（本项 + 追问各占一格），报告里的 resume 句柄
        #  才不会在批内就被滚动淘汰成死链
        if hasattr(runs, "capacity"):
            runs.capacity = max(int(runs.capacity), 2 * len(states) + 8)

        #  并发调度收归共享引擎（fanout.py）：错峰/超时巡检/中止保全只写一处
        def make_primary(state: _ItemState) -> Any:
            def primary(register: Any) -> DelegationResult:
                return execute_delegation(
                    target, config, registry, usage, sink, locked_approver,
                    permissions, runs, mcp_manager,
                    task=state.task,
                    capability_mode=capability_mode,
                    #  resume 项沿用上次的 worktree，isolation 本就被忽略
                    isolation=None if state.resume_from else iso_value,
                    resume_from=state.resume_from,
                    child_sink=_NullSink(),
                    on_agent=register,
                    #  批量并行写不许退回主工作区（worktree 建不出来=该项不执行）
                    require_isolation=iso_value == "worktree",
                )

            return primary

        def follow_up(run_id: str, register: Any) -> DelegationResult:
            #  追问轮一律收紧到 read-only：它只要一份更完整的交接，不需要写；
            #  且首轮的干净 worktree 已被回收，resume 会落回主工作区——
            #  带着写工具续跑等于绕过「绝不退回主工作区并行写」的硬保证
            return execute_delegation(
                target, config, registry, usage, sink, locked_approver,
                permissions, runs, mcp_manager,
                task=_CONTINUATION_TASK,
                capability_mode="read-only",
                resume_from=run_id,
                child_sink=_NullSink(),
                on_agent=register,
            )

        attempts = [
            Attempt(index=state.index, primary=make_primary(state), follow_up=follow_up)
            for state in states
        ]

        def copy_back(state: _ItemState, attempt: Attempt) -> None:
            state.result = attempt.result
            state.crash = attempt.crash
            state.timed_out = attempt.timed_out
            state.never_started = attempt.never_started

        def on_settled(attempt: Attempt, settled: int, total_n: int) -> None:
            state = states[attempt.index]
            copy_back(state, attempt)
            status = _status_of(state, cancelled=False)
            mark = {"completed": "✓", "failed": "✗"}.get(status, "⊘")
            sink.emit(
                Notice(
                    f"  🕸 七襄 {settled}/{total_n} {mark} "
                    f"{ui.preview(state.label, 60)}"
                )
            )

        sink.emit(
            Notice(
                f"  🕸 七襄开工：{spec_name} × {len(states)} 项（并发 {concurrency}"
                + (f"，单项限时 {timeout_s}s" if timeout_s else "")
                + "）"
            )
        )
        run_attempts(
            attempts,
            concurrency=concurrency,
            timeout_s=timeout_s,
            min_answer_chars=MIN_ANSWER_CHARS,
            on_settled=on_settled,
        )
        for state, attempt in zip(states, attempts):
            copy_back(state, attempt)

        #  ---------- 聚合 report：输入顺序，与完成先后无关 ----------
        counts = {"completed": 0, "failed": 0, "aborted": 0, "error": 0}
        blocks: list[str] = []
        retry_ids: list[str] = []
        for state in states:
            status = _status_of(state, cancelled=False)
            counts[status] += 1
            zh = {
                "completed": "完成",
                "failed": "失败",
                "aborted": "中止",
                "error": "未执行",
            }[status]
            lines = [f"--- {state.index + 1}/{len(states)} {zh} · {ui.preview(state.label, 80)}"]
            result = state.result
            if result is not None and result.run_id:
                lines.append(f"resume_from: {result.run_id}")
                if status != "completed":
                    retry_ids.append(result.run_id)
            if result is not None and result.worktree is not None:
                lines.append(
                    f"worktree: {result.worktree}"
                    "（改动未落主工作区；git -C 该路径 diff 查看，确认后 git apply 取回）"
                )
            if state.crash:
                lines.append(f"ERROR: 执行线程异常（{state.crash}）")
            elif result is not None and result.error:
                lines.append(result.error)
            elif state.timed_out:
                lines.append(f"超时中止（>{timeout_s}s），已存档可续跑。")
            elif result is not None and result.failure:
                lines.append(f"ERROR: 子 agent 失败（{result.failure}）")
            elif result is None:
                lines.append("未开始即被中止。")
            if result is not None and result.answer and status in ("completed", "failed"):
                answer = result.answer
                if len(answer) > per_item_cap:
                    answer = answer[:per_item_cap] + "\n…（结论过长已截断；完整上下文在存档里，可 resume 追问）"
                lines.append(answer)
            for note in result.notes if result is not None else []:
                lines.append(note)
            blocks.append("\n".join(lines))

        header = (
            f"[七襄 report] spec={spec_name} · "
            f"完成 {counts['completed']} / 失败 {counts['failed']} / "
            f"中止 {counts['aborted']} / 未执行 {counts['error']}"
            f"（共 {len(states)} 项，并发 {concurrency}）"
        )
        hints: list[str] = []
        if retry_ids:
            resume_obj = ", ".join(f'"{rid}": "<续跑指令>"' for rid in retry_ids[:4])
            hints.append(
                "未完成的项可批量续跑：再调 qixiang，resume 传 "
                f"{{{resume_obj}{', …' if len(retry_ids) > 4 else ''}}}；"
                f"或用 {spec_name} 工具带 resume_from 单独续跑。"
            )
        sink.emit(
            Notice(
                f"  🕸 七襄收束：完成 {counts['completed']}/{len(states)}"
            )
        )
        return "\n\n".join([header, *hints, *blocks])

    spec_names = sorted(spec_map)
    return Tool(
        name="qixiang",
        description=(
            "七襄（并行织造模式）：把一批**同构且互不依赖**的子任务并行扇出给同一个子 agent，"
            "全部完成后返回按输入顺序聚合的 report（每项含结论与 resume 句柄）。"
            "用法：prompt_template 写共同任务模板（含 {{item}} 占位符），items "
            "逐项填充；resume 参数批量续跑之前的委托。适合批量迁移/批量审查/"
            "批量调研；任务之间有依赖或需要共享中间结果时不要用（改为顺序委托）。"
            "非只读 spec 每项默认在独立 git worktree 里跑，写操作互不冲突；"
            "拆分粒度尽量细——项数多不增加你的上下文负担，report 是定长聚合。"
            f"可选 spec：{', '.join(spec_names)}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "spec": {
                    "type": "string",
                    "enum": spec_names,
                    "description": "扇出目标：已加载的子 agent 名",
                },
                "prompt_template": {
                    "type": "string",
                    "description": (
                        "任务模板，必须包含 {{item}} 占位符；每个 item 替换后"
                        "成为一个子 agent 的完整任务（子 agent 看不到当前对话，"
                        "背景要写全）"
                    ),
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        f"逐项填充 {{{{item}}}} 的清单（纯新建至少 2 项，"
                        f"一批最多 {MAX_ITEMS} 项）"
                    ),
                },
                "resume": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                    "description": (
                        "批量续跑：{resume_id: 续跑指令}。resume_id 来自上次"
                        "report 各项的 resume_from；可与 items 混用（resume 项先跑）"
                    ),
                },
                "capability_mode": {
                    "type": "string",
                    "enum": ["read-only", "read-write", "execute", "all"],
                    "description": "收紧本批全部委托的工具档位（只能比 spec 更严）",
                },
                "isolation": {
                    "type": "string",
                    "enum": ["none", "worktree"],
                    "description": (
                        "覆盖默认隔离。缺省：只读 spec 不隔离，非只读 spec 每项"
                        "独立 worktree（防并行写冲突）；确认各项写的文件互不相交"
                        "且要直接落主工作区时才传 none"
                    ),
                },
            },
            "required": ["spec"],
        },
        handler=run,
        #  与单发 subagent 工具同规则：委托本身不确认，写/执行逐工具照常确认
        #  （审批框经 locked_approver 串行化，一次只弹一个）
        requires_approval=False,
    )
