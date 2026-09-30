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

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ui
from .agents import (
    AgentSpec,
    DelegationResult,
    SubagentRun,
    _clean_param,
    execute_delegation,
)
from .config import EFFORT_LEVELS, Config
from .events import Notice, UISink
from .fanout import (
    BREAKER_STREAK,
    HEARTBEAT_SECONDS,
    Attempt,
    ObservingSink,
    attach_partial,
    run_attempts,
)
from .providers import UnknownModel
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


#  批量运行时子 agent 的 sink：工具刷屏不转发，告警与最近动作留着（见 ObservingSink）
_NullSink = ObservingSink


def make_progress_reporter(
    observers: list[tuple[str, ObservingSink, Callable[[], bool]]],
    sink: Any,
    badge: str,
) -> Callable[[], None]:
    """批量调度每个 tick 叫一次的播报：转述各项旁观到的告警；长时间没有任何一项
    收束时，报一次还在跑的各项最近在干什么。observers 每项是
    (标签, 旁观 sink, 这一项是否还在跑)。"""
    state = {"quiet_since": time.monotonic()}

    def report() -> None:
        spoke = False
        for label, observer, _running in observers:
            for text in observer.drain():
                spoke = True
                sink.emit(Notice(f"  {badge} [{ui.preview(label, 40)}] {text}", "warn"))
        now = time.monotonic()
        if spoke:
            state["quiet_since"] = now
            return
        if now - state["quiet_since"] < HEARTBEAT_SECONDS:
            return
        state["quiet_since"] = now
        for label, observer, running in observers:
            if running() and (activity := observer.activity()):
                sink.emit(Notice(f"  {badge} [{ui.preview(label, 40)}] 仍在跑：{activity}"))

    def settled() -> None:
        state["quiet_since"] = time.monotonic()

    report.settled = settled  # type: ignore[attr-defined]
    return report


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
    observer: ObservingSink = field(default_factory=ObservingSink)


