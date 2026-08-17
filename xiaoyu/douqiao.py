"""斗巧（douqiao）：竞争织造模式（Contest-Weave）——同题竞织，判官择优。

源起七夕斗巧之俗：令多名织手互不相通，独立织造同一幅锦段；待各方完工，
比对工巧优劣，选取最优成果。以多重织造的冗余投入，换取质量上限。

与另两种织造模式的分工：七襄是"N 个任务各跑一次"（拼产能），斗巧是
"一个任务跑 N 次"（拼质量）；宸枢管的是层级与秩序。适用判据就一条：
这个任务值不值得为质量上限付 N 倍钱（架构方案 / 公开 API 定稿 /
久攻不下的 bug）。日常任务用单发委托或七襄。

定名时已拍板的三个设计决策：
- **参赛者严格隔离**：会产生写操作的席位各占一个 worktree（建不出来
  该席弃权，绝不退回主工作区），互相看不到半成品——互通会让多样性
  塌缩成趋同。隔离按**本次调用的有效工具集**判定：capability_mode
  收紧到只读的比赛不需要 worktree（非 git 工作区也能比只读方案）。
- **判官制、赢者全拿**：不做方案合成（合成最易织出四不像）；败者亮点
  以"值得胜者吸收"的形式列出，供胜者方案后续迭代参考。
- **异构模型竞争**：models 参数让不同模型各织一匹——多样性来自模型
  本身，结构性优于同一模型重采样 N 次（错法都一样）。

判官是一次只读委托（读各席 worktree 与自述，横向比对后裁决）。裁决
解析取**最后一处**「胜者：#N」（判官先逐席评述再收尾，中途提及不算
定论）；判官中途失败或点名不在完赛之列时都不冒充权威——各席成果与
resume 句柄照常返回，由上层自行定夺（fail-open 只在评审环节，织造
环节的隔离与存档纪律与七襄同款 fail-closed）。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import worktree
from .agents import (
    CAPABILITY_TOOLS,
    AgentSpec,
    DelegationResult,
    SubagentRun,
    _clean_param,
    execute_delegation,
    normalize_capability,
)
from .config import Config
from .events import Notice, UISink
from .fanout import Attempt, run_attempts
from .providers import UnknownModel
from .qixiang import MIN_ANSWER_CHARS, _CONTINUATION_TASK, _NullSink
from .tools import Tool, Toolbox

MIN_CONTESTANTS = 2
MAX_CONTESTANTS = 6
DEFAULT_CONTESTANTS = 3
#  报告里每席自述的展示上限（席位少，给得比七襄宽）
_PER_SEAT_CAP = 3000
#  判官提示里引用各席自述的上限（判官还能亲自去 worktree 读原文）
_JUDGE_QUOTE_CAP = 4000

_DELIVERABLE_NOTE = (
    "\n\n交付要求：最后一条消息给出完整方案说明——做了什么、关键取舍与理由、"
    "如何验证。这是评审比对的主要依据之一。"
)

_VERDICT_RE = re.compile(r"胜者[:：]\s*#?(\d+)")

_JUDGE_SYSTEM = (
    "你是斗巧（竞争织造）的判官。多名织手就同一任务各自独立织造，"
    "你比对工巧优劣、择优而取。评判纪律：只看成果不看名头；先逐席独立评估，"
    "再横向比对；正确性 > 完备性 > 简洁 > 风格。各席给了 worktree 路径的，"
    "务必用 read_file / grep 亲自核查实际改动，不要只信自述。"
    "工作区根目录：{workspace}"
)


@dataclass
class _Seat:
    """一个参赛席位（身份与收束状态；调度状态在配对的 Attempt 里）。"""

    index: int  # 从 1 起（报告与裁决都用 #N）
    model_label: str  # 显示用；空 = 随 spec/主模型
    model_override: str | None
    result: DelegationResult | None = None
    crash: str = ""
    timed_out: bool = False
    never_started: bool = False
    #  worktree 改动清单（收束后算一次，判官提示与报告共用）
    files: list[str] = field(default_factory=list)

    @property
    def finished(self) -> bool:
        """完赛 = 正常收束且交出了自述。"""
        return (
            self.result is not None
            and not self.result.error
            and not self.result.failure
            and bool(self.result.answer)
            and not self.crash
            and not self.timed_out
        )


def _worktree_files(path: Path) -> list[str]:
    """席位 worktree 的改动清单（status --porcelain 行）。查不出来给空。"""
    try:
        result = worktree._run_git(["status", "--porcelain"], cwd=path)
    except Exception:  # noqa: BLE001 - 清单只是辅助信息
        return []
    if result.returncode != 0:
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def make_douqiao_tool(
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
    """斗巧工具：与七襄同一套依赖（同一本账、同一存档）。"""
    spec_map = {spec.name: spec for spec in specs}
    approve_lock = threading.Lock()

    def locked_approver(name: str, args: dict[str, Any]) -> Any:
        with approve_lock:
            return approver(name, args)

    def run(
        spec: str,
        task: str,
        contestants: Any = None,
        models: Any = None,
        criteria: str | None = None,
        capability_mode: str | None = None,
        judge_model: str | None = None,
    ) -> str:
        #  ---------- 校验（全在开赛之前：半途报错=白烧钱） ----------
        spec_name = _clean_param(spec)
        if spec_name is None or spec_name not in spec_map:
            return f"ERROR: spec 必须是已加载的子 agent 名，可选：{', '.join(spec_map)}。"
        target = spec_map[spec_name]
        if not str(task or "").strip():
            return "ERROR: task 必填——所有织手织的是同一幅锦段。"

        #  档位提前解析：既为隔离判定（写不写得动决定要不要 worktree），
        #  也把非法值挡在开赛前
        cap_param = _clean_param(capability_mode)
        req_mode: str | None = None
        if cap_param is not None:
            req_mode = normalize_capability(cap_param)
            if req_mode is None:
                return (
                    f"ERROR: capability_mode 只能是 read-only / read-write / "
                    f"execute / all，不认识 {cap_param!r}。"
                )
        effective_tools = (
            target.tools
            if req_mode is None
            else tuple(t for t in target.tools if t in CAPABILITY_TOOLS[req_mode])
        )
        if not effective_tools:
            return (
                f"ERROR: capability_mode={req_mode} 收紧后 {spec_name} 没有可用工具了。"
            )

        model_list: list[str] = []
        if models is not None:
            if not isinstance(models, list) or not all(
                isinstance(item, str) and item.strip() for item in models
            ):
                return "ERROR: models 必须是模型名数组（每个模型一席）。"
            model_list = [item.strip() for item in models]
            for name in model_list:
                try:
                    registry.resolve(name)
                except UnknownModel:
                    return f"ERROR: 模型 {name!r} 没有任何 provider 认领——先配好再开赛。"
            total = len(model_list)
        elif contestants is not None:
            #  显式给了席位数就以它为准——0 也要老实进边界报错，不许静默换默认值
            try:
                total = int(contestants)
            except (TypeError, ValueError):
                return f"ERROR: contestants 必须是整数（收到 {contestants!r}）。"
        else:
            total = DEFAULT_CONTESTANTS
        if not MIN_CONTESTANTS <= total <= MAX_CONTESTANTS:
            return (
                f"ERROR: 席位数须在 {MIN_CONTESTANTS}-{MAX_CONTESTANTS} 之间"
                f"（当前 {total}）。一席无赛可比，过多则冗余投入不再换来上限。"
            )
        judge = _clean_param(judge_model)
        if judge is not None:
            try:
                registry.resolve(judge)
            except UnknownModel:
                return f"ERROR: judge_model {judge!r} 没有任何 provider 认领。"

        #  隔离按本次有效工具集判定：会写才需要 worktree；只读比赛（含
        #  capability_mode 收紧出来的只读）无需隔离，非 git 工作区照样能比
        needs_isolation = any(t not in Toolbox.READONLY for t in effective_tools)
        iso_value = "worktree" if needs_isolation else "none"
        full_task = str(task).strip() + _DELIVERABLE_NOTE

        seats = [
            _Seat(
                index=index + 1,
                model_label=model_list[index] if model_list else "",
                model_override=model_list[index] if model_list else None,
            )
            for index in range(total)
        ]
        concurrency = max(1, min(int(config.qixiang_concurrency), 16))
        timeout_s = max(0, int(config.qixiang_task_timeout))
        if hasattr(runs, "capacity"):
            runs.capacity = max(int(runs.capacity), 2 * total + 8)

        #  ---------- 开赛（并发调度收归共享引擎 fanout.py） ----------
        def make_primary(seat: _Seat) -> Any:
            def primary(register: Any) -> DelegationResult:
                return execute_delegation(
                    target, config, registry, usage, sink, locked_approver,
                    permissions, runs, mcp_manager,
                    task=full_task,
                    capability_mode=cap_param,
                    isolation=iso_value,
                    child_sink=_NullSink(),
                    on_agent=register,
                    require_isolation=iso_value == "worktree",
                    model_override=seat.model_override,
                )

            return primary

        def follow_up(run_id: str, register: Any) -> DelegationResult:
            #  追问轮收紧到 read-only：它只要更完整的自述；首轮的干净
            #  worktree 已回收，带写工具 resume 会落回主工作区（理由同七襄）
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
            Attempt(index=seat.index - 1, primary=make_primary(seat), follow_up=follow_up)
            for seat in seats
        ]

        def copy_back(seat: _Seat, attempt: Attempt) -> None:
            seat.result = attempt.result
            seat.crash = attempt.crash
            seat.timed_out = attempt.timed_out
            seat.never_started = attempt.never_started

        def on_settled(attempt: Attempt, settled: int, total_n: int) -> None:
            seat = seats[attempt.index]
            copy_back(seat, attempt)
            mark = "✓" if seat.finished else "⊘"
            sink.emit(Notice(f"  🏮 斗巧：#{seat.index} {mark} 交卷（{settled}/{total_n}）"))

        label = "、".join(s.model_label or "（随 spec）" for s in seats)
        sink.emit(Notice(f"  🏮 斗巧开赛：{spec_name} × {total} 席（{label}）"))
        run_attempts(
            attempts,
            concurrency=concurrency,
            timeout_s=timeout_s,
            min_answer_chars=MIN_ANSWER_CHARS,
            on_settled=on_settled,
        )
        for seat, attempt in zip(seats, attempts):
            copy_back(seat, attempt)
        for seat in seats:
            if seat.result is not None and seat.result.worktree is not None:
                seat.files = _worktree_files(seat.result.worktree)

        finished_seats = [seat for seat in seats if seat.finished]

        #  ---------- 判官（≥2 席完赛才有得比） ----------
        judge_result: DelegationResult | None = None
        winner: _Seat | None = None
        picked: int | None = None
        if len(finished_seats) >= MIN_CONTESTANTS:
            judge_result = _run_judge(
                config, registry, usage, sink, permissions, runs,
                task=str(task).strip(),
                criteria=str(criteria or "").strip(),
                seats=finished_seats,
                judge_model=judge,
            )
            #  判官中途失败（哪怕留了半截输出）不冒充权威：不解析胜者
            if not judge_result.failure and judge_result.answer:
                matches = _VERDICT_RE.findall(judge_result.answer)
                if matches:
                    #  取最后一处：判官被要求先逐席评述再收尾定论，
                    #  中途的「某维度胜者：#N」不是最终裁决
                    picked = int(matches[-1])
                    winner = next(
                        (seat for seat in finished_seats if seat.index == picked), None
                    )

        #  ---------- report ----------
        lines: list[str] = []
        forfeited = total - len(finished_seats)
        header = (
            f"[斗巧 report] spec={spec_name} · {len(finished_seats)} 席完赛"
            + (f" / {forfeited} 席弃权" if forfeited else "")
        )
        if winner is not None:
            header += f" · 胜者 #{winner.index}" + (
                f"（{winner.model_label}）" if winner.model_label else ""
            )
        lines.append(header)
        if len(finished_seats) < MIN_CONTESTANTS:
            lines.append(
                "完赛席位不足两席，无赛可评——已完赛的成果与弃权席位的存档如下，"
                "可用各自的 resume_from 续跑后再战。"
            )
        elif judge_result is not None and judge_result.failure:
            partial = judge_result.answer
            if len(partial) > _PER_SEAT_CAP:
                partial = partial[:_PER_SEAT_CAP] + "\n…（截断）"
            lines.append(
                f"判官中途失败（{judge_result.failure}），未产生有效裁决——"
                "各席成果如下，自行定夺或重开一局只比评审。"
                + (f"\n判官中断前的部分输出（仅供参考，不作数）：\n{partial}" if partial else "")
            )
        elif judge_result is None or not judge_result.answer:
            lines.append("判官没有给出任何输出——各席成果如下，自行定夺。")
        else:
            lines.append("判官裁决：")
            verdict_text = judge_result.answer
            if len(verdict_text) > _PER_SEAT_CAP:
                verdict_text = verdict_text[:_PER_SEAT_CAP] + "\n…（裁决过长已截断）"
            lines.append(verdict_text)
            if winner is None and picked is not None:
                lines.append(
                    f"⚠ 判官点名 #{picked}，但该席不在完赛之列（弃权或编号有误）"
                    "——请核对裁决原文后自行认定胜者。"
                )
            elif winner is None:
                lines.append(
                    "⚠ 裁决文本里没有可解析的「胜者：#N」，胜者以上文为准自行认定。"
                )

        for seat in seats:
            tag = "完赛" if seat.finished else "弃权"
            crown = " 👑" if winner is seat else ""
            head = f"--- #{seat.index} {tag}{crown}"
            if seat.model_label:
                head += f" · model={seat.model_label}"
            elif seat.result is not None and seat.result.model:
                head += f" · model={seat.result.model}"
            lines.append(head)
            result = seat.result
            if result is not None and result.run_id:
                lines.append(f"resume_from: {result.run_id}")
            if result is not None and result.worktree is not None:
                lines.append(
                    f"worktree: {result.worktree}（改动 {len(seat.files)} 处；"
                    "胜者方案 git -C 该路径 diff 查看，确认后 git apply 取回）"
                )
            if seat.crash:
                lines.append(f"ERROR: 执行线程异常（{seat.crash}）")
            elif seat.never_started:
                lines.append("未开始即被中止。")
            elif seat.timed_out:
                lines.append(f"超时中止（>{timeout_s}s），已存档可续跑。")
            elif result is not None and result.error:
                lines.append(result.error)
            elif result is not None and result.failure:
                lines.append(f"ERROR: 织手失败（{result.failure}）")
            if result is not None and result.answer and seat.finished:
                answer = result.answer
                if len(answer) > _PER_SEAT_CAP:
                    answer = answer[:_PER_SEAT_CAP] + "\n…（自述过长已截断，完整上下文可 resume 追问）"
                lines.append(answer)
            for note in result.notes if result is not None else []:
                lines.append(note)
        sink.emit(
            Notice(
                "  🏮 斗巧收枢："
                + (f"胜者 #{winner.index}" if winner is not None else "未决出胜者")
            )
        )
        return "\n\n".join(lines)

    spec_names = sorted(spec_map)
    return Tool(
        name="douqiao",
        description=(
            "斗巧（竞争织造模式）：让 N 名织手**互不相通地独立完成同一个任务**，"
            "判官比对各方案后择优——赢者全拿，败者亮点列出供吸收。"
            "以 N 倍投入换质量上限，只用于值得的任务：架构方案、公开 API 定稿、"
            "久攻不下的 bug；日常任务用单发委托或 qixiang。"
            "models 参数可让不同模型各织一匹（异构竞争，多样性最大）。"
            "写型比赛每席独立 worktree，胜者改动 git apply 取回。"
            f"可选 spec：{', '.join(spec_names)}"
        ),
        parameters={
            "type": "object",
            "properties": {
                "spec": {
                    "type": "string",
                    "enum": spec_names,
                    "description": "织手用哪个子 agent（每席同一 spec，不同上下文）",
                },
                "task": {
                    "type": "string",
                    "description": (
                        "所有织手共同的任务（自包含：他们看不到当前对话，"
                        "背景与验收标准写全）"
                    ),
                },
                "contestants": {
                    "type": "integer",
                    "description": (
                        f"席位数（{MIN_CONTESTANTS}-{MAX_CONTESTANTS}，默认 "
                        f"{DEFAULT_CONTESTANTS}）；给了 models 时以其长度为准"
                    ),
                },
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "异构竞争：每个模型一席（如不同厂商旗舰各一）。"
                        "省略则各席都随 spec/主模型"
                    ),
                },
                "criteria": {
                    "type": "string",
                    "description": "评判标准（本次任务里「工巧」指什么；省略用默认纪律）",
                },
                "capability_mode": {
                    "type": "string",
                    "enum": ["read-only", "read-write", "execute", "all"],
                    "description": (
                        "收紧全部席位的工具档位（只能比 spec 更严）；"
                        "收紧到只读的比赛不需要 git 仓库"
                    ),
                },
                "judge_model": {
                    "type": "string",
                    "description": "判官用哪个模型（默认随主模型；建议用最强的）",
                },
            },
            "required": ["spec", "task"],
        },
        handler=run,
        requires_approval=False,
    )


def _run_judge(
    config: Config,
    registry: Any,
    usage: Any,
    sink: UISink,
    permissions: Any,
    runs: dict[str, SubagentRun],
    *,
    task: str,
    criteria: str,
    seats: list[_Seat],
    judge_model: str | None,
) -> DelegationResult:
    """判官 = 一次只读委托（免确认），横向比对后裁决。"""
    judge_spec = AgentSpec(
        name="douqiao_judge",
        description="斗巧判官",
        system_prompt=_JUDGE_SYSTEM,
        tools=("read_file", "grep", "list_files"),
        max_iterations=24,
    )
    sections = [
        "以下多个方案是对同一任务的独立织造，请评出胜者。",
        f"原任务：\n{task}",
    ]
    if criteria:
        sections.append(f"本次评判标准（优先于默认纪律）：\n{criteria}")
    for seat in seats:
        result = seat.result
        assert result is not None
        quote = result.answer
        if len(quote) > _JUDGE_QUOTE_CAP:
            quote = quote[:_JUDGE_QUOTE_CAP] + "\n…（截断；完整改动去 worktree 里核查）"
        block = [f"--- 方案 #{seat.index}" + (f"（model={seat.model_label}）" if seat.model_label else "")]
        if result.worktree is not None:
            listing = "\n".join(seat.files[:20]) + ("\n…" if len(seat.files) > 20 else "")
            block.append(
                f"改动所在 worktree：{result.worktree}\n改动清单：\n{listing or '（无未提交改动）'}"
            )
        block.append(f"织手自述：\n{quote}")
        sections.append("\n".join(block))
    sections.append(
        "输出要求：先逐席简评，再横向比对，最后**必须**以固定格式收尾——\n"
        "胜者：#<编号>\n理由：<为什么它最优>\n败者亮点：<值得胜者吸收的点；没有写「无」>\n"
        "收尾之前不要出现「胜者：#」字样（中途比较请换说法），收尾只出现一次。"
    )
    sink.emit(Notice("  🏮 斗巧：判官阅卷"))
    return execute_delegation(
        judge_spec, config, registry, usage, sink,
        lambda name, args: True, permissions, runs, None,
        task="\n\n".join(sections),
        child_sink=_NullSink(),
        model_override=judge_model,
    )
