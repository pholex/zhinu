"""表现层：消费 events.py 的类型化事件流，怎么画由各 UISink 实现决定。

第 0 步抽出接口（agent 不再直接 print），本次对象化：
sink 收敛为单方法 emit(event)，事件即协议。新事件（tool.running /
tool.denied）在明文渲染下刻意无输出——它们是给 TUI 活区（spinner/终态定格）
和 headless 消费者准备的。

唯一的例外是 request.started：它覆盖的"请求已发出、还没有任何输出"这段
空白在明文下同样存在，而且没有活区可以替代。所以在 **stdout 是终端时**
打一行，管道/CI 里仍然静默（那里多打一行是污染，且没人在看）。
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable

from . import media, ui
from .events import (
    Notice,
    PlanUpdated,
    RequestStarted,
    SteerAccepted,
    TextDelta,
    TextEnd,
    ToolCompleted,
    ToolPending,
    ToolPurpose,
    UIEvent,
)

#  兼容再导出：sink 协议与事件定义住在 events.py，历史引用路径仍可用
from .events import NoticeLevel, UISink  # noqa: F401


def args_preview(
    name: str, args: dict[str, Any], reserve: int = 0, width: int | None = None
) -> str:
    """工具参数的一行预览。

    宽度按终端实际列数算：`reserve` 是调用方在这段预览之前已经占掉的列数
    （缩进 + 符号 + 工具名）。写死上限的话，窄终端上"一行预览"会折成两行，
    折叠摘要的意义就没了。
    """
    if name == "bash":
        return ui.fit(args.get("command", ""), reserve, width)
    if "path" in args:
        return ui.fit(args["path"], reserve, width)
    return ui.fit(args, reserve, width)


#  计划状态的显示符号（校验用的合法状态集在 agent 侧，这里只管画）
_PLAN_MARKS = {"completed": "✔", "in_progress": "▶", "pending": "○"}


# ---------- resume 回放：历史消息 → 事件流补打 ----------

#  resume 后默认回放的轮数：1 轮常常只有一句收尾，2 轮足够看清"上次做到哪"
REPLAY_TURNS = 2
#  单条消息的展示上限（防历史里一条超长回答把终端打爆）。工具输出的折叠
#  由各 sink 自己管（RichSink 折叠成一行、PlainSink 只打首行），这里不管
_REPLAY_USER_LINES = 4
_REPLAY_TEXT_LINES = 40


def replay_transcript(
    messages: list[dict[str, Any]],
    sink: UISink,
    skip_user_texts: frozenset[str] = frozenset(),
    header: str = "",
) -> int:
    """把历史消息按事件流补打给 sink（重建 turn、喂同一个渲染器），
    resume 后 scrollback 里就有对话现场，不再只有一行"上次说到"。

    只用现有事件词汇，任何 sink 都能消费，不为回放另立协议：
    - user → 一行 Notice「› …」（skip_user_texts 里的 harness 注入文案跳过）
    - assistant 正文 → TextDelta + TextEnd（超长截断，Notice 说明还藏多少）
    - 工具调用 → 撞到 tool 结果时把 ToolPending + ToolCompleted 成对补发，
      RichSink 的只读折叠组与 Ctrl-O 留底照常生效；没等到结果的调用
      （会话在执行中断掉）不补发，回放只求现场感不求完备
    header 非空时在首个内容之前打出（懒发：全被跳过就什么都不打）。
    返回补打出内容的消息条数，0 = 没有可回放内容（调用方可退回旧摘要）。
    """
    emitted = 0

    def notice(text: str) -> None:
        sink.emit(Notice(text))

    def ensure_header() -> None:
        nonlocal emitted
        if emitted == 0 and header:
            notice(header)
        emitted += 1

    #  tool_call id → (工具名, 参数)。参数在历史里是 JSON 字符串，坏了就给空 dict
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    for record in messages:
        role = record.get("role")
        if role == "user":
            text = media.text_of(record.get("content"))
            if not text.strip() or text in skip_user_texts:
                continue
            if emitted:
                notice("")  # 轮与轮之间空一行
            ensure_header()
            lines = text.rstrip("\n").splitlines() or [""]
            shown = ["› " + lines[0]] + ["  " + line for line in lines[1:_REPLAY_USER_LINES]]
            if len(lines) > _REPLAY_USER_LINES:
                shown.append(f"  …还有 {len(lines) - _REPLAY_USER_LINES} 行")
            notice("\n".join(shown))
        elif role == "assistant":
            for call in record.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                calls[str(call.get("id") or "")] = (str(function.get("name") or "tool"), args)
            text = media.text_of(record.get("content")).rstrip("\n")
            if not text:
                continue
            ensure_header()
            lines = text.splitlines()
            sink.emit(TextDelta("\n".join(lines[:_REPLAY_TEXT_LINES])))
            sink.emit(TextEnd())
            if len(lines) > _REPLAY_TEXT_LINES:
                notice(f"  …还有 {len(lines) - _REPLAY_TEXT_LINES} 行")
        elif role == "tool":
            name, args = calls.get(str(record.get("tool_call_id") or ""), ("", {}))
            if not name:
                name = str(record.get("name") or "tool")
            output = media.text_of(record.get("content"))
            ensure_header()
            sink.emit(ToolPending(name, args))
            sink.emit(
                ToolCompleted(name, output=output, ok=not output.startswith("ERROR"), seconds=0.0)
            )
    if emitted:
        #  收尾空行兼当分隔：也让 RichSink 顺手把攒着的只读折叠组落盘
        notice("")
    return emitted


#  OSC 133 语义锚点：assistant 正文块的首尾打零宽标记（A=块起点，B/C=正文收束）。
#  识别 shell integration 协议的终端（iTerm2/Kitty/WezTerm/Ghostty）会把每段
#  回复当成一个可导航的"提示块"——跳上一条回复、选中整段输出都是终端白送的。
#  纯文本直出 scrollback 的架构下这是零成本增强；只在 stdout 是终端时发，
#  管道/CI 里是纯污染。
OSC133_TEXT_START = "\x1b]133;A\x07"
OSC133_TEXT_END = "\x1b]133;B\x07\x1b]133;C\x07"


class PlainSink:
    """明文终端渲染。verbose=False 对应 quiet 子 agent：不刷正文、不画计划，
    但工具调用行照常打印（配合 indent 显示子 agent 在干什么）。"""

    #  提示级别 → 语义 token（配色本身住在 theme，这里只管选哪一档）
    _NOTICE_TOKENS = {"info": "text.secondary", "warn": "status.warning", "error": "status.error"}

    def __init__(self, indent: str = "", verbose: bool = True) -> None:
        self.indent = indent
        self.verbose = verbose
        #  当前正文块是否已打开 OSC 133 锚点（TextEnd 负责收束配对）
        self._osc133_open = False
        #  按事件类型分发；不认识的事件（将来新增的）静默忽略——
        #  旧前端遇到新事件不该崩，这是事件协议的向后兼容底线
        self._handlers: dict[type, Callable[[Any], None]] = {
            TextDelta: self._text_delta,
            TextEnd: self._text_end,
            RequestStarted: self._request_started,
            ToolPending: self._tool_pending,
            ToolPurpose: self._tool_purpose,
            ToolCompleted: self._tool_completed,
            PlanUpdated: self._plan,
            SteerAccepted: self._steer,
            Notice: self._notice,
        }

    def emit(self, event: UIEvent) -> None:
        handler = self._handlers.get(type(event))
        if handler is not None:
            handler(event)

    def _request_started(self, event: RequestStarted) -> None:
        """等模型时给一行提示——但只在有人盯着终端看的时候。

        明文 sink 没有活区可用，只能实打实占一行 scrollback。这在管道 / CI /
        一次性执行里是纯污染（`xy "改一下" > out.txt` 不该混进进度行），
        但在 --no-tui 或缺依赖的交互场景里，一行提示总好过完全静默。
        以 stdout 是不是终端为准：不是终端就等于没人在看。
        """
        if self.verbose and sys.stdout.isatty():
            print(ui.secondary(f"{self.indent}· {event.model} 思考中…"))

    def _text_delta(self, event: TextDelta) -> None:
        if self.verbose:
            if not self._osc133_open and sys.stdout.isatty():
                #  正文块起点的零宽锚点，每块只打一次（终态在 _text_end 收束）
                print(end=OSC133_TEXT_START)
                self._osc133_open = True
            print(event.text, end="", flush=True)

    def _text_end(self, event: TextEnd) -> None:
        if self.verbose:
            if self._osc133_open:
                print(end=OSC133_TEXT_END)
                self._osc133_open = False
            print()

    def _tool_pending(self, event: ToolPending) -> None:
        print(
            self.indent
            + ui.accent(f"⚙ {event.name}")
            + ui.secondary(f" {args_preview(event.name, event.args, self._reserve(event.name))}")
        )

    def _reserve(self, name: str = "") -> int:
        """本行在预览文本之前已占掉的列数：缩进 + 符号 + 空格（+ 工具名）。"""
        return len(self.indent) + len(name) + 4

    def _tool_purpose(self, event: ToolPurpose) -> None:
        print(self.indent + ui.secondary(f"  目的：{event.purpose}"))

    def _tool_completed(self, event: ToolCompleted) -> None:
        first_line = event.output.split("\n", 1)[0]
        print(self.indent + ui.secondary(f"  ↳ {ui.fit(first_line, self._reserve())}"))

    def _plan(self, event: PlanUpdated) -> None:
        if not self.verbose:
            return
        if event.explanation:
            print(self.indent + ui.secondary(f"  ✎ {ui.fit(event.explanation, self._reserve())}"))
        for item in event.plan:
            mark = _PLAN_MARKS[item["status"]]
            row = f"  {mark} {item['step']}"
            if item["status"] == "in_progress":
                print(self.indent + ui.accent(row))
            elif item["status"] == "completed":
                print(self.indent + ui.secondary(row))
            else:
                print(self.indent + row)

    def _steer(self, event: SteerAccepted) -> None:
        #  确认"插话已进入本轮"：用户敲的时候只有 tty 回显，没有这行就无从
        #  分辨插话是生效了还是丢了
        print(self.indent + ui.secondary(f"  ↳ 插话已加入本轮：{ui.fit(event.text, self._reserve())}"))

    def _notice(self, event: Notice) -> None:
        print(ui.styled(self._NOTICE_TOKENS[event.level], event.text))


class NullSink:
    """全部丢弃：测试与 eval 场景不需要任何终端输出。"""

    def emit(self, event: UIEvent) -> None:
        pass


class JsonlSink:
    """headless 流式渲染（--output-format stream-json）：每个事件一行 JSON。

    events.py 预留的"to_dict() + JSON 就是线上协议"在这里兑现——消费方按
    kind 分发，不认识的 kind 跳过即可（与 PlainSink 同一条向后兼容底线）。
    逐行 flush：下游往往是 `| jq` 或逐行读的编排器，攒缓冲区等于白流式。
    """

    def emit(self, event: UIEvent) -> None:
        print(json.dumps(event.to_dict(), ensure_ascii=False), flush=True)
