"""小羽的 agent 主循环。

一轮的形状：
    用户输入 → 带 tools 调模型（流式）→ 有 tool_calls 就执行并回灌 → 循环
    → 模型不再调工具即本轮结束。
"""

from __future__ import annotations

import json
import platform
import queue
import random
import re
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import envprobe, errors, media, modes, providers, sandbox, skills, tokens
from .compaction import (
    MIN_SUMMARY_CHARS,
    PREFIX_SUMMARY_INSTRUCTION,
    SUMMARY_INSTRUCTION,
    Compactor,
    is_degenerate_summary,
    microcompact,
)
from .config import Config
from .errors import classify
from .providers import Registry, Route, UnknownModel
from .permissions import Permissions
from .events import (
    Notice,
    PlanUpdated,
    RequestEnded,
    RequestStarted,
    SteerAccepted,
    TextDelta,
    TextEnd,
    ToolCompleted,
    ToolDenied,
    ToolPending,
    ToolPurpose,
    ToolRunning,
    UISink,
)
from .render import PlainSink
from .responses import REASONING_KEY
from .tools import PURPOSE_PARAM, Tool, Toolbox

#  approver(tool_name, args) -> True=允许；(True, 附言)=允许且附言随 tool result
#  回灌模型；False / "" / (False, 理由)=拒绝；非空 str（或 Deny.reason）=拒绝并附理由。
#  理由/附言原文会作为 tool result 回灌模型（拒绝即改指令）——
#  用户在确认框里随手打的一句话，就是给模型的新指示。
#  需要"批准但改写参数"（如宿主把命令包进沙箱再放行）时返回 Allow(updated_args=…)；
#  bool/str/tuple 的简写形态继续有效，Allow/Deny 是它们的显式全功能版。
Approver = Callable[[str, dict[str, Any]], "bool | str | tuple[bool, str] | Allow | Deny"]

#  asker(归一化问题列表) -> {问题: 答案}。ask_user 工具的前端通道：TUI 是行内
#  提问面板（Tui.ask），明文 REPL 是编号问答（cli.text_ask_questions），嵌入
#  宿主可注入自己的形态（如转成飞书交互卡片）。用户提前收工时只含已答部分，
#  空 dict = 一题都没答；不注入（None）时工具不进 schemas，模型看不见它。
#  问题列表每项：{"question": str, "options": [{"label","description"}...],
#  "multi_select": bool}（已由 handler 校验归一，asker 不必再防御）。
Asker = Callable[[list[dict[str, Any]]], dict[str, str]]


class PeerLink(Protocol):
    """跨会话信箱的注入契约（实现见 peers.Registration）。

    Agent 只认这两个方法，不 import peers——库层嵌入的宿主想接自己的消息
    总线（飞书、队列）时，照这个形状实现一个即可。
    """

    def drain(self) -> list[tuple[str, str]]:
        """取走待收消息 → [(来源展示名, 已包装成上下文文本)]，按到达顺序。"""
        ...

    def set_state(self, state: str) -> None:
        """"idle" / "busy"。纯展示，投递不看它。"""
        ...


@dataclass(frozen=True)
class Allow:
    """审批结果：批准。

    note 非空时随 tool result 回灌模型（与 (True, 附言) 等价）。
    updated_args 非 None 时以它**整体替换**本次调用的参数再执行——宿主"批准但
    改写"的通道，典型用法是把只读形命令包上 OS 级沙箱后放行。替换发生在权限规则与
    needs_approval 判定之后：改写出来的参数是宿主自己的责任，不再过一遍规则。
    """

    note: str = ""
    updated_args: dict[str, Any] | None = None


@dataclass(frozen=True)
class Deny:
    """审批结果：拒绝。reason 非空时原文回灌模型（拒绝即改指令）。"""

    reason: str = ""


def normalize_verdict(verdict: Any) -> tuple[bool, str, str, dict[str, Any] | None]:
    """把 Approver 的多形态返回值归一成 (批准?, 附言, 拒绝理由, 改写参数)。

    简写形态的语义完整保留：True=批准；(True, 附言)=批准并附言；非空 str=
    拒绝并附理由；其余 falsy=普通拒绝。(False, 理由) 的理由**不再被静默丢弃**
    ——它就是拒绝理由（此前这条路会把理由吞掉，宿主以为回灌了模型其实没有）。
    改写参数只能经 Allow(updated_args=…) 表达，简写形态没有这个通道。
    """
    if isinstance(verdict, Allow):
        updated = dict(verdict.updated_args) if verdict.updated_args is not None else None
        return True, verdict.note.strip(), "", updated
    if isinstance(verdict, Deny):
        return False, "", verdict.reason.strip(), None
    if isinstance(verdict, tuple):
        flag = verdict[0] if verdict else False
        text = str(verdict[1]).strip() if len(verdict) > 1 else ""
        #  头元素本身是字符串时沿用裸 str 的语义：非空=拒绝理由
        if isinstance(flag, str):
            return False, "", flag.strip() or text, None
        if flag:
            return True, text, "", None
        return False, "", text, None
    if isinstance(verdict, str):
        return False, "", verdict.strip(), None
    return bool(verdict), "", "", None


def normalize_questions(questions: Any) -> list[dict[str, Any]] | str:
    """把模型给的 ask_user 参数归一成 asker 契约的形态；不合法返回错误文本。

    宽进：选项允许写成裸字符串（当 label）；空说明容忍。严出：没有问题文本、
    没有可用选项、超过 4 题或 9 个选项（数字直选只有 1-9，静默截断会误导
    用户"选项都在这了"）都明确报错，让模型改了重调，而不是猜。
    """
    if not isinstance(questions, list) or not questions:
        return "questions 必须是非空数组。"
    if len(questions) > 4:
        return "一次最多 4 个问题，请拆开或合并。"
    normalized: list[dict[str, Any]] = []
    for item in questions:
        if not isinstance(item, dict):
            return "questions 每项必须是对象（含 question 与 options）。"
        question = str(item.get("question", "") or "").strip()
        if not question:
            return "每个问题都必须有非空的 question 文本。"
        raw_options = item.get("options")
        if not isinstance(raw_options, list) or not raw_options:
            return f"问题「{question}」缺少 options 选项数组。"
        if len(raw_options) > 9:
            return f"问题「{question}」的选项超过 9 个，请精简（数字直选只有 1-9）。"
        options: list[dict[str, str]] = []
        for option in raw_options:
            if isinstance(option, str):
                label, description = option.strip(), ""
            elif isinstance(option, dict):
                label = str(option.get("label", "") or "").strip()
                description = str(option.get("description", "") or "").strip()
            else:
                return f"问题「{question}」的选项必须是对象或字符串。"
            if label:
                options.append({"label": label, "description": description})
        if not options:
            return f"问题「{question}」没有任何有效选项（label 都是空的）。"
        normalized.append(
            {
                "question": question,
                "options": options,
                "multi_select": bool(item.get("multi_select")),
            }
        )
    return normalized


class Interrupted(Exception):
    """`Agent.interrupt()` 触发的打断——不是 OS 信号，是宿主线程/协程主动请求的。

    刻意**不**继承 `KeyboardInterrupt`（最初这么写过，被 async 场景的测试炸出来
    才改掉）：`asyncio.Task` 对 `(KeyboardInterrupt, SystemExit)` 有特殊处理——
    不会把它们收进 Task 的结果里正常传播，而是直接原样捅穿事件循环，效果等同于
    "整个进程被 Ctrl-C 了"。库层嵌入场景下 `interrupt()` 跑在 `asyncio.to_thread`
    包着的工作线程里，这个特殊处理会导致 `await async_agent.send(...)` 直接把
    宿主的整个事件循环带崩，而不是像一次普通异常那样被 `try/except` 接住。

    `_stream_once` 的收尾分支同时捕获 `(KeyboardInterrupt, Interrupted)`——两条
    触发路径共用同一段"半截话入历史、残缺 tool_calls 丢弃"的逻辑，但只有真的
    OS 信号才会被顶层特殊对待。
    """


SYSTEM_PROMPT = """你是小羽（Xiaoyu），一个在终端里干活的编码 agent。
名字取自董永传说中七仙女天羽——织女织布，你织代码。

工作方式：
- **跨文件探查一律先用 explore**。凡是"这个符号定义在哪""谁调用了它""这条链路怎么走"
  "这个功能涉及哪些文件"这类需要翻 2 个以上文件才能回答的问题，交给 explore
  （便宜模型的只读子 agent），它会返回带 路径:行号 的结论。
  不要自己连续 read_file 去翻 —— 那些中间内容会永久占住你的上下文，实测会多花一倍。
- 目标明确的单点查找（找一个已知字符串、列某类文件）直接用 grep / list_files，不必绕 explore。
- **explore 给的证据（路径:行号 + 原文行）可以直接采信**，不要为了核对再把那些文件读一遍
  —— 重读一遍等于白花了委托的钱。
- 只有你**马上要动手改**的那个文件，才必须先自己完整 read_file 一遍。
  新建文件不需要读任何东西。
- 改已有文件用 str_replace（精确替换一段唯一文本），这是默认手段。
  write_file 是整文件覆盖，只在新建文件或确实要全量重写时才用。
- str_replace 的 old_str 必须照抄原文、逐字符一致，且在文件中唯一；不唯一就把上下文往外扩。
- 用 bash 做验证：跑测试、跑构建、看 git 状态。
- 改完代码要验证：能跑测试就跑测试，至少做语法/导入检查。发现错误自己修完再交付。
- 一次只解决用户问的问题，不顺手重构、不加没要求的功能。

计划工具（update_plan）：
- 多步任务开始前用 update_plan 列计划：每步一句话、不超过 12 个字；
  不做单步计划；简单直接的任务（大约最容易的那 1/4）跳过计划直接做。
- 状态随做随更：同一时刻恰好一条 in_progress；做完一步立刻标 completed 并把
  下一步标 in_progress。不要事后一次性补记，也不要从 pending 直接跳 completed。
- 理解变了（拆步/合步/换顺序）就先改计划再继续，别让计划过期。
- 调用之后不要在正文里复述整个计划——界面已经显示了，只说这次变更和下一步。
- 好计划的步骤具体可验收（「解析 CLI 参数并校验」），坏计划全是空话
  （「实现功能」「写代码」）。只写好计划。

环境约束：
- {shell_note}
- 工作区根目录：{workspace}
- 系统：{system}

回答风格：一律用中文，包括中间的过程说明和思考，不要中英混杂。结论先行，简洁。
不要复述你做过的每一步，只讲结果和需要用户知道的事。
不确定的地方直说，不要编造文件内容或命令输出。"""

#  空回复自救指令：deepseek 等模型偶发返回完全空的补全，静默收尾等于用户面前
#  一片空白（真实会话里模型改完 8 处代码后空回复结束，用户等了 27 分钟才追问）
EMPTY_REPLY_NUDGE = (
    "你上一条回复是空的，用户什么都没有看到。"
    "请用几句话把当前进展和结论说清楚；如果刚才完成了修改，"
    "总结改了什么、用户接下来如何验证。"
)

#  达到单轮工具调用上限时的收尾指令：与其静默截断，不如让模型交代现场
WRAPUP_INSTRUCTION = """已达到本轮工具调用次数上限，请立刻停止操作，不要再调用任何工具。
直接用几句话总结：
1. 已经完成了什么（具体到文件/改动）
2. 进行到哪一步、还剩什么没做
3. 建议用户下一步怎么做（继续让你做？手动处理？换个思路？）"""

#  plan mode（只读规划态）下
#  允许的工具白名单。deny-by-default：不在名单里的（bash/write_file/str_replace/
#  browser/MCP/插件工具）一律拦——MCP 工具即使"看起来只读"也可能有副作用，宁可误拦。
#  list_sessions 在列（真只读，规划期查一下无害）；send_message 刻意不在——
#  它把文本塞进别人的上下文，是有外部副作用的动作，只读承诺不能对它破例
PLAN_MODE_TOOLS = frozenset(
    {"read_file", "grep", "list_files", "explore", "skill", "web_search",
     "update_plan", "exit_plan_mode", "ask_user", "list_sessions", "task_output",
     "search_tool"}
)

#  进入/退出 plan mode 时注入历史的说明（user 角色）：模型从历史里得知规则，
#  system prompt 不动（它是 prompt cache 的最长前缀，不能随会话态变化）。
#  {plan_file} 由 Agent 按会话填充（计划落成会话侧的 plan.md 文件，
#  plan mode 下它是唯一可编辑的文件）。
PLAN_MODE_ENTER_NOTE = (
    "[系统提示] 用户已开启 plan mode（只读规划态）。规则：\n"
    "1. 只调研、不动手：只能使用只读工具（读文件/搜索/explore/技能说明/联网搜索）"
    "和 update_plan；写文件、执行命令等一切有副作用的工具都会被拦截。\n"
    "2. 把计划写进 plan 文件：{plan_file}（已创建；用 write_file / str_replace"
    " 编辑，免确认——plan mode 下**只有**这个文件可以编辑）。\n"
    "3. 充分调研、计划写好后，调用 exit_plan_mode 提交（以 plan 文件内容为准，"
    "plan 参数可省略），等待用户批准后才能开始执行。\n"
    "4. 用户批准前不要宣称任务已完成。"
)
PLAN_MODE_LEAVE_NOTE = "[系统提示] 用户已关闭 plan mode，可以正常使用全部工具。"