def _status_of(state: _ItemState, cancelled: bool) -> str:
    """收束分类：error = 参数校验失败没执行；aborted = 超时/用户中止；
    failed = 执行异常；partial = 撞轮数上限 / 预算被叫停，交了进度但没做完。"""
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
    if state.result.cut_short:
        return "partial"
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
    stop_requested: Callable[[], bool] | None = None,
    guards: Any = None,
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
        model: str | None = None,
        effort: str | None = None,
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

        #  模型与推理深度：整批统一（批量迁移用便宜模型、只读调研给 low）。
        #  模型名先过 registry：没人认领的名字在开工前挡下，不让 N 项一起 400
        model_name = _clean_param(model)
        if model_name is not None:
            try:
                registry.resolve(model_name)
            except UnknownModel:
                return f"ERROR: 模型 {model_name!r} 没有任何 provider 认领——先配好再扇出。"
        effort_level = _clean_param(effort)
        if effort_level is not None:
            effort_level = effort_level.lower()
            if effort_level not in EFFORT_LEVELS:
                return (
                    f"ERROR: effort 只认 {' / '.join(EFFORT_LEVELS)}，不认识 {effort!r}。"
                )

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
                    #  resume 项沿用上次的隔离，isolation 本就被忽略
                    isolation=None if state.resume_from else iso_value,
                    resume_from=state.resume_from,
                    child_sink=state.observer,
                    on_agent=register,
                    #  批量并行写不许退回主工作区（worktree 建不出来=该项不执行）。
                    #  resume 项看的是存档里"上次是否隔离"，与本批的 isolation 无关
                    require_isolation=iso_value == "worktree" or bool(state.resume_from),
                    #  resume 项的模型钉在存档上（execute_delegation 内部），
                    #  这里传了也只对新开项生效
                    model_override=model_name,
                    effort_override=effort_level,
                    guards=guards,
                )

            return primary

        def make_follow_up(state: _ItemState) -> Any:
            def follow_up(run_id: str, register: Any) -> DelegationResult:
                #  追问轮一律收紧到 read-only：它只要一份更完整的交接，不需要写；
                #  首轮的干净 worktree 已被回收，只读续跑直接在主工作区看即可，
                #  不必为一轮追问重建目录
                return execute_delegation(
                    target, config, registry, usage, sink, locked_approver,
                    permissions, runs, mcp_manager,
                    task=_CONTINUATION_TASK,
                    capability_mode="read-only",
                    resume_from=run_id,
                    child_sink=state.observer,
                    on_agent=register,
                    effort_override=effort_level,
                    guards=guards,
                )

            return follow_up

        attempts = [
            Attempt(
                index=state.index, primary=make_primary(state), follow_up=make_follow_up(state)
            )
            for state in states
        ]
        for state in states:
            state.observer.label = state.label

        def still_running(attempt: Attempt) -> Callable[[], bool]:
            return lambda: (
                attempt.started_at is not None and attempt.result is None
                and not attempt.crash and not attempt.never_started
            )

        progress = make_progress_reporter(
            [(state.label, state.observer, still_running(attempt))
             for state, attempt in zip(states, attempts)],
            sink, "🕸 七襄",
        )

        def copy_back(state: _ItemState, attempt: Attempt) -> None:
            state.result = attempt.result
            state.crash = attempt.crash
            state.timed_out = attempt.timed_out
            state.never_started = attempt.never_started

        def on_settled(attempt: Attempt, settled: int, total_n: int) -> None:
            state = states[attempt.index]
            copy_back(state, attempt)
            progress.settled()
            status = _status_of(state, cancelled=False)
            mark = {"completed": "✓", "failed": "✗", "partial": "◐"}.get(status, "⊘")
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
        def report(cancelled: bool) -> str:
            return _report(
                states, attempts, cancelled=cancelled, tripped=tripped[0], spec_name=spec_name,
                model_name=model_name, effort_level=effort_level, concurrency=concurrency,
                timeout_s=timeout_s, per_item_cap=per_item_cap, sink=sink,
            )

        tripped = [""]
        try:
            tripped[0] = run_attempts(
                attempts,
                concurrency=concurrency,
                timeout_s=timeout_s,
                min_answer_chars=MIN_ANSWER_CHARS,
                on_settled=on_settled,
                stop_requested=stop_requested,
                on_tick=progress,
                breaker=BREAKER_STREAK,
            ) or ""
        except BaseException as exc:
            #  用户打断 / 宿主叫停：照样往上抛，但已收束各项的结论与 resume 句柄
            #  随异常带出去——不带的话存档还在，句柄却没人知道
            attach_partial(exc, lambda: report(cancelled=True))
            raise
        finally:
            #  最后一个 tick 之后才冒出来的告警也要转述
            with contextlib.suppress(Exception):
                progress()
        return report(cancelled=False)

    def _report(
        states: list[_ItemState], attempts: list[Attempt], *, cancelled: bool,
        tripped: str = "",
        spec_name: str, model_name: str, effort_level: str, concurrency: int,
        timeout_s: int, per_item_cap: int, sink: Any,
    ) -> str:
        for state, attempt in zip(states, attempts):
            state.result = attempt.result
            state.crash = attempt.crash
            state.timed_out = attempt.timed_out
            state.never_started = attempt.never_started
            state.started_at = attempt.started_at

        #  ---------- 聚合 report：输入顺序，与完成先后无关 ----------
        counts = {"completed": 0, "partial": 0, "failed": 0, "aborted": 0, "error": 0}
        blocks: list[str] = []
        retry_ids: list[str] = []
        for state in states:
            status = _status_of(state, cancelled=cancelled)
            counts[status] += 1
            zh = {
                "completed": "完成",
                "partial": "未做完",
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
                lines.append("被打断时还没交卷。" if cancelled and state.started_at else "未开始即被中止。")
            elif status == "partial":
                lines.append(f"{result.cut_short}，被叫停时交代的进度如下（不是最终结论，可续跑）：")
            if result is not None and result.answer and status in ("completed", "partial", "failed"):
                answer = result.answer
                if len(answer) > per_item_cap:
                    answer = answer[:per_item_cap] + "\n…（结论过长已截断；完整上下文在存档里，可 resume 追问）"
                lines.append(answer)
            for note in result.notes if result is not None else []:
                lines.append(note)
            blocks.append("\n".join(lines))

        header = (
            ("[七襄 report · 被打断，以下是打断时的进度] " if cancelled else "[七襄 report] ")
            + f"spec={spec_name}"
            + (f" · model={model_name}" if model_name else "")
            + (f" · effort={effort_level}" if effort_level else "")
            + f" · 完成 {counts['completed']} / 未做完 {counts['partial']} / "
            f"失败 {counts['failed']} / 中止 {counts['aborted']} / 未执行 {counts['error']}"
            f"（共 {len(states)} 项，并发 {concurrency}）"
        )
        hints: list[str] = []
        if tripped:
            hints.append(f"⚠ {tripped}——这不是换一项就能好的问题，先把它解决再重跑。")
        if retry_ids:
            resume_obj = ", ".join(f'"{rid}": "<续跑指令>"' for rid in retry_ids[:4])
            hints.append(
                "未完成的项可批量续跑：再调 qixiang，resume 传 "
                f"{{{resume_obj}{', …' if len(retry_ids) > 4 else ''}}}；"
                f"或用 {spec_name} 工具带 resume_from 单独续跑。"
            )
        sink.emit(
            Notice(
                f"  🕸 七襄{'被打断' if cancelled else '收束'}："
                f"完成 {counts['completed']}/{len(states)}"
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
                "model": {
                    "type": "string",
                    "description": (
                        "本批全部委托用的模型（缺省随 spec 声明/主会话）。"
                        "批量迁移、批量调研这类活给便宜模型；resume 项钉住上次的模型"
                    ),
                },
                "effort": {
                    "type": "string",
                    "enum": list(EFFORT_LEVELS),
                    "description": "本批全部委托的推理深度（缺省随 spec 声明/主会话）；只读调研给 low",
                },
            },
            "required": ["spec"],
        },
        handler=run,
        #  与单发 subagent 工具同规则：委托本身不确认，写/执行逐工具照常确认
        #  （审批框经 locked_approver 串行化，一次只弹一个）
        requires_approval=False,
    )