#  插话包装：中途插话裸放进历史时，
#  模型容易把它当成全新任务、丢下在飞的活儿改道。三件套对症：说明这是工作
#  中途来的消息 + <user_query> 划清消息边界 + 尾句提醒把旧账收完。
INTERJECTION_NOTE = "用户在你工作过程中发来一条消息："
INTERJECTION_TAIL = "处理这条消息，同时确保完成之前尚未完成的任务，不要把在做的事丢在半路。"
#  超长插话的截断上限（字符数。Python 按字符切片，天然不会切坏多字节字符，
#  无需额外的 UTF-8 边界对齐）
_INTERJECTION_CAP = 25_000

#  跨会话信箱消息在任务中途到达时补的尾巴（turn 开始消费的不带——那时没有在飞的活）
INBOX_MIDTURN_TAIL = "（这条消息到达时你正在执行任务：处理完它，确保完成之前尚未完成的任务。）"


def wrap_interjection(text: str) -> str:
    """把中途插话包成带说明与收尾提醒的成品文本（steer 的消费点专用）。"""
    if len(text) > _INTERJECTION_CAP:
        text = text[:_INTERJECTION_CAP] + "…（消息过长，已截断）"
    return f"{INTERJECTION_NOTE}\n<user_query>\n{text}\n</user_query>\n{INTERJECTION_TAIL}"

#  harness 注入的伪 user 消息全集：压缩时不当"用户原话"备份，session fork
#  列轮次时也不当轮次开头。新增注入文案必须进这里，否则两处都会误判。
#  PLAN_MODE_ENTER_NOTE 是模板（含 {plan_file}），格式化后的实文由 Agent 追加
#  进自己的 Compactor 集合；turn_starts 另有"[系统提示] 前缀即跳过"的兜底
#  （session_log.turn_starts），离线场景（fork 列轮次）不依赖会话态也能排除。
SYNTHETIC_USER_TEXTS = frozenset(
    {WRAPUP_INSTRUCTION, EMPTY_REPLY_NUDGE, PLAN_MODE_ENTER_NOTE, PLAN_MODE_LEAVE_NOTE}
)


def _find_project_root(workspace: Path) -> Path:
    """最近的含 .git 的祖先目录；没有就是工作区自己（层链的上界）。"""
    for candidate in (workspace, *workspace.parents):
        if (candidate / ".git").exists():
            return candidate
    return workspace


def collect_project_docs(
    workspace: Path, names: tuple[str, ...], cap: int
) -> list[tuple[str, str]]:
    """git 根→工作区逐层收集项目指令文件，返回 [(来源标注, 正文)]，root→leaf 序。

    每层只认 names 里首个命中的非空文件；预算 cap 按 leaf-first 分配——
    从最深层往上分，分完为止，浅层文件被截断或整个丢弃时都有显式标注。
    """
    root = _find_project_root(workspace)
    levels = [root]
    if workspace != root:
        for part in workspace.relative_to(root).parts:
            levels.append(levels[-1] / part)

    found: list[tuple[str, str]] = []
    for level in levels:
        for name in names:
            path = level / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if not text:
                continue
            if level == workspace:
                label = name
            else:
                try:
                    label = f"上层 {path.relative_to(workspace.parent)}"
                except ValueError:
                    label = f"上层 {path}"
            found.append((label, text))
            break

    #  leaf-first 预算：倒序分配，深层（更具体）的永不被浅层挤掉
    budget = cap
    kept: list[tuple[str, str]] = []
    for label, text in reversed(found):
        if budget <= 0:
            kept.append((label, "…（预算已被更深层的指令文件用完，本文件整体省略）"))
            continue
        if len(text) > budget:
            text = text[:budget] + "\n…（指令文件过长，已截断）"
        budget -= len(text)
        kept.append((label, text))
    kept.reverse()
    return kept


def _worth_another_provider(
    verdict: errors.Verdict, chain: list[Route], index: int
) -> bool:
    """鉴权失败/额度耗尽时，链上后面还有**别家** provider 就值得再试一次。

    这两类同一家换个模型名解决不了（key 一家一把、额度是账户级的），但换一家
    可以：直连 key 过期 / 额度用光时，网关兜底正是这个功能存在的理由。别的
    不可重试错误（fatal）不走这条路——那是请求本身有问题，换谁都一样，
    还会把 bug 掩盖成"所有模型都失败"。
    """
    if verdict.kind not in ("auth", "quota"):
        return False
    current = chain[index].provider
    return any(route.provider != current for route in chain[index + 1 :])


@dataclass
class ModelUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: int = 0


@dataclass
class Usage:
    """按**路由**分开记账，key 是 `provider/model` 全限定名。

    做了模型路由就必须分开记——否则省了多少根本算不出来。多 provider 之后同一个
    模型名会跑在两家上（直连 + 网关兜底），单价不同，所以粒度要到 provider 而不是
    只到 model，否则"直连到底省了多少"依旧算不出来。
    也别拿 LiteLLM 侧的 spend 判断：Mantle 系模型不回 usage，spend 记近 $0
    但 token 照样海量。
    """

    by_model: dict[str, ModelUsage] = field(default_factory=dict)

    def add(self, model: str, prompt: int, completion: int) -> None:
        entry = self.by_model.setdefault(model, ModelUsage())
        entry.prompt_tokens += prompt
        entry.completion_tokens += completion
        entry.calls += 1

    @property
    def prompt_tokens(self) -> int:
        return sum(entry.prompt_tokens for entry in self.by_model.values())

    @property
    def completion_tokens(self) -> int:
        return sum(entry.completion_tokens for entry in self.by_model.values())

    @property
    def turns(self) -> int:
        return sum(entry.calls for entry in self.by_model.values())

    def to_dict(self) -> dict[str, Any]:
        """结构化形态：--output-format json / stream-json 的 usage 字段。"""
        return {
            "turns": self.turns,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "by_model": {
                model: {
                    "calls": entry.calls,
                    "prompt_tokens": entry.prompt_tokens,
                    "completion_tokens": entry.completion_tokens,
                }
                for model, entry in sorted(self.by_model.items())
            },
        }

    def __str__(self) -> str:
        if not self.by_model:
            return "还没有调用记录"
        lines = [
            f"{self.turns} 次模型调用 · in {self.prompt_tokens} tok / out {self.completion_tokens} tok"
        ]
        for model, entry in sorted(self.by_model.items()):
            lines.append(
                f"  {model}: {entry.calls} 次 · in {entry.prompt_tokens} / out {entry.completion_tokens}"
            )
        return "\n".join(lines)


class Agent:
    def __init__(
        self,
        config: Config,
        toolbox: Toolbox | None = None,
        approver: Approver | None = None,
        usage: Usage | None = None,
        registry: Registry | None = None,
        quiet: bool = False,
        allow_explore: bool = True,
        session_log: Any | None = None,
        permissions: Permissions | None = None,
        sink: UISink | None = None,
        hook_engine: Any | None = None,
        asker: Asker | None = None,
        peer: "PeerLink | None" = None,
    ) -> None:
        self.config = config
        self.toolbox = toolbox or Toolbox(config)
        #  默认放行，交互式 CLI 会传入真正的确认函数。
        self.approver: Approver = approver or (lambda name, args: True)
        #  提问通道（ask_user 工具）：None = 前端没有提问界面，工具不进 schemas
        self.asker = asker
        #  权限规则（allow/deny + 会话授权）。不传就是空规则集：一切走常规确认。
        self.permissions = permissions or Permissions(config.workspace)
        #  子 agent 复用父级的 registry 和 usage：registry 里 client 按 provider 缓存，
        #  所以连接照样不重开，花的钱也记在同一本账上。
        #  没配任何 provider 时 build() 抛 MissingConfig——启动就报，
        #  而不是等用户开始对话了才炸。
        self.registry = registry or providers.build(config)
        self.usage = usage if usage is not None else Usage()
        #  quiet 用于子 agent：不刷正文，只用缩进显示它调了什么工具。
        #  所有面向用户的输出走 sink（第 0 步重构）：不传就按 quiet 构造明文渲染。
        self.quiet = quiet
        self.sink: UISink = sink or PlainSink(indent="    " if quiet else "", verbose=not quiet)
        #  每次工具调用的记录，供 eval 与排障使用。
        self.trace: list[dict[str, Any]] = []
        #  打转检测：连续完全相同的 (工具, 参数) 调用计数
        self._last_call_key: tuple[str, str] | None = None
        self._call_repeats = 0
        #  库层嵌入用：宿主可从任意线程/协程调用 interrupt()，线程安全，
        #  不依赖 OS 信号。_consume_stream 在下一个 chunk 边界自己发现并收尾。
        self._interrupt_flag = threading.Event()
        #  steer：运行中追加的用户输入，任意线程可入队，
        #  agent 在 step 边界消费。queue.Queue 自带锁，与 interrupt 同一纪律。
        self._steer_queue: queue.Queue[str] = queue.Queue()
        #  通知轨道（后台完成的"搭便车"提醒）：异步事件包成
        #  <system-reminder> 搭在下一条工具结果尾部顺路送达，模型不用轮询、
        #  角色交替与 tool_calls 配对两个不变量都不动。面向嵌入宿主
        #  （后台任务完成、外部状态变化）；带 key 的通知记入已报集合防重。
        self._notify_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._notified_keys: set[str] = set()
        #  后台任务（bash run_in_background / monitor）的完成与事件通知直接挂
        #  通知轨道——notify 线程安全（队列），watcher 线程可直接调。
        #  hasattr 兜底：嵌入宿主可能注入自定义 Toolbox 形态。
        if hasattr(self.toolbox, "tasks"):
            self.toolbox.tasks.notify = self.notify
        #  MCP 检索模式的 server 上线公告走同一条轨道（tools._announce_mcp）
        if hasattr(self.toolbox, "notify_hook"):
            self.toolbox.notify_hook = self.notify
        #  跨会话信箱（可选，由 CLI 注入；见 peers.py）。消费点与 steer 完全重合——
        #  轮次开始 + 每个 step 边界，不新增任何注入时机。
        self.peer = peer
        #  会话落盘（可选，由 CLI 注入；eval / explore 子 agent 不落盘）
        self.session_log = session_log
        #  plan mode 的计划文件：有会话文件就放它旁边
        #  （<会话名>.plan.md，不进仓库）；没有（eval/嵌入）退到工作区 .xiaoyu/。
        log_path = getattr(session_log, "path", None) if session_log is not None else None
        if isinstance(log_path, Path):
            self.plan_file = log_path.with_suffix(".plan.md")
        else:
            self.plan_file = config.workspace / ".xiaoyu" / "plan.md"
        self._plan_enter_note = PLAN_MODE_ENTER_NOTE.format(plan_file=self.plan_file)
        #  生命周期钩子（用户级 hooks.toml；测试可直接注入 engine）。
        #  没有任何 hook 时保持 None——四个触发点零开销。
        self.hook_engine = hook_engine
        if hook_engine is None and config.enable_hooks:
            from . import hooks as hooks_mod

            loaded, problems = hooks_mod.load_hooks()
            for problem in problems:
                self.sink.emit(Notice(f"[hooks.toml：{problem}]", "warn"))
            if loaded:
                self.hook_engine = hooks_mod.HookEngine(
                    loaded,
                    config.workspace,
                    notify=lambda text: self.sink.emit(Notice(text, "warn")),
                )
        #  SKILL.md 技能：启动时扫描一次，索引要写进 system prompt，必须先于它构建
        self.skills = skills.scan_skills() if config.enable_skills else []
        #  来源目录指纹：轮首差量检测（_refresh_skills）靠它把"无变化"的轮次
        #  压到几次 stat，不必每轮重读全部 frontmatter
        self._skills_fingerprint = skills.sources_fingerprint() if config.enable_skills else ()
        #  本会话已加载过的技能名：重复加载时给模型提示，省一轮全文
        self._loaded_skills: set[str] = set()
        #  自上次 update_plan 以来的执行类工具（bash/browser）调用数：
        #  「宣称完成」护栏靠它判断验证类步骤是否真的验证过
        self._exec_evidence = 0
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        #  token 记账锚点（服务端 usage 是权威值，本地只估算它之后新增的
        #  部分，误差不随会话累积）：(权威 prompt_tokens, 当时的消息条数)。
        #  Mantle 系模型不回 usage 时锚点保持 None，退化为纯本地估算。
        self._anchor: tuple[int, int] | None = None
        #  发请求时的消息条数，usage 到达时用来落锚
        self._request_len = 0
        self.compactor = Compactor(
            context_limit=config.context_limit,
            compact_at=config.compact_at,
            keep_recent=config.keep_recent,
            summarizer=self._summarize,
            #  harness 注入的伪 user 消息（收尾/nudge/plan mode），压缩时不算"用户原话"。
            #  plan mode 进场说明是按会话格式化的（含 plan 文件路径），追加实文
            synthetic_user_texts=SYNTHETIC_USER_TEXTS | {self._plan_enter_note},
        )
        #  子 agent 不再挂 explore，避免无限套娃
        if allow_explore and config.enable_explore and self.toolbox.get("explore") is None:
            from .explore import make_explore_tool

            self.toolbox.register(make_explore_tool(config, self.registry, self.usage, self.sink))
        #  web_search：借 deepseek Responses 内置搜索的一次性调用（见 websearch.py 顶部
        #  关于"为什么不切协议"的说明）。没配 deepseek 直连时 check_fn 让它不进 schemas。
        if config.enable_web_search and self.toolbox.get("web_search") is None:
            from .websearch import make_web_search_tool

            self.toolbox.register(
                make_web_search_tool(config, self.registry, self.usage, self.sink)
            )
        #  交互模式（默认 / auto / plan，见 modes.py）。plan mode 是其中一档：
        #  /plan on 或 /mode plan 开启；exit_plan_mode 只在态内可见（check_fn 每轮
        #  求值的工具动态可见性）。requires_approval=True 让
        #  "计划 → 用户批准 → 开始执行"走现成的审批管线，不新增交互形态。
        self.mode: str = modes.get(config.mode).name
        if self.toolbox.get("exit_plan_mode") is None:
            self.toolbox.register(
                Tool(
                    name="exit_plan_mode",
                    description=(
                        "结束 plan mode（只读规划态）。调研完成、计划写进 plan 文件"
                        "之后调用，用户审阅的就是 plan 文件的内容（文件非空时 plan "
                        "参数被忽略，可省略）；获批后即可开始执行。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "plan": {
                                "type": "string",
                                "description": (
                                    "计划全文；已写进 plan 文件时省略（以文件内容为准）"
                                ),
                            }
                        },
                        "required": [],
                    },
                    handler=self._exit_plan_mode,
                    requires_approval=True,
                    check_fn=lambda: self.plan_mode,
                )
            )
        #  ask_user：向用户提选择题。
        #  免确认——提问本身就是用户交互，再套一层审批是自问
        #  自答；check_fn 动态可见：headless / wire / 子 agent 没有 asker，
        #  工具不进 schemas，模型根本看不见（而不是调了才报错）。
        if self.toolbox.get("ask_user") is None:
            self.toolbox.register(
                Tool(
                    name="ask_user",
                    description=(
                        "向用户提出带选项的选择题（1-4 题），等用户作答后返回答案。"
                        "适用：指令有歧义需要用户拍板、几个方案同样合理需要用户选边、"
                        "动手前必须确认偏好。不适用：从上下文能推断答案（果断继续别问）、"
                        "无关紧要的小决定——频繁提问会打断用户。"
                        "界面会自动给每题附加「其他（自由输入）」项，不要自己造；"
                        "推荐某项时把它排第一并在 label 末尾加（推荐）。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "questions": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 4,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "question": {
                                            "type": "string",
                                            "description": "完整问句，以问号结尾",
                                        },
                                        "options": {
                                            "type": "array",
                                            "minItems": 2,
                                            "maxItems": 4,
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "label": {
                                                        "type": "string",
                                                        "description": "选项文本（简短）",
                                                    },
                                                    "description": {
                                                        "type": "string",
                                                        "description": "该选项的含义或取舍（可省）",
                                                    },
                                                },
                                                "required": ["label"],
                                            },
                                        },
                                        "multi_select": {
                                            "type": "boolean",
                                            "description": "是否允许多选（默认单选）",
                                        },
                                    },
                                    "required": ["question", "options"],
                                },
                            }
                        },
                        "required": ["questions"],
                    },
                    handler=self._ask_user,
                    requires_approval=False,
                    check_fn=lambda: self.asker is not None,
                )
            )
        #  update_plan：任务清单工具。handler 几乎什么都不做——
        #  存下来、画出来、返回固定字符串；状态机约束（恰好一条 in_progress）
        #  靠 system prompt 不靠代码，写错的代价只是界面显示怪一点，不值得
        #  用 error 打断模型。全量替换语义：没有增量 API 就没有状态不同步。
        self.plan: list[dict[str, str]] = []
        if config.enable_plan and self.toolbox.get("update_plan") is None:
            self.toolbox.register(
                Tool(
                    name="update_plan",
                    description=(
                        "更新任务计划（全量替换）。传入完整的步骤列表，每步带 step 文本"
                        "与 status 状态；explanation 可选，只在计划本身变更时说明原因。"
                        "同一时刻最多一条 in_progress。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "explanation": {
                                "type": "string",
                                "description": "本次计划变更的原因；照常推进时省略",
                            },
                            "plan": {
                                "type": "array",
                                "description": "完整的步骤列表（全量替换，不是追加）",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step": {
                                            "type": "string",
                                            "description": "一句话步骤，不超过 12 个字",
                                        },
                                        "status": {
                                            "type": "string",
                                            "enum": ["pending", "in_progress", "completed"],
                                            "description": "步骤状态",
                                        },
                                    },
                                    "required": ["step", "status"],
                                },
                            },
                        },
                        "required": ["plan"],
                    },
                    handler=self._update_plan,
                    requires_approval=False,
                )
            )
        #  声明式 subagent（agents/*.toml）：挂成与 explore
        #  同形态的委托工具。allow_explore 兼作"不套娃"闸门——子 agent 不再挂
        if allow_explore and config.enable_agents:
            from .agents import load_agent_specs, make_subagent_tool

            agent_specs, spec_problems = load_agent_specs(config.workspace)
            for problem in spec_problems:
                self.sink.emit(Notice(f"[agents/：{problem}]", "warn"))
            #  resume 存档跨 spec 共享一本：句柄全局唯一，spec 归属在记录里查
            subagent_runs: dict[str, Any] = {}
            for spec in agent_specs:
                if self.toolbox.get(spec.name) is not None:
                    self.sink.emit(
                        Notice(f"[agents/{spec.name}：与已有工具同名，跳过]", "warn")
                    )
                    continue
                self.toolbox.register(
                    make_subagent_tool(
                        spec, config, self.registry, self.usage, self.sink,
                        self.approver, self.permissions,
                        runs=subagent_runs,
                        mcp_manager=getattr(self.toolbox, "mcp_manager", None),
                    )
                )
        #  skill 工具：正文按需加载（渐进披露）。注册与否看开关而不是"当前有没有
        #  技能"——技能可以在会话中途落盘（模型自写/外部安装），启动时零技能不等于
        #  永远零技能；没有技能的时刻由 check_fn 把它挡在 schemas 外。
        if config.enable_skills and self.toolbox.get("skill") is None:
            self.toolbox.register(
                Tool(
                    name="skill",
                    description=(
                        "加载一个技能的完整说明（SKILL.md 正文）。可用技能列表见系统提示；"
                        "会话中途新落盘的技能也能按名加载（未命中会重扫磁盘），加载后按说明执行。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {"name": {"type": "string", "description": "技能名"}},
                        "required": ["name"],
                    },
                    handler=self._load_skill,
                    requires_approval=False,
                    check_fn=lambda: bool(self.skills),
                )
            )

    def last_assistant_text(self) -> str:
        """最后一条 assistant 正文，子 agent 用它把结论交回给父级。"""
        for message in reversed(self.messages):
            if message.get("role") == "assistant" and message.get("content"):
                return media.text_of(message["content"]).strip()
        return ""

    #  项目级指令文件：按此顺序取第一个存在的（AGENTS.md 是跨 agent 的事实标准，
    #  CLAUDE.md 兜底照顾已有此文件的仓库）。
    _PROJECT_DOC_NAMES = ("AGENTS.md", "XIAOYU.md", "CLAUDE.md")
    #  指令文件的字符上限：它常驻每一轮请求，不能无限大
    _PROJECT_DOC_CAP = 12_000
    #  技能索引占上下文窗口的预算比例。超预算不是丢技能而是逐字符截短描述
    #  （见 skills.index_block），所以这个比例只影响描述的详略、不影响技能可见性。
    _SKILL_BUDGET_RATIO = 0.02

    def _system_prompt(self) -> str:
        """组装 system prompt。

        整个 prompt 在会话内是静态的（构建一次、不随轮次变）——这是 prompt cache
        的前缀资产：OpenAI 兼容网关（DeepSeek/LiteLLM）的前缀缓存是自动的，
        守住"不往 system prompt 里放易变内容（时间、动态状态）"即可白拿折扣。
        """
        from .tools import _platform_shell_note

        prompt = SYSTEM_PROMPT.format(
            shell_note=_platform_shell_note(),
            workspace=self.config.workspace,
            system=f"{platform.system()} {platform.release()} ({platform.machine()})",
        )
        #  宿主注入的身份/人格（--append-system-prompt）：紧跟核心身份之后，
        #  早于环境探测/项目指令——人格是"我是谁"，后面两段是"我在什么环境里"。
        if self.config.append_system_prompt:
            prompt += f"\n\n{self.config.append_system_prompt}"
        #  环境画像（工具链有无 + 网络区域）：启动时探测一次即静态，
        #  让模型在选型阶段就避开"缺 git 的机器选 Git 路线"这类死路
        prompt += envprobe.block()
        #  自扩展指南（"文档即能力"）：扩展格式不靠模型的训练记忆——指南随包
        #  分发，这里只放绝对路径，要用时现读全文。路径在会话内静态，
        #  不破坏前缀缓存；文件缺失（罕见的裁剪安装）就整段不提。
        extending_doc = Path(__file__).parent / "docs" / "extending.md"
        if extending_doc.is_file():
            prompt += (
                "\n\n要为小羽本身新增能力（技能 SKILL.md、工具插件、MCP server、hooks）时，"
                f"先完整阅读随包分发的扩展指南再动手：{extending_doc}"
            )
        prompt += self._project_instructions()
        prompt += skills.index_block(
            self.skills, max_tokens=int(self.config.context_limit * self._SKILL_BUDGET_RATIO)
        )
        return prompt

    def _project_instructions(self) -> str:
        """项目级指令文件拼进 system prompt（多层收集）。

        从 git 项目根到工作区逐层收集（monorepo 里在子目录启动也能吃到根目录的
        总规范 + 子项目自己的细则），root→leaf 拼接并标注来源；每层仍只认
        `_PROJECT_DOC_NAMES` 首个命中的文件——保持可预测。层链只在 git 根与
        工作区之间走，绝不越过仓库边界往家目录爬。

        总预算 `_PROJECT_DOC_CAP` 按 **leaf-first** 分配：更深、更具体的文件
        永不被浅层的大文件挤掉——预算先给工作区自己的规范，剩多少才轮到上层。
        """
        found = collect_project_docs(
            self.config.workspace, self._PROJECT_DOC_NAMES, self._PROJECT_DOC_CAP
        )
        if not found:
            return ""
        if len(found) == 1:
            label, text = found[0]
            return f"\n\n项目指令（来自 {label}，由项目维护者提供，遵照执行）：\n{text}"
        blocks = "\n\n".join(f"【{label}】\n{text}" for label, text in found)
        return (
            "\n\n项目指令（由项目维护者提供，遵照执行；自项目根到工作区逐层收集，"
            f"越靠后越具体、冲突时以靠后者为准）：\n{blocks}"
        )

    #  计划状态的合法值（显示符号在 render.py，这里只管校验）
    _PLAN_STATUSES = ("pending", "in_progress", "completed")

    def _update_plan(self, plan: list, explanation: str = "", **extra: Any) -> str:
        """update_plan 的 handler：校验 → 存储 → 打印 → 固定返回。

        返回值恒为固定短语：不回显计划内容，
        不给模型「复读计划」的诱因，也不浪费 token。
        参数校验拒绝未知字段（deny_unknown_fields）：多传字段直接报错，逼模型守约。
        """
        if extra:
            return f"ERROR: update_plan 不接受这些字段：{', '.join(extra)}。只有 explanation 和 plan。"
        if not isinstance(plan, list) or not plan:
            return "ERROR: plan 必须是非空数组，每项形如 {\"step\": \"…\", \"status\": \"pending\"}。"
        cleaned: list[dict[str, str]] = []
        for index, item in enumerate(plan, start=1):
            if not isinstance(item, dict) or set(item) != {"step", "status"}:
                return f"ERROR: 第 {index} 项必须恰好包含 step 和 status 两个字段。"
            step, status = item["step"], item["status"]
            if not isinstance(step, str) or not step.strip():
                return f"ERROR: 第 {index} 项的 step 必须是非空文本。"
            if status not in self._PLAN_STATUSES:
                return (
                    f"ERROR: 第 {index} 项的 status 是 {status!r}，"
                    "只能是 pending / in_progress / completed。"
                )
            cleaned.append({"step": step.strip(), "status": status})
        previous = {item["step"]: item["status"] for item in self.plan}
        self.plan = cleaned
        self.sink.emit(PlanUpdated(plan=cleaned, explanation=explanation))
        note = self._unverified_completion_note(previous, cleaned)
        self._exec_evidence = 0
        return "已更新计划" + note

    # ---------- 交互模式（默认 / auto / plan） ----------

    @property
    def plan_mode(self) -> bool:
        """是不是只读规划态。plan 只是三档之一，但它有一堆关卡在读这个布尔值，
        保留成只读属性——调用方（工具可见性 check_fn、_execute 的关卡、cli、
        测试）一行都不用改。写请走 set_mode。"""
        return self.mode == modes.PLAN

    def sandbox_ready(self) -> bool:
        """这次执行 bash 会不会真的被套上沙箱。

        auto 档放行命令的前提，必须与 tools.py 执行时用同一个谓词——
        判据分成两处迟早会出现"以为在沙箱里所以放行了、实际没套上"。
        """
        return sandbox.enabled(self.config.sandbox)

    def set_mode(self, name: str) -> str:
        """切档。返回给用户看的一句话（TUI/明文 REPL 打的是同一句）。

        plan 的进出要往历史里注入说明（模型得知道规则变了），其它档不注入：
        auto 与默认档的差别只在"问不问用户"，模型侧的能力完全一样，
        告诉它反而是在暗示"现在没人看着"。
        """
        target = modes.get(name).name
        if target == self.mode:
            return f"已经在{modes.label(target)}档"
        was_plan = self.mode == modes.PLAN
        self.mode = target
        if target == modes.PLAN:
            self._seed_plan_file()
            self._record({"role": "user", "content": self._plan_enter_note})
        elif was_plan:
            self._record({"role": "user", "content": PLAN_MODE_LEAVE_NOTE})
        if self.session_log:
            self.session_log.event("mode", value=target)
            #  plan 的进出照旧再发一条：已有的会话日志消费方按这个键读
            if target == modes.PLAN or was_plan:
                self.session_log.event("plan_mode", on=target == modes.PLAN)
        return modes.describe(target, sandbox_ready=self.sandbox_ready())

    def adopt_mode(self, name: str) -> None:
        """restore 路径的切档（ACP session/load 跟随旧会话模式用）。

        与 set_mode 的差别：不往历史注入进出说明（历史里已有当时的那条，
        再注一遍就是让模型读两份规则）、不留 mode 事件（旧日志里最后那条
        仍是事实，续写文件时 last_mode 的"最后写的说了算"不被搅浑）。
        plan 档照样补种 plan 文件——文件可能被清理过，模型按历史里的路径
        去编辑不该扑空。
        """
        target = modes.get(name).name
        if target == self.mode:
            return
        self.mode = target
        if target == modes.PLAN:
            self._seed_plan_file()

    def _seed_plan_file(self) -> None:
        """进 plan mode 时确保 plan 文件存在。**绝不截断已有内容**
        （再次进入时上次的计划还在，模型可以接着改）。
        创建即登记"已读"——空文件没有内容可读，逼模型先 read_file 纯属流程税。
        """
        try:
            if not self.plan_file.exists():
                self.plan_file.parent.mkdir(parents=True, exist_ok=True)
                self.plan_file.write_bytes(b"")
        except OSError as exc:
            self.sink.emit(Notice(f"[plan 文件创建失败：{exc}，编辑时会再报错]", "warn"))
            return
        mark = getattr(self.toolbox, "_mark_read", None)
        if callable(mark):
            mark(self.plan_file.resolve())

    def _is_plan_file(self, path: Any) -> bool:
        """这次调用的 path 是不是 plan 文件（严格路径相等；
        拒绝谓词与放行谓词共用一份，不可能不一致）。"""
        if not isinstance(path, str) or not path:
            return False
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.config.workspace / candidate
        try:
            return candidate.resolve() == self.plan_file.resolve()
        except OSError:
            return False

    def _read_plan_file(self) -> str:
        try:
            return self.plan_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def enter_plan_mode(self) -> str:
        """开启只读规划态（/plan on）。规则以 user 消息注入历史，模型即刻可见。"""
        if self.plan_mode:
            return "已经在 plan mode 中"
        self.set_mode(modes.PLAN)
        return "已进入 plan mode（只读规划态）：只调研不动手，模型交计划后需你批准才执行；/plan off 退出"

    def leave_plan_mode(self) -> str:
        """用户侧关闭（/plan off）。模型侧的退出走 exit_plan_mode 工具（要审批）。"""
        if not self.plan_mode:
            return "当前不在 plan mode 中"
        self.set_mode(modes.DEFAULT)
        return "已退出 plan mode"

    def _exit_plan_mode(self, plan: str = "", **extra: Any) -> str:
        """exit_plan_mode 的 handler。走到这里说明审批已通过（管线在 handler 之前）。

        plan 参数本身不再回显——它已经在确认框里被用户读过，回显只是给模型
        「复读计划」的诱因（与 update_plan 固定返回语同一个道理）。
        """
        if extra:
            return f"ERROR: exit_plan_mode 不接受这些字段：{', '.join(extra)}。只有 plan。"
        if not self.plan_mode:
            return "ERROR: 当前不在 plan mode 中，无需退出。"
        #  不走 set_mode：那会往历史注入"已关闭 plan mode"的说明，而这里的返回值
        #  本身就是给模型的通知，注入等于说两遍
        self.mode = modes.DEFAULT
        if self.session_log:
            self.session_log.event("mode", value=modes.DEFAULT)
            self.session_log.event("plan_mode", on=False)
        #  磁盘为准的替换在 _execute_inner；两头都空 = 没写计划就退。用户已在
        #  审批框放行（他看到的就是空的），照样退出但明说（不硬拦）
        if not isinstance(plan, str) or not plan.strip():
            return (
                "plan mode 已退出（用户批准），但没有找到计划内容——plan 文件是空的。"
                "可以继续执行，必要时先把方案跟用户对齐。"
            )
        return (
            "计划已获用户批准，plan mode 已退出。现在可以使用全部工具，按计划开始执行。"
            f"计划全文保存在 {self.plan_file}，执行途中可随时 read_file 回看。"
        )

    def _ask_user(self, questions: Any = None, **extra: Any) -> str:
        """ask_user 的 handler：校验归一 → 交给前端 asker → 把答案排版回模型。

        校验错误按 ERROR 文本回灌（模型自愈重调）；用户提前收工不是错误——
        已答的照常返回，没答的点名列出，让模型自行判断或换文字形式问。
        """
        if extra:
            return f"ERROR: ask_user 不接受这些字段：{', '.join(extra)}。只有 questions。"
        if self.asker is None:
            #  check_fn 已挡住 schemas，走到这里只剩"历史会话残留调用"一种可能
            return "ERROR: 当前前端没有提问界面。请直接在给用户的正文里说明选项并请求答复。"
        normalized = normalize_questions(questions)
        if isinstance(normalized, str):
            return f"ERROR: {normalized}"
        answers = self.asker(normalized)
        if not answers:
            return (
                "用户关闭了提问，一题都没有回答。请自行做出合理决定并继续；"
                "这个决定确实绕不开用户时，在给用户的正文里直接说明选项并请求答复，"
                "不要立刻再次调用 ask_user。"
            )
        lines = [f"- {question}：{answer}" for question, answer in answers.items()]
        text = "用户的回答：\n" + "\n".join(lines)
        skipped = [item["question"] for item in normalized if item["question"] not in answers]
        if skipped:
            text += (
                "\n\n（用户没有回答其余问题：" + "；".join(skipped)
                + "。请按已有的回答推进，其余自行判断或在正文里问。）"
            )
        return text

    #  验证类步骤的关键词：命中且被标 completed 时检查有没有真的执行过什么
    _VERIFY_STEP = re.compile(r"测试|验证|verify|test", re.IGNORECASE)

    def _unverified_completion_note(
        self, previous: dict[str, str], cleaned: list[dict[str, str]]
    ) -> str:
        """「宣称完成」护栏：验证类步骤被标 completed，但自上次计划更新以来
        没有执行过任何命令/浏览器操作——大概率是没做就说做完了。

        真实会话里模型两次把「测试验证」直接标 completed 并宣布"搞定"，
        实际零验证，用户拿到手才发现黑屏。这里不拦调用，只把疑点回灌给模型。
        """
        suspicious = [
            item["step"]
            for item in cleaned
            if item["status"] == "completed"
            and previous.get(item["step"]) != "completed"
            and self._VERIFY_STEP.search(item["step"])
        ]
        if not suspicious or self._exec_evidence > 0:
            return ""
        steps = "、".join(f"「{step}」" for step in suspicious)
        return (
            f"\n[注意] {steps} 被标记为 completed，但自上次更新计划以来"
            "你没有执行过任何命令或浏览器操作。如果这一步实际没有验证过，"
            "请改回未完成并真正验证（跑命令 / 打开页面确认）；"
            "确实无法在本机验证时，如实告诉用户「未经验证」，不要宣称已测试。"
        )

    def _load_skill(self, name: str) -> str:
        found = next((item for item in self.skills if item.name == name), None)
        if found is None and self.config.enable_skills:
            #  未命中先重扫磁盘再判死刑：技能可能是**本轮**刚落盘的（模型自己
            #  写的），轮首的 _refresh_skills 看不到轮内的新文件。skill 工具
            #  以磁盘为真，索引只是快照。
            self.skills = skills.scan_skills()
            self._skills_fingerprint = skills.sources_fingerprint()
            found = next((item for item in self.skills if item.name == name), None)
        if found is None:
            known = ", ".join(skill.name for skill in self.skills) or "（无）"
            return f"ERROR: 没有名为 {name!r} 的技能。可用：{known}"
        body = skills.load_skill_body(found)
        if body.startswith("ERROR:"):
            return body
        #  基准目录必须随正文给出：技能正文里的相对路径（references/…、
        #  ../共享文档）模型无从知道相对谁——真实会话里模型猜错目录后，
        #  又花二十分钟递归搜盘才找到真实位置。
        header = (
            f"[技能目录：{found.path.parent}。"
            "正文中的相对路径都以此目录为基准。]\n\n"
        )
        if name in self._loaded_skills:
            header = (
                "[提示] 本会话已加载过该技能，若内容还在上下文里无需重复加载。"
                "以下是完整内容（供上下文被压缩后重取）。\n\n"
            ) + header
        self._loaded_skills.add(name)
        return header + body

    #  轮首注入的技能更新提示里，单条描述最多带这么多字符——它是"让模型能选中"
    #  的路由信息，不是完整说明（完整说明归 skill 工具），别让一条长描述吃掉半轮上下文。
    _REFRESH_DESC_CAP = 200

    def _refresh_skills(self) -> None:
        """轮首差量检测：技能目录有增删时更新会话内快照，并向对话尾部注入一条
        `[系统提示]`，让模型**当轮**就看见新技能。

        刻意不重建 system prompt：索引是 prompt cache 的前缀资产，抖一下整段
        前缀作废——那个成本只归显式的 reload_skills 付。检测本身只 stat 来源
        目录（增删技能必然改父目录 mtime，见 skills.sources_fingerprint），
        无变化的轮次零文件读取。
        """
        if not self.config.enable_skills:
            return
        fingerprint = skills.sources_fingerprint()
        if fingerprint == self._skills_fingerprint:
            return
        self._skills_fingerprint = fingerprint
        fresh = skills.scan_skills()
        old_names = {item.name for item in self.skills}
        new_names = {item.name for item in fresh}
        self.skills = fresh
        added, removed = sorted(new_names - old_names), sorted(old_names - new_names)
        if not added and not removed:
            return  # 目录动了但索引没变（如技能内的资源文件增删），不必打扰模型
        parts = []
        if added:
            by_name = {item.name: item for item in fresh}
            listed = "；".join(
                f"{name}（{(by_name[name].description or '无描述')[: self._REFRESH_DESC_CAP]}）"
                for name in added
            )
            parts.append(f"新增 {listed}")
        if removed:
            parts.append(f"移除 {'、'.join(removed)}")
        note = (
            "[系统提示] 技能目录有更新——"
            + "；".join(parts)
            + "。新增技能本会话即可用 skill 工具按名加载；system prompt 里的索引"
            "下次会话才刷新，与本条不一致时以本条为准。不必回应本条。"
        )
        self._record({"role": "user", "content": note})
        #  压缩与 turn_starts 都不能把这条当用户原话（[系统提示] 前缀是离线侧
        #  的兜底判据，这里再进会话内集合，双保险与 plan mode 注入同一纪律）
        self.compactor.synthetic_user_texts |= {note}
        self.sink.emit(
            Notice(f"[技能目录已更新：+{len(added)} / -{len(removed)}，模型本轮可见]", "info")
        )

    def reload_skills(self) -> tuple[list[str], list[str]]:
        """全量重扫技能并重建 system prompt 里的索引。返回 (新增名, 移除名)。

        显式动作（/skills reload、嵌入宿主调用）：system prompt 一变，prompt
        cache 前缀整段作废，这个成本只应由明确要它的人付——轮首的被动通道
        （_refresh_skills）只更新快照 + 注入提示，不动 system prompt。
        """
        old_names = {item.name for item in self.skills}
        if self.config.enable_skills:
            self.skills = skills.scan_skills()
            self._skills_fingerprint = skills.sources_fingerprint()
        else:
            self.skills = []
            self._skills_fingerprint = ()
        new_names = {item.name for item in self.skills}
        self.messages[0] = {"role": "system", "content": self._system_prompt()}
        return sorted(new_names - old_names), sorted(old_names - new_names)

    def reset(self) -> None:
        """清空对话，保留 system prompt。

        随对话一起清的还有全部会话内状态——常驻嵌入场景（recycle 后继续服役
        数周）下这几项不清就是泄漏或错乱：trace 无界增长吃内存；_loaded_skills
        残留会让技能在新会话里返回"已加载过"的存根而模型根本没见过全文
        （全文随历史清掉了）；打转计数/验证证据跨会话残留会误触发护栏。

        system prompt 这里**重建**而不是原样保留：reset 后 prompt cache 反正
        从头计，重建零额外成本，还能把会话中途更新过的技能快照（_refresh_skills）
        转正进索引——常驻嵌入场景 recycle 一次就该拿到当前的技能表。
        静态部分（人格/环境/项目指令）逐字重建结果相同，行为不变。
        """
        self.messages = [{"role": "system", "content": self._system_prompt()}]
        self._anchor = None
        self.plan = []
        #  plan 档必须跟着清：它的规则是以 user 消息注入历史的，历史一空模型就
        #  不知道自己在规划态了，留着就是"关卡还在拦、模型却不明白为什么"。
        #  auto 档没有这种历史依赖（模型侧能力与默认档完全一样），予以保留——
        #  /clear 清的是对话，不是用户的授权偏好。
        if self.mode == modes.PLAN:
            self.mode = modes.DEFAULT
        self.drain_steers()
        self._drain_notifications()
        self._notified_keys.clear()
        #  快照点的对话锚点（用户原话）随历史清空而失效，整表一起清
        store = getattr(self.toolbox, "rewind", None)
        if store is not None:
            store.drop_all()
        self.trace.clear()
        self._loaded_skills.clear()
        self._last_call_key = None
        self._call_repeats = 0
        self._exec_evidence = 0
        if self.session_log:
            self.session_log.event("clear")

    #  resume 时补给未配对 tool_call 的结果文案。与活体中断的"按已放弃处理"
    #  刻意不同：崩溃发生在调用
    #  返回之前，工具**可能已经执行过**——只读/幂等的操作可以直接重试，
    #  有副作用的（写文件/提交/发布）得先核实现状，"按已放弃"会误导模型
    #  以为什么都没发生。
    UNKNOWN_TOOL_OUTCOME = (
        "[结果未知：会话在此调用返回前异常终止，工具可能已执行也可能没有。"
        "只读或幂等的操作可直接重试；有副作用的操作（写文件、git 提交、"
        "发布、发请求）请先用只读手段核实当前状态，再决定要不要重做]"
    )

    def restore(
        self, messages: list[dict[str, Any]], source: str = "", copy: bool = True
    ) -> None:
        """把历史消息接回当前会话（resume 的公开入口，CLI 与嵌入宿主共用）。

        逐条走 _record：既接上上下文，也复制进当前会话文件——新文件自包含、
        可再次 resume。配 `session_log.load_messages()` 使用；source 传来源
        文件路径，会记一条 resumed_from 事件供事后对账。

        copy=False = 只接上下文、不写文件：`--session-id` 续写的是历史所在的
        那个文件本身，再抄一遍等于每次调用都把全部历史翻倍。
        """
        for message in messages:
            if copy:
                self._record(message)
            else:
                self.messages.append(message)
        if self.session_log and source:
            self.session_log.event("resumed_from", source=source)
        #  崩溃恢复：历史末尾若有未配对的 tool_call（= 会话在工具返回前死了；
        #  正常退出路径都会先补齐），用"结果未知"文案补齐——修复写进新会话
        #  文件（copy=False 时写进续写的原文件），下次 resume 不必再修。
        self.close_open_tool_calls(self.UNKNOWN_TOOL_OUTCOME)
        #  压缩日志锁的孤儿检测：来源文件里有
        #  compact_start 而无配对的 compact_end = 上次会话死在压缩中途。
        #  历史本身无损（replacement 没写入就还是原文），提示一句即可。
        if source:
            from .session_log import has_orphan_compact

            try:
                orphan = has_orphan_compact(Path(source))
            except OSError:
                orphan = False
            if orphan:
                self.sink.emit(
                    Notice(
                        "[上次会话在上下文压缩进行中异常终止（历史无损）。"
                        "若上下文仍然偏满，可 /compact 手动补一次压缩]",
                        "warn",
                    )
                )

    def interrupt(self) -> None:
        """请求打断当前正在进行的生成（线程安全，可从任意线程/协程调用）。

        不是立即抛异常——只是置一个标志位，`_consume_stream` 在处理下一个
        chunk 前自己发现并优雅收尾：已经说出的半截话正常入历史，残缺的
        tool_calls 直接丢弃，会话可以继续。语义与 Ctrl-C 完全一致（见
        `Interrupted`），区别只是触发源不是 OS 信号。

        没有正在进行的请求时调用是无操作：`send()` 一开始就会清掉陈旧的标志位，
        不会让"这次调用之前、没打中任何东西的 interrupt()"污染到下一轮不相干的
        请求上——这里故意不做"当前是否在跑"的判断，避免库层再引入一层状态机。
        """
        self._interrupt_flag.set()

    def steer(self, text: str) -> None:
        """运行中追加一条用户输入（线程安全）。

        不打断当前动作——只入队，agent 在下一个 step 边界（一批工具执行完、
        或模型刚给出收尾正文时）消费并强制再跑一步，模型在正确的位置看到它。
        空白输入静默忽略。没有正在进行的轮次时入队也无妨：send() 开头会
        丢弃陈旧插话（与 interrupt 标志位同一纪律），不会污染下一轮。
        """
        if text.strip():
            self._steer_queue.put(text.strip())

    def drain_steers(self) -> list[str]:
        """取走全部未消费的插话（不入历史）。

        两个用途：send() 开头丢弃陈旧插话；前端在一轮结束后回收"没赶上
        本轮"的插话，转成下一轮输入行的预填，不让用户的话凭空消失。
        """
        items: list[str] = []
        while True:
            try:
                items.append(self._steer_queue.get_nowait())
            except queue.Empty:
                return items

    def notify(self, text: str, key: str = "") -> None:
        """投递一条系统通知（线程安全），搭下一条工具结果的 <system-reminder> 送达。

        与 steer 的分工：steer 是"用户的话"（以 user 消息入历史），notify 是
        "系统事件"（宿主的后台任务完成、外部状态变化）——它不该冒充用户发言。
        送达时机：下一次工具结果的尾部；模型正在收尾正文时则在 step 边界以独立
        消息补投并强制再跑一步。turn 开始不丢弃（事件已经发生，丢了就凭空消失，
        与信箱同一纪律）。同一 key 的通知整个会话只送达一次（防宿主重复投递）。
        """
        if text.strip():
            self._notify_queue.put((key.strip(), text.strip()))

    def _drain_notifications(self) -> list[str]:
        """取走待送达的通知并登记已报 key（只在会话自己的执行线程里调用）。"""
        items: list[str] = []
        while True:
            try:
                key, text = self._notify_queue.get_nowait()
            except queue.Empty:
                return items
            if key:
                if key in self._notified_keys:
                    continue
                self._notified_keys.add(key)
            items.append(text)

    def _consume_inbox(self, midturn: bool = False) -> bool:
        """跨会话消息的消费点：与 steer 同一批边界，同样逐条入历史。

        与 steer 的区别只在两处：消息带来源包装（见 peers.wrap），以及**不在
        turn 开始时丢弃**——steer 丢的是"上一轮没赶上的用户插话"，而信箱里的
        消息是别人刚投进来的，丢了就凭空消失了。
        """
        if self.peer is None:
            return False
        try:
            items = self.peer.drain()
        except Exception:  # noqa: BLE001 - 信箱读不动绝不能拖垮会话
            return False
        for sender, wrapped in items:
            #  中途到达的消息补一句收尾提醒（与 steer 的 INTERJECTION_TAIL 同理）：
            #  不让别人的来信把在飞的活儿带偏
            content = f"{wrapped}\n{INBOX_MIDTURN_TAIL}" if midturn else wrapped
            self._record({"role": "user", "content": content})
            self.sink.emit(Notice(f"[收到来自 {sender} 的消息]", "info"))
            if self.session_log:
                self.session_log.event("peer_message", sender=sender)
        return bool(items)

    def _consume_steers(self) -> bool:
        """step 边界的消费点：插话逐条入历史，发 steer.accepted 事件。

        插话不裸放：wrap_interjection 说明"这是工作中途来的
        消息"并提醒收完旧账。事件与会话日志记的是包装前的原话。
        待送达的系统通知也在这里补投——模型已给收尾正文、不再有工具结果可搭
        便车时，以独立消息入历史并强制再跑一步，通知不会烂在队列里。
        """
        consumed = self._consume_inbox(midturn=True)
        for text in self.drain_steers():
            self._record({"role": "user", "content": wrap_interjection(text)})
            self.sink.emit(SteerAccepted(text))
            if self.session_log:
                self.session_log.event("steer")
            consumed = True
        for note in self._drain_notifications():
            self._record(
                {"role": "user", "content": f"<system-reminder>\n{note}\n</system-reminder>"}
            )
            if self.session_log:
                self.session_log.event("notify")
            consumed = True
        return consumed

    def _record(self, message: dict[str, Any]) -> None:
        """消息入历史的唯一入口：同时写会话日志。"""
        self.messages.append(message)
        if self.session_log:
            self.session_log.append(message)

    # ---------- rewind（/rewind：回滚到某轮开始前） ----------

    def rewind_to(self, index: int, conversation: bool = True, files: bool = True) -> str:
        """回滚到快照点 index（第 index 轮开始前）。返回给用户看的结果文本。

        对话截断按**该轮用户输入原文**在当前历史里定位（同文多次出现按点序数
        对应第 n 次）：压缩把消息改写后下标不可靠，原文匹配不上即说明该轮已被
        压缩合并——这时只回文件不回对话（跨 compaction 一律不硬截断的
        保守方向）。文件恢复委托 RewindStore（全成才丢点，失败保留重试）。
        """
        store = getattr(self.toolbox, "rewind", None)
        if store is None:
            return "当前工具箱没有挂快照，无法回滚。"
        point = store.get(index)
        if point is None:
            return f"没有编号为 {index} 的快照点（/rewind 重新看列表）。"
        notes: list[str] = []
        if conversation:
            occurrence = sum(
                1
                for other in store.points()
                if other.index <= index and other.prompt_text == point.prompt_text
            )
            found = -1
            seen = 0
            for position, message in enumerate(self.messages):
                if message.get("role") != "user":
                    continue
                if media.text_of(message.get("content")) != point.prompt_text:
                    continue
                seen += 1
                if seen == occurrence:
                    found = position
                    break
            if found < 0:
                notes.append("该轮对话已被压缩合并，本次只回滚文件、不动对话")
                conversation = False
            else:
                self.messages = self.messages[:found]
                self._anchor = None
                self.plan = []
                if self.session_log:
                    #  与 compact 同一套 replacement 机制：resume 重放时撞到即
                    #  整体替换，不需要理解 rewind 语义
                    self.session_log.event(
                        "rewind", target=index, replacement=self.messages[1:]
                    )
                notes.append(f"对话已回滚到第 {index} 轮开始前（截掉其后的全部轮次）")
        if files:
            ok, summary = store.rewind_files(index)
            notes.append(("文件：" if ok else "文件恢复出错：") + summary)
            if self.session_log:
                self.session_log.event("rewind_files", target=index, ok=ok)
        return "；".join(notes) if notes else "什么也没做。"

    # ---------- 主循环 ----------

    def send(self, user_input: str | list[dict[str, Any]]) -> None:
        """跑完一轮。有跨会话登记时顺带维护 idle/busy——纯展示，不门控投递。

        `user_input` 也可以是 content 部件列表（带图片的一条消息，见 media.py）。
        除了"入历史时原样放进 content"之外，本轮的其余环节一律只看它的文本
        投影（`media.text_of`）——hook 载荷、插话比对、trace 都不需要认识部件。
        """
        self._peer_state("busy")
        #  /rewind 快照的轮次边界：begin 开一个新点，finally 里 finish 补 after
        #  快照并归档——中断/异常路径也要收口，否则该轮的改动不可回滚。
        store = getattr(self.toolbox, "rewind", None)
        if store is not None:
            store.begin(media.text_of(user_input))
        try:
            self._turn(user_input)
        finally:
            if store is not None:
                store.finish()
            self._peer_state("idle")

    def _peer_state(self, state: str) -> None:
        if self.peer is None:
            return
        try:
            self.peer.set_state(state)
        except Exception:  # noqa: BLE001 - 登记坏了不影响会话
            pass

    def _turn(self, user_input: str | list[dict[str, Any]]) -> None:
        #  上一轮没打中任何东西的 interrupt() 不能悬在这——否则这一轮第一个
        #  chunk 就会被误伤打断，且用户莫名其妙。陈旧插话同理：
        #  turn 开始丢弃上轮残留——前端若想救回来，应在轮间自行 drain_steers。
        self._interrupt_flag.clear()
        self.drain_steers()
        #  信箱不在丢弃之列：别人趁我发呆投进来的消息，时间上确实排在本轮
        #  输入之前，先入历史即是正确顺序
        self._consume_inbox()
        #  技能差量检测也在用户输入入历史之前：新技能的提示排在本轮输入前面，
        #  模型读到任务时已经知道有哪些新家伙可用
        self._refresh_skills()
        #  UserPromptSubmit hook：block 则本轮不发生（不入历史、不调模型）
        if self.hook_engine is not None and self.hook_engine.has("UserPromptSubmit"):
            from .hooks import clip

            decision = self.hook_engine.fire(
                "UserPromptSubmit", {"prompt": clip(media.text_of(user_input))}
            )
            if decision.blocked:
                self.sink.emit(Notice(f"[hook 拦截了本轮输入：{decision.reason}]", "warn"))
                return
        self._record({"role": "user", "content": user_input})

        nudged_empty = False
        #  Stop hook 每轮只顶回一次：hook 永远 block 也不会造出死循环
        stop_checked = False
        for _ in range(self.config.max_iterations):
            self.maybe_compact()
            message = self._stream_with_recovery()
            self._record(message)

            calls = message.get("tool_calls")
            if not calls:
                if media.text_of(message.get("content")).strip():
                    #  模型已给出收尾正文，但期间用户插了话：不结束本轮，
                    #  把插话入历史再跑一步
                    if self._consume_steers():
                        continue
                    #  Stop hook：block 则把理由作为 user 消息顶回去续跑一步
                    if (
                        not stop_checked
                        and self.hook_engine is not None
                        and self.hook_engine.has("Stop")
                    ):
                        from .hooks import clip

                        stop_checked = True
                        decision = self.hook_engine.fire(
                            "Stop", {"last_text": clip(media.text_of(message.get("content")))}
                        )
                        if decision.blocked:
                            self.sink.emit(
                                Notice(f"[Stop hook 要求继续：{decision.reason}]", "warn")
                            )
                            self._record(
                                {"role": "user", "content": f"[hook 反馈] {decision.reason}"}
                            )
                            continue
                    return
                #  空回复护栏：content 空且没有工具调用。静默 return 会让整轮
                #  无声结束——用户看不到任何输出，也不知道该不该继续等。
                #  补一条提示请模型重述结论；连续两次空回复就显式告警收场。
                if self.session_log:
                    self.session_log.event("empty_reply", nudged=not nudged_empty)
                if nudged_empty:
                    self.sink.emit(
                        Notice(
                            "[模型连续返回空回复，本轮到此结束。会话仍在，可直接追问]",
                            "error",
                        )
                    )
                    return
                nudged_empty = True
                self.sink.emit(Notice("[模型返回了空回复，已自动请它补上结论]", "warn"))
                self._record({"role": "user", "content": EMPTY_REPLY_NUDGE})
                continue

            for call in calls:
                self._record(self._execute(call))
            self._attach_media()
            #  一批工具执行完是插话生效的主时机：赶在下一次模型调用之前
            self._consume_steers()

        #  撞上限不静默截断：
        #  干了几十轮的活，至少让模型交代做到哪了、下一步怎么办
        self.sink.emit(
            Notice(f"\n[已达到单轮工具调用上限 {self.config.max_iterations}，请模型收尾]", "warn")
        )
        self._record({"role": "user", "content": WRAPUP_INSTRUCTION})
        self._record(self._stream_with_recovery(with_tools=False))

    def _attach_media(self) -> None:
        """把本批工具产出的图片接到历史里（目前只有 MCP 工具会产出）。

        **为什么是一条随后的 user 消息、而不是塞进 tool 消息**：chat completions
        的 `role: tool` 只收字符串内容，各家一致；Anthropic 原生协议允许图片进
        tool_result，但小羽走的是 OpenAI 兼容面，塞进去会被 400。所以工具结果
        仍是纯文本（"图片见下一条"），图片单独作为 user 消息附上——模型看到的
        顺序和因果关系不变，也不动 tool_calls/tool 配对这个硬不变量。

        当前模型看不了图时不静默丢弃，而是明确告诉模型"有 N 张图但我看不了"：
        它据此可以换个办法（让工具输出文本、或请用户描述），而不是对着一份
        缺了关键内容的结果瞎猜。
        """
        parts = self.toolbox.take_media()
        if not parts:
            return
        count = len(parts)
        if not self.registry.sees_images(self.config.model):
            self._record(
                {
                    "role": "user",
                    "content": (
                        f"[上一步的工具返回了 {count} 张图片，但当前模型 "
                        f"{self.config.model} 不接受图片输入，已省略。"
                        "若这些图片是完成任务的关键，请告知用户换用支持视觉的模型"
                        "（/model 可切换），或改用能返回文本的工具。]"
                    ),
                }
            )
            self.sink.emit(
                Notice(
                    f"[工具返回了 {count} 张图片，当前模型 {self.config.model} 看不了，已降级为文字说明"
                    "（/model 换视觉模型，或用 XIAOYU_VISION_MODELS 点名放行）]",
                    "warn",
                )
            )
            return
        self._record(
            {
                "role": "user",
                "content": [
                    media.text_part(f"[上一步的工具返回了 {count} 张图片，如下]"),
                    *parts,
                ],
            }
        )

    # ---------- 上下文管理 ----------

    def context_tokens(self) -> int:
        """当前上下文的 token 估算。

        有锚点：锚点（服务端权威值，已含 system prompt 和工具 schema）+
        只估算锚点之后新增的消息——不是"全量估算 × 校准系数"，误差不累积。
        无锚点（会话开头 / 不回 usage 的模型 / 历史刚被改写）：纯本地估算。
        """
        if self._anchor is not None:
            anchor_tokens, anchor_index = self._anchor
            if anchor_index <= len(self.messages):
                return anchor_tokens + tokens.estimate_messages(self.messages[anchor_index:])
        return tokens.estimate_messages(self.messages) + tokens.estimate_tools(
            self.toolbox.schemas()
        )

    def context_source(self) -> str:
        """/context 显示用：当前估算的依据。"""
        if self._anchor is None:
            return "纯本地估算（尚无服务端 usage 锚点）"
        anchor_tokens, anchor_index = self._anchor
        return f"服务端锚点 {anchor_tokens} tok（第 {anchor_index} 条消息处）+ 增量估算"

    def maybe_compact(self, force: bool = False) -> str | None:
        """需要时压缩历史。返回说明文本，没压缩返回 None。

        分层回收（递进式上下文回收）：
        1. microcompact：清理旧工具输出——便宜（不花模型调用）、不磨损结论；
        2. 还不够再走全量摘要压缩（花一次摘要调用，细节有损）。
        """
        #  上限跟随当前模型（/model 切换、粘性降级都会改它），构造时的快照会过期
        self.compactor.context_limit = self.config.context_limit
        estimated = self.context_tokens()
        if not force and not self.compactor.should_compact(estimated):
            return None

        self.messages, cleared, saved_chars = microcompact(
            self.messages, self.config.keep_recent
        )
        if cleared:
            #  锚点之前的消息被改写了，权威值不再对应现状
            self._anchor = None
            after_micro = self.context_tokens()
            self.sink.emit(
                Notice(f"[已清理 {cleared} 条旧工具输出，估算 {estimated} → {after_micro} tok]")
            )
            if self.session_log:
                self.session_log.event(
                    "microcompact", cleared=cleared, saved_chars=saved_chars
                )
            if not force and not self.compactor.should_compact(after_micro):
                #  清完就降到阈值以下：这轮不必花钱做摘要了
                return f"microcompact：清理 {cleared} 条旧工具输出，估算 {estimated} → {after_micro} tok"
            estimated = after_micro

        self.sink.emit(
            Notice(f"[上下文 {estimated} / {self.config.context_limit} tok，压缩历史…]", "warn")
        )
        before_compact = self.messages
        #  压缩日志锁（start‖end 成对括号）：摘要调用之前
        #  先落 compact_start，全部写完后才落 compact_end——锁最后释放，
        #  崩溃在中途就留下可检测的孤儿 start（restore 会据此提示），
        #  而不是一条谎称压缩完成的记录。
        if self.session_log:
            self.session_log.event("compact_start", trigger="manual" if force else "auto")
        self.messages, note = self.compactor.compact(self.messages)
        self.sink.emit(Notice(f"  {note}"))
        changed = self.messages is not before_compact
        if changed:
            self._anchor = None
        if self.session_log:
            if changed:
                #  把压缩后的完整历史（不含 system，resume 时用新的）存进事件：
                #  /resume 只需
                #  「反向找最后一个带 replacement 的 compact 事件 + 重放其后的行」，
                #  不必理解任何压缩语义。
                self.session_log.event("compact", note=note, replacement=self.messages[1:])
            else:
                self.session_log.event("compact", note=note)
            self.session_log.event("compact_end", ok=changed)
        return note

    def summary_models(self) -> list[Route]:
        """摘要的尝试顺序：先便宜的（连它的影子兜底），失败再回退主模型。"""
        names = [self.config.summary_model or self.config.model, self.config.model]
        return self._routes(names)

    def _routes(self, names: list[str]) -> list[Route]:
        """一串模型名 → 路由链：每个名字展开成「主路由 + 影子兜底」，按 (家, 名) 去重保序。

        影子兜底就是"同名去重"的另一半：网关上的同名模型不出现在 /model 清单里，
        但直连持续失败时它是退路——网关从单点变成兜底，这是整件事最大的收益。
        解析不了的名字直接跳过（多半是备用链里写错了一个名字），不该让每次请求都炸；
        主模型解析不了则照常抛，那是必须立刻看见的配置错误。
        """
        routes: list[Route] = []
        seen: set[tuple[str, str]] = set()
        for index, name in enumerate(names):
            try:
                candidates = [self.registry.resolve(name), *self.registry.backups(name)]
            except UnknownModel:
                if index == 0:
                    raise
                continue
            for route in candidates:
                key = (route.provider, route.model)
                if key not in seen:
                    seen.add(key)
                    routes.append(route)
        return routes

    def _main_route_key(self) -> tuple[str, str] | None:
        """主对话路由的 (provider, model)——前缀缓存只在这条路由上存在。"""
        try:
            route = self.registry.resolve(self.config.model)
        except UnknownModel:
            return None
        return (route.provider, route.model)

    def _summary_call(self, route: Route, transcript: str, prefix: list[dict[str, Any]]) -> str:
        """在一条路由上生成一次摘要，返回正文（可能退化，由调用方判定）。

        两种姿势按路由选：
        - **主对话路由**：逐字重放会话前缀 + 指令尾，带上工具 schema 但
          tool_choice="none"——请求前缀与正常轮次一致，吃 provider 的
          KV 缓存；摘要模型看到的是全保真历史，而非砍到 600 字符的转写。
        - 其它路由（便宜摘要模型）：渲染转写。它的窗口通常小得多，
          也没有本会话的缓存可复用，逐字重放只会超窗。
        前缀重放失败（个别网关不认 tool_choice 等）在本函数内退回转写姿势，
        不把这条路由整个作废。
        """
        if prefix and (route.provider, route.model) == self._main_route_key():
            try:
                schemas = self.toolbox.schemas()
                extra: dict[str, Any] = (
                    {"tools": schemas, "tool_choice": "none"} if schemas else {}
                )
                response = route.client.chat.completions.create(
                    model=route.model,
                    messages=[*prefix, {"role": "user", "content": PREFIX_SUMMARY_INSTRUCTION}],
                    **extra,
                )
            except Exception:  # noqa: BLE001 - 重放姿势失败退回转写姿势
                response = None
            if response is not None:
                if response.usage:
                    self.usage.add(
                        route.qualified,
                        response.usage.prompt_tokens or 0,
                        response.usage.completion_tokens or 0,
                    )
                content = response.choices[0].message.content or ""
                if not is_degenerate_summary(content):
                    return content
                #  重放姿势退化（比如模型执意调工具）：掉回转写姿势再试本路由
        prompt = f"{SUMMARY_INSTRUCTION}\n\n---\n\n{transcript}"
        response = route.client.chat.completions.create(
            model=route.model,
            messages=[{"role": "user", "content": prompt}],
        )
        if response.usage:
            #  摘要调用也是真金白银，记进总账（按路由分开）
            self.usage.add(
                route.qualified,
                response.usage.prompt_tokens or 0,
                response.usage.completion_tokens or 0,
            )
        return response.choices[0].message.content or ""

    def _summarize(self, transcript: str, prefix: list[dict[str, Any]] | None = None) -> str:
        """让模型把早期对话总结成交接说明。不带工具调用、不流式。

        路由链：先便宜模型（默认 deepseek-v4-flash——摘要是有界任务），
        挂了或退化再回退主模型；主模型腿用前缀重放（见 _summary_call）。
        压缩失败的代价是整段历史带不动，不值得为省这一次调用而放弃。
        """
        last_error: Exception | None = None

        for route in self.summary_models():
            try:
                content = self._summary_call(route, transcript, prefix or [])
            except Exception as exc:  # noqa: BLE001 - 换下一个模型再试
                last_error = exc
                self.sink.emit(
                    Notice(f"  摘要模型 {route.qualified} 失败（{type(exc).__name__}），回退下一个")
                )
                continue

            if not is_degenerate_summary(content):
                if route.model != self.config.model:
                    self.sink.emit(Notice(f"  摘要由 {route.qualified} 生成"))
                return content
            #  退化摘要与空摘要同罪：
            #  几十个字的应付式输出不可能承载它要替换的历史，当瞬时失败换下一个
            #  模型；全链都退化时 compact 的降级阶梯会砍半 transcript 再试
            last_error = RuntimeError(
                f"{route.qualified} 返回退化摘要"
                f"（清洗后 {len(content.strip())} 字符，低于下限 {MIN_SUMMARY_CHARS}）"
            )

        raise last_error or RuntimeError("没有可用的摘要模型")

    #  瞬时错误的重试次数与首次退避秒数（指数递增 + jitter）。
    #  SDK 层 max_retries=0：这一层是唯一的重试点，管"分类后行动"：
    #  限流退避、上下文超限先压缩、鉴权直接报清楚，而不是所有异常一视同仁地炸。
    _RECOVERY_ATTEMPTS = 3
    _RECOVERY_DELAY = 2.0
    #  单次退避上限（秒）：指数递增和 Retry-After 都不许超过它
    _RECOVERY_MAX_DELAY = 60.0

    def switch_model(self, name: str) -> None:
        """切换当前模型（粘性），并在会话日志记一条 model 事件。

        所有 config.model 的写点（TUI /model、ACP set_config_option、降级链的
        粘性切换）都走这里：留痕是给"恢复时跟随旧模型"用的——ACP session/load
        按日志里最后生效的模型起会话（session_log.last_model），不留痕的切换
        在恢复后就会静默回到默认模型。
        """
        self.config.model = name
        if self.session_log:
            self.session_log.event("model", model=name)

    def switchable_models(self) -> list[str]:
        """`/model <名字>` 补全的候选：合并清单 + 全限定名 + 当前降级链
        + 网关探测缓存里出现过的。

        全限定名也补出来，因为它是"我就要走网关那份"的唯一表达方式。
        网关清单只在 `/model` 无参探测过之后才有（逐键补全不做网络请求）。
        """
        names: list[str] = []
        for entry in self.registry.listing():
            names.append(entry.model)
            names.extend(f"{provider}/{entry.model}" for provider in entry.backups)
        for route in self.model_chain():
            if route.model not in names:
                names.append(route.model)
        for name in self.registry.remote_cached():
            if name not in names:
                names.append(name)
        return names

    def model_chain(self) -> list[Route]:
        """本次请求的路由尝试顺序：主模型 + 它的影子兜底 + 配置的备用模型（各带兜底）。"""
        return self._routes([self.config.model, *self.config.fallback_models])

    def _stream_with_recovery(self, with_tools: bool = True) -> dict[str, Any]:
        """备用模型降级链的外层：主模型重试耗尽后依次切备用模型。

        层序不能反：
        retry 在内层——瞬时错误该等，等完多半就好了；换模型在外层——重试都耗尽
        说明是模型级故障（持续限流/持续 5xx），再等下去只是白挨。
        换模型时 self.messages 原样重发，会话状态一点不丢。
        切换是粘性的（改 config.model）：否则主模型宕机期间每次请求都要先
        白等一轮重试退避才轮到备用模型。恢复用 /model 切回即可。
        最外层的保底"降级为非 LLM 行为"在 REPL：异常被接住、会话保留，不崩。
        """
        chain = self.model_chain()
        last_error: Exception | None = None
        for index, route in enumerate(chain):
            if index:
                self.sink.emit(
                    Notice(
                        f"[{chain[index - 1].qualified} 重试已耗尽，切换到 {route.qualified}"
                        "（对话原样继续；/model 可切回）]",
                        "warn",
                    )
                )
                #  粘性写回：能用裸名表达就用裸名，跨 provider 才写全限定名。
                #  粘性状态仍是单个字符串，/model、banner、SessionLog 都不用改。
                self.switch_model(self.registry.sticky_name(route))
            try:
                return self._stream_retrying(route, with_tools=with_tools)
            except Exception as exc:  # noqa: BLE001 - 分类器决定要不要换模型
                verdict = classify(exc)
                #  瞬时故障（限流/超时/5xx）值得换；上下文超限该压缩不该换。
                #  鉴权失败/额度耗尽：同一家换了也一样，但**换一家值得试**——直连
                #  key 过期/额度用光时，网关兜底正是这个功能存在的理由。
                #  所以只在还有别家可试时放行。
                if verdict.should_compact:
                    raise
                if not verdict.retryable and not _worth_another_provider(
                    verdict, chain, index
                ):
                    raise
                last_error = exc
        assert last_error is not None  # chain 至少有主模型，必然进过循环
        if len(chain) > 1:
            names = ", ".join(route.qualified for route in chain)
            self.sink.emit(
                Notice(
                    f"[全部路由（{names}）都失败。会话已保留，"
                    "稍后重发即可继续，或 /model 换其它模型]",
                    "error",
                )
            )
        raise last_error

    def _stream_retrying(self, route: Route, with_tools: bool = True) -> dict[str, Any]:
        """单个模型内的重试层：分类后退避重试，耗尽或不可重试则抛出。

        空补全（无正文、无 tool_calls）也在这层原地重试（这必然是流中断或
        reasoning 阶段耗尽 max_tokens，绝非模型故意）——
        比 send() 层的 nudge 便宜（不污染历史、不多一轮可见交互）。不退避：
        这不是限流，立刻重发即可。重试耗尽仍返回空消息，send() 的空回复
        护栏（nudge → 显式告警）是最终兜底，两层不冲突。
        """
        delay = self._RECOVERY_DELAY
        for attempt in range(1, self._RECOVERY_ATTEMPTS + 1):
            try:
                message = self._stream_once(route, with_tools=with_tools)
                if (
                    not media.text_of(message.get("content")).strip()
                    and not message.get("tool_calls")
                    and attempt < self._RECOVERY_ATTEMPTS
                ):
                    self.sink.emit(
                        Notice(
                            f"[模型返回空补全（疑似流中断），已重发"
                            f"（{attempt}/{self._RECOVERY_ATTEMPTS - 1}）]",
                            "warn",
                        )
                    )
                    continue
                return message
            except Exception as exc:  # noqa: BLE001 - 分类器决定去留
                verdict = classify(exc)
                if verdict.should_compact:
                    #  上下文超限：本地估算低估了才会走到这，立刻强制压缩
                    self.maybe_compact(force=True)
                if not verdict.retryable or attempt == self._RECOVERY_ATTEMPTS:
                    raise
                #  服务端给了 Retry-After 就听它的；否则用指数退避。
                #  再乘 ±25% jitter：
                #  同一网关后面的多个会话同时被限流时，不 jitter 会同时醒来再挤一次。
                #  退避等待始终打印出来：用户看得见在等什么，不会误判成假死。
                wait = min(
                    errors.retry_after_seconds(exc) or delay, self._RECOVERY_MAX_DELAY
                )
                wait *= random.uniform(0.75, 1.25)
                self.sink.emit(
                    Notice(
                        f"[{verdict.hint}，{wait:.1f}s 后重试"
                        f"（{attempt}/{self._RECOVERY_ATTEMPTS - 1}）]",
                        "warn",
                    )
                )
                time.sleep(wait)
                delay *= 2
        raise RuntimeError("unreachable")  # for 循环里必然 return 或 raise

    def _stream_once(self, route: Route, with_tools: bool = True) -> dict[str, Any]:
        """流式跑一次模型调用，边打印边攒出完整的 assistant message。

        with_tools=False 用于收尾总结这类"只许说话不许干活"的调用。
        """
        #  发请求前惰性修复历史不变量：
        #  中断路径不需要记得做清理，任何来路的悬空/孤儿都在这里兜住。
        self._repair_history()
        self._request_len = len(self.messages)

        request: dict[str, Any] = {
            "model": route.model,
            "messages": self.messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if with_tools:
            request["tools"] = self.toolbox.schemas()

        content_parts: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        #  推理模型的思考状态（只有 Responses 传输给得出，见 responses.py）
        reasoning: list[dict[str, Any]] = []

        #  create() 阻塞到响应头才返回，之后还要等模型吐出第一个 token——
        #  这整段以前没有任何事件，屏幕停在用户刚敲的那行不动。started/ended
        #  包住它，前端才有东西可画。ended 走 finally：异常和 Ctrl-C 路径上
        #  活区也必须收掉，否则 spinner 会一直转下去。
        self.sink.emit(RequestStarted(route.model))
        try:
            stream = route.client.chat.completions.create(**request)
            self._consume_stream(route, stream, content_parts, pending, reasoning)
        except (KeyboardInterrupt, Interrupted):
            #  Ctrl-C 打在流式中途，或宿主调了 interrupt()：已经说出的半截话也要
            #  入历史，否则整轮凭空消失，用户看到过的内容模型自己却"不记得"。
            #  拼了一半的 tool_calls 直接丢弃（arguments 多半是残缺 JSON，留着
            #  只会害下一轮）。两条触发路径共用这一段，见 Interrupted 的注释。
            if content_parts:
                self._record(
                    {
                        "role": "assistant",
                        "content": "".join(content_parts) + "\n[回答在此处被用户中断]",
                    }
                )
            raise
        finally:
            #  正文段在请求内部闭合，且**任何路径**上都要闭合：
            #  started → delta… → text.end → ended。中断/异常时缺发 TextEnd，
            #  明文 sink 少收尾换行、OSC 133 锚点悬空、ACP 的正文段没有边界——
            #  事件消费者不该为失败路径写补偿逻辑。
            if content_parts:
                self.sink.emit(TextEnd())
            self.sink.emit(RequestEnded())

        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if pending:
            message["tool_calls"] = [pending[index] for index in sorted(pending)]
        if reasoning:
            #  挂在消息上（不另起边表）：压缩丢消息时它跟着走、落盘时跟着存，
            #  没有需要"记得同步清理"的地方。记下产出它的 provider+model——
            #  加密串是模型/服务侧的私有状态，/model 切了不能往回塞；降级链让
            #  同名模型跑在两家上（直连 vs 网关），跨家回放同样过不了校验。
            #  出网前统一被摘掉
            message[REASONING_KEY] = {
                "model": route.model,
                "provider": route.provider,
                "items": reasoning,
            }
        return message

    def _consume_stream(
        self,
        route: Route,
        stream: Any,
        content_parts: list[str],
        pending: dict[int, dict[str, Any]],
        reasoning: list[dict[str, Any]],
    ) -> None:
        """逐 chunk 消费流式响应，把正文和 tool_call 分片攒进传入的容器。"""
        for chunk in stream:
            #  库层嵌入：宿主的 interrupt() 在这个边界被发现。抛
            #  Interrupted（KeyboardInterrupt 子类）复用 _stream_once 里
            #  现成的收尾分支——半截话入历史、残缺 tool_calls 丢弃。
            if self._interrupt_flag.is_set():
                self._interrupt_flag.clear()
                raise Interrupted("宿主请求打断")
            if getattr(chunk, "usage", None):
                prompt_tokens = chunk.usage.prompt_tokens or 0
                self.usage.add(
                    route.qualified,
                    prompt_tokens,
                    chunk.usage.completion_tokens or 0,
                )
                #  拿到真实 usage 就落锚：它权威覆盖了发请求时的全部消息 + 工具
                #  schema，之后 context_tokens 只需估算这个位置之后新增的部分
                if prompt_tokens > 0:
                    self._anchor = (prompt_tokens, self._request_len)
                    #  静默溢出检测（GLM 系实测有此行为）：个别服务端在输入超窗时
                    #  不报错，而是悄悄截断输入再正常作答——usage 里的 prompt_tokens
                    #  反而是唯一的现场证据。回答可能基于被截断的上下文，必须让
                    #  用户知道；上下文本身由锚点记账在下一步触发自动压缩自愈，
                    #  这里只告警不改流程。
                    if prompt_tokens > self.config.context_limit:
                        self.sink.emit(
                            Notice(
                                f"[警告] 服务端报告输入 {prompt_tokens} tok，已超过 "
                                f"{route.model} 的上下文窗口（{self.config.context_limit}）——"
                                "这轮回答可能基于被服务端静默截断的历史，结论请多校验；"
                                "接下来会自动压缩上下文",
                                "warn",
                            )
                        )
            #  chat 协议的 chunk 上没有这个属性，getattr 取到 None——
            #  内核不需要知道自己在跟哪种协议说话
            if items := getattr(chunk, "reasoning", None):
                reasoning.extend(items)
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta
            if delta is None:
                continue

            if delta.content:
                self.sink.emit(TextDelta(delta.content))
                content_parts.append(delta.content)

            for fragment in delta.tool_calls or []:
                slot = pending.setdefault(
                    fragment.index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if fragment.id:
                    slot["id"] = fragment.id
                if fragment.function is None:
                    continue
                #  name 各家都是一次给全，先到先得；arguments 一定是分片累加。
                if fragment.function.name and not slot["function"]["name"]:
                    slot["function"]["name"] = fragment.function.name
                if fragment.function.arguments:
                    slot["function"]["arguments"] += fragment.function.arguments

    # ---------- 工具执行 ----------

    #  连续相同调用：达到这个次数附加提示 / 直接拒绝执行
    _REPEAT_WARN = 3
    _REPEAT_BLOCK = 5

    def _track_repeat(self, name: str, raw_args: str) -> int:
        """返回当前连续相同调用的次数（含本次）。"""
        key = (name, raw_args)
        if key == self._last_call_key:
            self._call_repeats += 1
        else:
            self._last_call_key, self._call_repeats = key, 1
        return self._call_repeats

    def _execute(self, call: dict[str, Any]) -> dict[str, Any]:
        """执行一次工具调用。待送达的系统通知搭在结果尾部（见 notify）。

        附加发生在这层外壳而不是 _tool_message 里：修补孤儿 tool_calls 的
        填充结果（close_open_tool_calls）不该顺手消费掉通知队列。
        trace 与 ToolCompleted 事件记录的仍是不带通知的原始 output。
        """
        message = self._execute_inner(call)
        for note in self._drain_notifications():
            message["content"] += f"\n\n<system-reminder>\n{note}\n</system-reminder>"
        return message

    def _execute_inner(self, call: dict[str, Any]) -> dict[str, Any]:
        name = call["function"]["name"]
        raw_args = call["function"]["arguments"] or "{}"

        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError as exc:
            return self._tool_message(
                call, f"ERROR: 参数不是合法 JSON：{exc}。请重新调用并给出合法 JSON。"
            )
        if not isinstance(args, dict):
            return self._tool_message(call, "ERROR: 参数必须是 JSON 对象。")

        #  目的参数只给人看，不进 handler（handler 收到未知 kwarg 会 TypeError）。
        #  无条件剥离：模型可能给免确认的工具也编一个。
        purpose = str(args.pop(PURPOSE_PARAM, "") or "").strip()

        #  exit_plan_mode 以磁盘为准（工具刻意不吃参数里的计划文本，
        #  防上下文与文件分叉）：plan 文件有内容就整体替换 plan 参数——审批框里
        #  用户读到的、handler 收到的，都是文件的真身。
        if name == "exit_plan_mode":
            disk_plan = self._read_plan_file()
            if disk_plan.strip():
                args = {**args, "plan": disk_plan}

        #  plan 文件专线：plan mode 下对
        #  plan 文件本身的读写免白名单、免确认——拒绝谓词与放行谓词共用
        #  _is_plan_file，二者不可能不一致。
        plan_file_access = (
            self.plan_mode
            and name in ("read_file", "write_file", "str_replace")
            and self._is_plan_file(args.get("path"))
        )

        #  打转检测：模型（尤其便宜模型）会原地用相同参数反复调同一工具。
        #  参数没变结果就不会变——警告两轮还不改就拒绝执行，逼它换路。
        repeats = self._track_repeat(name, raw_args)
        if repeats >= self._REPEAT_BLOCK:
            return self._tool_message(
                call,
                f"ERROR: 你已连续 {repeats} 次用完全相同的参数调用 {name}，"
                "本次拒绝执行。参数不变结果就不会变——换工具、换参数，"
                "或者停下来向用户说明卡在哪里。",
            )

        self.sink.emit(ToolPending(name, args))

        #  权限判定的有序管线：
        #  deny 规则 → 会话授权/allow 规则 → 常规确认。
        #  deny 是 bypass-immune：连 --yolo / auto_approve 都不放行。
        decision, deny_rule = self.permissions.explain(name, args)
        if decision == "deny":
            self.trace.append(
                {"tool": name, "args": args, "ok": False, "output": "DENIED_BY_RULE"}
            )
            self.sink.emit(ToolDenied(name, by="rule"))
            return self._tool_message(
                call,
                f"ERROR: 这次调用命中了用户配置的 deny 权限规则「{deny_rule}」，已拦截"
                "（deny 规则在任何模式下都生效，包括 --yolo）。"
                "请换个做法，或提示用户用 /perm 查看、调整规则。",
            )

        #  plan mode 关卡：白名单之外一律拦（含 --yolo——只读承诺不该被自动
        #  批准绕过）；唯一例外是 plan 文件自身的读写（上面的专线）。
        #  拒因回灌给模型指路：继续调研，或交计划请求批准。
        if self.plan_mode and name not in PLAN_MODE_TOOLS and not plan_file_access:
            self.trace.append(
                {"tool": name, "args": args, "ok": False, "output": "DENIED_PLAN_MODE"}
            )
            self.sink.emit(ToolDenied(name, by="rule"))
            return self._tool_message(
                call,
                f"ERROR: 当前处于 plan mode（只读规划态），{name} 被拦截。"
                "此态下只能用只读工具（read_file/grep/list_files/explore/skill/"
                f"web_search）和 update_plan；唯一可编辑的文件是 plan 文件"
                f"（{self.plan_file}）。请继续调研；计划成形后调用 "
                "exit_plan_mode 提交计划，用户批准后才能执行。",
            )

        #  附言（TUI 确认框的 Tab）：批准时给出，拼进本次 tool result 回灌——
        #  比单独插一条 user 消息安全（不破坏 tool 结果与 tool_calls 的相邻
        #  不变量），且模型在正确的上下文里看到它
        note = ""
        #  exit_plan_mode 的审批 bypass-immune（与 deny 规则同级）：--yolo 也要问。
        #  否则 --yolo 下模型可以自行退出规划态，"用户批准后才执行"就成了空话。
        #  沙箱升权同理（升权只认一次性的人工批准）：allow 规则说的是
        #  "这个形状的命令在沙箱里安全"，不等于"可以不套沙箱跑"；--yolo 下
        #  自动放行升权等于模型能无声解除自己的沙箱。
        must_confirm = name == "exit_plan_mode" or (
            name == "bash" and bool(str(args.get("sandbox_permissions", "") or "").strip())
        )
        #  auto 档：沙箱兜得住的那部分免确认（工作区内改文件、沙箱内跑命令）。
        #  写成 `not must_confirm and …` 而不是指望 exit_plan_mode 恰好不在
        #  auto 白名单里——bypass-immune 这种承诺不该靠巧合成立。
        auto_ok = (
            not must_confirm
            and self.mode == modes.AUTO
            and modes.auto_approves(
                name,
                args,
                outside_workspace=self.toolbox.outside_workspace(args),
                sandbox_ready=self.sandbox_ready(),
            )
        )
        if (
            #  must_confirm 对 allow 规则同样免疫：exit_plan_mode / 沙箱升权
            #  的"必须问"承诺不该被一条恰好匹配的持久规则架空
            (decision != "allow" or must_confirm)
            and not auto_ok
            and not plan_file_access
            and self.toolbox.needs_approval(name, args)
            and (must_confirm or not self.config.auto_approve)
        ):
            #  模型自述的调用目的：确认框上方展示，"这条命令要干嘛"不用人肉猜
            if purpose:
                self.sink.emit(ToolPurpose(name, purpose))
            approved, note, reason, updated = normalize_verdict(self.approver(name, args))
            if not approved:
                self.trace.append({"tool": name, "args": args, "ok": False, "output": "DENIED"})
                self.sink.emit(ToolDenied(name, by="user"))
                if reason:
                    return self._tool_message(
                        call,
                        f"用户拒绝了这次工具调用，并说：「{reason}」。"
                        "这是新的指示，请按用户的说法调整做法。",
                    )
                return self._tool_message(call, "用户拒绝了这次工具调用。请换个做法或先问清用户。")
            if updated is not None:
                #  批准并改写：后续执行、trace、tool.running 事件全部用改写后的参数
                args = dict(updated)
                args.pop(PURPOSE_PARAM, None)
                #  改写后的参数重新过 deny 规则——"deny 是 bypass-immune"的承诺
                #  对宿主改写同样成立（宿主可信，但结构性兜底不靠约定：改写逻辑
                #  的 bug 不该有能力绕过用户明令禁止的操作）
                decision, deny_rule = self.permissions.explain(name, args)
                if decision == "deny":
                    self.trace.append(
                        {"tool": name, "args": args, "ok": False, "output": "DENIED_BY_RULE"}
                    )
                    self.sink.emit(ToolDenied(name, by="rule"))
                    return self._tool_message(
                        call,
                        f"ERROR: 审批方改写后的参数命中了 deny 权限规则「{deny_rule}」，"
                        "已拦截（deny 规则对审批改写同样生效）。",
                    )

        #  PreToolUse hook：在审批之后、执行之前——审批改写过的参数也在
        #  hook 眼前（宿主 seatbelt 包装后的命令才是真正要跑的东西）
        if self.hook_engine is not None and self.hook_engine.has("PreToolUse"):
            decision = self.hook_engine.fire(
                "PreToolUse", {"tool": name, "args": args}, tool_name=name
            )
            if decision.blocked:
                self.trace.append(
                    {"tool": name, "args": args, "ok": False, "output": "DENIED_BY_HOOK"}
                )
                self.sink.emit(ToolDenied(name, by="rule"))
                return self._tool_message(
                    call,
                    f"ERROR: 这次调用被用户配置的 PreToolUse hook 拦截：{decision.reason}。"
                    "请按此反馈调整做法。",
                )

        #  过了全部关卡才算 running（状态机：pending → running →
        #  completed|denied，每个 pending 恰好一个终态——将来活区 spinner 靠它不悬空）
        self.sink.emit(ToolRunning(name, args))
        started = time.monotonic()
        try:
            output = self.toolbox.run(name, args)
        except BaseException as exc:
            #  终态承诺覆盖异常路径（Ctrl-C 最常打在长命令执行中）：running 已发
            #  就必须给终态，否则 TUI spinner 靠带外清扫、ACP client 的 tool_call
            #  永远停在 in_progress。历史侧的 tool 配对由 close_open_tool_calls
            #  兜底，这里只管事件面。
            self.sink.emit(
                ToolCompleted(
                    name,
                    output=f"[执行被中断/异常：{type(exc).__name__}]",
                    ok=False,
                    seconds=time.monotonic() - started,
                )
            )
            raise
        elapsed = time.monotonic() - started
        #  「宣称完成」护栏的证据计数：只有真的跑过命令/操作过浏览器，
        #  验证类计划步骤才谈得上"验证过"
        if name in ("bash", "browser"):
            self._exec_evidence += 1
        if note:
            output += f"\n\n（用户批准这次调用时附加了指示，请照此执行：{note}）"
        if repeats >= self._REPEAT_WARN:
            output += (
                f"\n\n[提示] 这是你连续第 {repeats} 次用相同参数调用 {name}，"
                "结果和上次一样。如果没有新信息，请换一种方法。"
            )
        #  PostToolUse hook：工具已执行，block 的理由作为附注拼进结果让模型看到
        #  （拼在 ERROR 判定之后，反馈不改变工具本身的成败归类——见 ok 的注释）
        ok = not output.startswith("ERROR:")
        if self.hook_engine is not None and self.hook_engine.has("PostToolUse"):
            from .hooks import clip

            decision = self.hook_engine.fire(
                "PostToolUse",
                {"tool": name, "args": args, "ok": ok, "output": clip(output)},
                tool_name=name,
            )
            if decision.blocked:
                output += f"\n\n[PostToolUse hook 反馈，请重视] {decision.reason}"
        self.trace.append({"tool": name, "args": args, "ok": ok, "output": output})
        self.sink.emit(ToolCompleted(name, output=output, ok=ok, seconds=elapsed))
        return self._tool_message(call, output)

    @staticmethod
    def _tool_message(call: dict[str, Any], content: str) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": call["id"],
            "content": content,
        }

    def close_open_tool_calls(self, reason: str) -> None:
        """补齐所有悬空的 tool_calls，否则下一次请求会因缺少 tool 结果而报错。

        全量扫描而不是只看最后一条（"tool_use/tool_result 永远配对"做成
        随处可调的显式收尾函数）：中断可能打在批量工具执行到一半——
        前几个结果已入历史、后几个悬空，此时最后一条是 tool 消息而不是 assistant。

        CLI 在 Ctrl-C 时仍显式调用它（能给出比通用兜底更准确的原因文案）；
        _repair_history 在每次发请求前用通用文案再兜一次底。
        """
        index = 0
        while index < len(self.messages):
            message = self.messages[index]
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                index += 1
                continue
            #  收集紧随其后的 tool 结果；缺的补在这一段 tool 消息的末尾，保持相邻
            tail = index + 1
            answered: set[str] = set()
            while tail < len(self.messages) and self.messages[tail].get("role") == "tool":
                answered.add(self.messages[tail].get("tool_call_id", ""))
                tail += 1
            for call in message["tool_calls"]:
                if call.get("id") in answered:
                    continue
                filler = self._tool_message(call, reason)
                self.messages.insert(tail, filler)
                #  插入点可能在锚点之前，权威值不再对应现状
                self._anchor = None
                if self.session_log:
                    self.session_log.append(filler)
                tail += 1
            index = tail

    def _repair_history(self) -> None:
        """发请求前修复历史不变量（惰性修复）：

        1. 每个 tool_call 都有对应的 tool 结果（缺的补 aborted 占位）；
        2. 每条 tool 消息都对应得上某个 tool_call（孤儿直接删——压缩切点、
           手工改历史等任何来路的孤儿都会让请求 400）。

        好处是中断/异常路径不需要"记得清理"：只要走到发请求，历史一定合法。
        """
        self.close_open_tool_calls("[此调用未返回结果（可能被中断），按已放弃处理]")
        known_ids = {
            call.get("id")
            for message in self.messages
            for call in message.get("tool_calls") or []
        }
        repaired = [
            message
            for message in self.messages
            if message.get("role") != "tool" or message.get("tool_call_id") in known_ids
        ]
        if len(repaired) != len(self.messages):
            self.messages = repaired
            self._anchor = None
