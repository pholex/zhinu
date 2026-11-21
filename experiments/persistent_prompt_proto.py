"""常驻输入区原型：模型作答期间输入框与状态行不消失。

不调模型、不接 agent，只回答一个问题：在不进 alt-screen 的前提下，
prompt_toolkit 能不能把「输入框 + 一行状态」贯穿整轮，同时正文照常落进
终端原生 scrollback。数据源是脚本内置的假流式文本。

    .venv/bin/python experiments/persistent_prompt_proto.py            # 手动体验
    .venv/bin/python experiments/persistent_prompt_proto.py --selfcheck  # tmux 里自动验收

做法（三条，缺一条就退回「时隐时现」）：

1. 一个 PromptSession 从启动活到退出。回车不结束应用，只取走文本并清空
   输入框——所以没有「提示符收起、再画出来」这一拍。
2. 假 agent 跑在工作线程，保持同步写法；它只改共享状态，不碰终端。
3. 界面线程按固定节拍把正文补打到输入区上方（进 scrollback，每个字符只落
   一次）。成行的整行落；没成行但已经铺满一屏宽的部分按「软折行」落——
   写满一行后多写一个字符让终端自己折行，所以复制出去仍是一整行、缩放时
   终端也会替它重排。活区里只留最后不足一行的尾巴。
4. 缩放自己接管：窗口变窄时终端会把活区里的宽行重排成多行，库自带的擦除
   按旧行数回退、会漏擦留影；这里按新宽度算出多出来的行数再擦。

验收对应五条判据：全程输入区不消失、缩放无残影、高速输出不丢键、
退出后记录与预期逐字一致、多终端各过一遍（最后一条只能人工）。
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE = """\
先看结论：这次改动只动输入层，正文渲染路径不变。

## 为什么要常驻

输入框跟着提示符生灭的话，模型一跑它就没了，状态也跟着没了。把应用循环
拉长到整个会话，底部这块才算真的「常驻」。下面是一段代码：

```python
def commit(lines: list[str]) -> None:
    for line in lines:
        write(line + "\\n")   # 每个字符只落一次
```

一行很长的正文用来看折行：{long}

- 列表项一：English words mixed with 中文、全角标点，以及 emoji 👍。
- 列表项二：`inline code` 与 **粗体** 标记原样保留。

"""

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MODEL = "demo-model"
MARK_STATUS = MODEL         # 自检靠这两个记号在屏幕上找状态行与输入行
MARK_PROMPT = "›"
IDLE = "就绪"
DIFF = "+169 −37"           # 改动量是假数据；分支名启动时真读一次
CONTEXT_CHARS = 20000       # 假设的上下文容量，只为让用量数字会动

#  底部的配色：分隔线与说明文字压暗，提示符与 spinner 用强调色，状态行不反白。
STYLE = {
    "rule": "fg:ansibrightblack",
    "dim": "fg:ansibrightblack",
    "accent": "fg:ansicyan",
    "mark": "fg:ansicyan bold",        # 不叫 prompt：库把整段提示文字都归在那个类下
    "mode": "fg:ansiyellow",
    "ask": "fg:ansiyellow",
    "ask.key": "fg:ansiyellow bold",
    "placeholder": "fg:ansibrightblack italic",
    "bottom-toolbar": "noreverse",
    "bottom-toolbar.text": "noreverse",
}


def sample_text(chars: int) -> str:
    unit = SAMPLE.format(long="长行内容，" * 40)
    out, n = [], 0
    for i in itertools.count(1):
        block = f"【第 {i} 段】\n{unit}"
        out.append(block)
        n += len(block)
        if n >= chars:
            break
    return "".join(out)[:chars].rstrip("\n") + "\n【完】\n"


@dataclass
class Approval:
    question: str
    done: threading.Event = field(default_factory=threading.Event)
    answer: str = ""


@dataclass
class Shared:
    """工作线程写、界面线程读。所有字段都在 lock 下动。"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    pending: list[str] = field(default_factory=list)   # 已成行、待补打
    tail: str = ""                                      # 未成行的尾巴
    tool: str = ""                                      # 进行中的工具，空串=没有
    approval: Approval | None = None
    steers: list[str] = field(default_factory=list)
    running: bool = False
    cancel: threading.Event = field(default_factory=threading.Event)
    started: float = 0.0
    emitted: int = 0
    turns: int = 0

    def feed(self, chunk: str) -> None:
        with self.lock:
            self.emitted += len(chunk)
            *lines, self.tail = (self.tail + chunk).split("\n")
            self.pending.extend(lines)

    def line(self, text: str) -> None:
        """整行直接入队（工具行、回显行），先把尾巴顶成一行。"""
        with self.lock:
            if self.tail:
                self.pending.append(self.tail)
                self.tail = ""
            self.pending.append(text)


def fake_turn(shared: Shared, *, chars: int, cps: int, approve: bool) -> None:
    """一轮假作答：正文 → 工具 →（可选确认）→ 正文。全同步，像真 agent。"""
    text = sample_text(chars)
    half = text.find("\n", len(text) // 2) + 1
    step = max(1, cps // 100)          # 每 10ms 吐一小块，近似逐 token
    try:
        for part, after in ((text[:half], True), (text[half:], False)):
            for i in range(0, len(part), step):
                if shared.cancel.is_set():
                    shared.line("⎿ 已打断")
                    return
                shared.feed(part[i:i + step])
                time.sleep(step / cps)
            if not after:
                break
            with shared.lock:
                shared.tool = "bash  sleep 1 && echo ok"
            if approve:
                ask = Approval("要运行 bash：sleep 1 && echo ok 吗？")
                with shared.lock:
                    shared.approval = ask
                while not ask.done.wait(0.05):
                    if shared.cancel.is_set():
                        break
                with shared.lock:
                    shared.approval = None
                if ask.answer != "y":
                    with shared.lock:
                        shared.tool = ""
                    shared.line("● bash  已拒绝")
                    continue
            shared.cancel.wait(1.0)
            with shared.lock:
                shared.tool = ""
            shared.line("● bash  sleep 1 && echo ok")
            shared.line("  ⎿ ok")
            for s in drain(shared):
                shared.line(f"↳ 插话已送达：{s}")
    finally:
        for s in drain(shared):
            shared.line(f"↳ 插话（轮末）：{s}")
        with shared.lock:
            if shared.tail:
                shared.pending.append(shared.tail)
                shared.tail = ""
            shared.running = False


def drain(shared: Shared) -> list[str]:
    with shared.lock:
        out, shared.steers = shared.steers, []
    return out


def split_rows(text: str, cols: int) -> tuple[list[str], str]:
    """把超过一屏宽的文本切成若干「铺满的行」和不足一行的余下部分。

    按终端的折行规则算：宽字符放不进行尾最后一格就整个挪到下一行。
    只有余下部分确实非空时才切——铺满的行要靠「下一个字符」触发终端折行。
    """
    from prompt_toolkit.utils import get_cwidth

    rows, start, width = [], 0, 0
    for i, ch in enumerate(text):
        w = get_cwidth(ch)
        if width + w > cols:
            rows.append(text[start:i])
            start, width = i, 0
        width += w
    return rows, text[start:]


def erase_live(app, *, cont: int = 0, up: int | None = None) -> None:
    """把光标挪到输入区首行并擦掉整个输入区，渲染器归零。只写缓冲、不冲刷。

    擦除不用库自带的「回到首行清到屏末」：输入区恰好从屏幕第一行开始时，
    有的终端复用器会把整屏当作「清屏」推进历史，留下一帧输入区的残影。
    这里首行单独清行，其余行再清到屏末，绕开它。

    cont 非零表示首行是上一行软折下来的续行，行首那个字符（宽 cont 格）已经
    落在终端里：这时不能从行首清——那会抹掉上一行的折行标记、把一个逻辑行
    拆成两行——只从它后面清到屏末。up 给出时按它回退（缩放后行数变了）。
    """
    out, renderer = app.output, app.renderer
    out.write_raw("\r")
    out.cursor_up(renderer._cursor_pos.y if up is None else up)
    if cont:
        out.write_raw(f"\x1b[{cont}C\x1b[J\r")
    else:
        out.write_raw("\x1b[2K\x1b[1B\x1b[J\x1b[1A")
    out.reset_attributes()
    out.enable_autowrap()
    renderer.reset()
    app._request_absolute_cursor_position()


def redraw_fresh(app, render=None) -> None:
    """擦过之后的第一帧。库在这一帧前会再从行首清一次屏，那一下同样会抹掉
    续行的折行标记；我们已经清过，所以这期间让它空转。"""
    out = app.output
    erase_down, out.erase_down = out.erase_down, lambda: None
    try:
        (render or app._redraw)()
    finally:
        out.erase_down = erase_down


def commit(app, data: str, *, sync: bool, cont: int = 0) -> None:
    """整块重画式的补打：擦掉输入区 → 写正文 → 重画输入区，攒成一次写出。

    会闪，只在活区变矮、需要把高度归位时用；平时走 commit_in_place。
    用到渲染器的私有成员（光标位置、上一帧、强制重画），正式实现时要么
    收进一个渲染器子类，要么接受随库版本核对。
    """
    out = app.output
    flush, out.flush = out.flush, lambda: None      # 攒到最后一次写出
    try:
        if sync:
            out.write_raw("\x1b[?2026h")
        erase_live(app, cont=cont)
        out.write_raw(data)
        redraw_fresh(app)
        if sync:
            out.write_raw("\x1b[?2026l")
    finally:
        out.flush = flush
        out.flush()


def commit_in_place(app, data: str, rows: int, *, sync: bool, cont: int) -> None:
    """不擦除的补打：正文直接盖在输入区最上面的 rows 行上，输入区整体下移。

    `commit` 是「整块擦掉再重画」，每秒二十次，在不支持同步输出的终端上
    底部会闪。这里不清屏：正文逐行覆盖旧内容（行尾清到行末），然后告诉
    渲染器「屏幕上现在是旧的一帧去掉前 rows 行」，让它照常做差量重画——
    没变的格子一个字节都不写，状态行自然就不闪了。
    """
    out, renderer = app.output, app.renderer
    prev = renderer._last_screen
    if prev is None:
        commit(app, data, sync=sync, cont=cont)
        return
    flush, out.flush = out.flush, lambda: None
    try:
        if sync:
            out.write_raw("\x1b[?2026h")
        out.write_raw("\r")
        out.cursor_up(renderer._cursor_pos.y)
        out.reset_attributes()
        out.enable_autowrap()
        out.write_raw(data)
        for name in ("data_buffer", "zero_width_escapes"):
            old = getattr(prev, name)
            new = type(old)(old.default_factory)
            for y, row in old.items():
                if y >= rows:
                    new[y - rows] = row
            setattr(prev, name, new)
        prev.height = max(0, prev.height - rows)
        renderer._cursor_pos = type(renderer._cursor_pos)(x=0, y=0)
        renderer._last_style = None
        app._request_absolute_cursor_position()
        app._redraw()
        if sync:
            out.write_raw("\x1b[?2026l")
    finally:
        out.flush = flush
        out.flush()


def row_text(screen, y: int) -> str:
    """上一帧第 y 行实际画到终端上的文字（去掉行尾空白）。"""
    row = screen.data_buffer.get(y) or {}
    return "".join(row[x].char for x in sorted(row) if row[x].width).rstrip()


def rows_after_reflow(screen, y: int, cols: int) -> int:
    """上一帧第 y 行在新宽度下占几行：终端会把比窗口宽的行重排成多行。"""
    row = screen.data_buffer.get(y) or {}
    width = max((x + c.width for x, c in row.items() if c.char.strip()), default=0)
    return max(1, -(-width // cols))


def run_ui(args: argparse.Namespace) -> int:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.filters import Condition
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    from prompt_toolkit.utils import get_cwidth

    shared = Shared()
    transcript: list[str] = []          # 我们「打算」落进 scrollback 的每一行
    stats = {"ticks": 0, "commits": 0, "max_lag_ms": 0.0, "max_commit_ms": 0.0}
    last_ctrl_c = [0.0]
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, timeout=2,
        ).stdout.decode("utf-8", errors="replace").strip() or "main"
    except (OSError, subprocess.SubprocessError):
        branch = "main"
    #  tail  活区首行里还没落下的尾巴
    #  open  当前逻辑行已经落进终端的部分
    #  carry open 的末尾里、和尾巴同在一个物理行上的那一截：它已经落下，但每帧
    #        随首行一起重画（只有缩放把行重排之后才会非空）
    #  cont  首行是续行时，行首字符的宽度（见 commit）
    view = {"tail": "", "open": "", "carry": "", "cont": 0, "rows": 0, "size": None}
    session: PromptSession[str] = PromptSession(
        erase_when_done=True, reserve_space_for_menu=0, style=Style.from_dict(STYLE))

    #  不向终端查询光标位置：库拿到「光标下方还剩几行」后会把输入区撑到屏幕
    #  底部，屏幕没写满时输入行和下面的分隔线之间就隔着一片空白。不查，输入区
    #  就按自身高度紧凑排布；顺带省掉每次补打一问一答的往返。
    #  但库只在「高度已知」时才画下方那一栏，所以把查询换成直接报一个最小高度。
    def height_known() -> None:
        session.app.renderer._min_available_height = 1

    session.app.renderer.request_absolute_cursor_position = height_known

    def columns() -> int:
        return max(8, session.app.output.get_size().columns)

    def clip(text: str, cols: int) -> str:
        """按显示宽度截断，超出时以省略号收尾。"""
        if sum(get_cwidth(c) for c in text) <= cols:
            return text
        out, width = "", 0
        for ch in text:
            width += get_cwidth(ch)
            if width > cols - 1:
                break
            out += ch
        return out + "…"

    def take() -> tuple[str, int]:
        """取走该落的正文，返回（要写的字节，占了几行）；顺手记进预期记录。

        只在界面线程调。每个物理行写完都清到行末，因为它是盖在旧内容上的。
        """
        cols = columns()
        carry = view["carry"]
        with shared.lock:
            lines, shared.pending = shared.pending, []
            rows, rest = split_rows(carry + shared.tail, cols) if not lines else ([], "")
            if rows:
                shared.tail = rest
            view["tail"] = shared.tail

        def laid(text: str) -> tuple[str, int]:
            """一个逻辑行从行首写起的字节与占用行数；铺满的行靠终端自己折。"""
            wrapped, last = split_rows(text, cols)
            body = "".join(
                r if sum(get_cwidth(c) for c in r) >= cols else r + "\x1b[K" for r in wrapped)
            return body + last, len(wrapped)

        data, used = "", 0
        for line in lines:
            transcript.append(view["open"] + line)
            body, n = laid(carry + line)        # carry 已在屏上，原样重写一遍不碍事
            data += body + "\x1b[K\r\n"
            used += n + 1
            carry = view["open"] = view["carry"] = ""
            view["cont"] = 0
        if rows:                        # 铺满的行 + 下一个字符触发折行，再回到行首
            body, n = laid("".join(rows) + rest[0])
            data += body + "\r"
            used += n
            view["open"] += "".join(rows)[len(carry):]
            view["carry"] = ""
            view["cont"] = get_cwidth(rest[0])
        return data, used

    def resync() -> None:
        """窗口大小变了：按终端重排后的样子找到输入区的新起点，擦掉重画。

        终端会把每个逻辑行按新宽度重新折行。输入区里的行各自是一个逻辑行，
        比新窗口宽的会变成多行；首行若是续行，则连同上面已落下的部分一起
        重排——尾巴可能被并到上一行行尾，也可能被挤到下面几行。这里用同一套
        折行规则重算一遍：重排后落在「最后一个物理行」之前的字符都算已落下，
        最后一行那一截（carry + 尾巴）留给首行继续画。
        """
        app = session.app
        size = view["size"] = app.output.get_size()
        cols = max(8, size.columns)
        screen, pos = app.renderer._last_screen, app.renderer._cursor_pos
        if screen is None:
            return
        drawn = row_text(screen, 0)
        with shared.lock:
            shown = drawn[len(view["carry"]):] if drawn.startswith(view["carry"]) else ""
            while shown and not shared.tail.startswith(shown):
                shown = shown[:-1]
            text = view["open"] + shown
            rows, last = split_rows(text, cols)
            above = sum(len(r) for r in rows)           # 重排后落在最后一行之前的字符数
            moved = max(0, above - len(view["open"]))   # 其中原本还在尾巴里的
            view["open"] += shared.tail[:moved]
            shared.tail = shared.tail[moved:]
            view["carry"] = view["open"][above:]
            view["tail"] = shared.tail
            view["cont"] = get_cwidth(last[0]) if above and last else 0
        up = 1 + sum(rows_after_reflow(screen, y, cols) for y in range(1, pos.y))
        erase_live(app, cont=view["cont"], up=up + pos.x // cols)

    #  缩放的处理卡在渲染入口：终端一缩放就已经把屏上的内容重排了，在我们
    #  按新布局对齐之前，库哪怕只按旧位置画一帧，后面的推算就全错位。所以
    #  每次重画前先看窗口大小变没变，变了就先对齐、再画这一帧。
    plain_render = session.app.renderer.render

    def render(app, layout, is_done: bool = False) -> None:
        if is_done or view["size"] is None or app.output.get_size() == view["size"]:
            plain_render(app, layout, is_done)
            return
        resync()
        redraw_fresh(app, lambda: plain_render(app, layout, is_done))

    session.app.renderer.render = render
    session.app._on_resize = session.app._redraw

    def start_turn(text: str) -> None:
        shared.cancel.clear()
        with shared.lock:
            shared.running = True
            shared.started = time.monotonic()
            shared.turns += 1
        shared.line(f"{MARK_PROMPT} {text}")
        threading.Thread(
            target=fake_turn, name="fake-agent", daemon=True,
            kwargs=dict(shared=shared, chars=args.chars, cps=args.cps, approve=args.approve),
        ).start()

    kb = KeyBindings()
    asking = Condition(lambda: shared.approval is not None)

    @kb.add("enter", filter=~asking)
    def _(event):
        buf = event.current_buffer
        text = buf.text.strip()
        if not text:
            return
        buf.append_to_history()
        buf.reset()
        if text in ("/exit", "/quit"):
            event.app.exit(result="")
        elif shared.running:
            with shared.lock:
                shared.steers.append(text)
        else:
            start_turn(text)

    @kb.add("y", filter=asking)
    @kb.add("n", filter=asking)
    def _(event):
        ask = shared.approval
        if ask is not None:
            ask.answer = event.key_sequence[0].key
            ask.done.set()

    @kb.add("<any>", filter=asking)
    def _(event):                       # 确认挂起时吞掉其它键，不漏进输入框
        pass

    @kb.add("c-c")
    def _(event):
        if shared.running:
            shared.cancel.set()
        elif time.monotonic() - last_ctrl_c[0] < 1.5:
            event.app.exit(result="")
        else:
            last_ctrl_c[0] = time.monotonic()
            event.current_buffer.reset()

    @kb.add("escape", eager=True, filter=Condition(lambda: shared.running))
    def _(event):
        shared.cancel.set()

    @kb.add("c-d")
    def _(event):
        event.app.exit(result="")

    def message() -> FormattedText:
        """输入行上方的活区，自上而下：未成行尾巴、空一行、进行中的事、分隔线。"""
        with shared.lock:
            tool, ask, running = shared.tool, shared.approval, shared.running
            steers, started = list(shared.steers), shared.started
        tail = view["tail"]             # 取节拍时的快照，与已落正文同步切换
        cols = columns()
        frame = SPINNER[int(time.monotonic() * 10) % len(SPINNER)]
        out: list[tuple[str, str]] = []
        out.append(("", view["carry"] + tail + "\n"))   # 这一行常在（空着也占位）
        if ask or running or steers:
            out.append(("", "\n"))
        if ask:
            out.append(("class:ask", "▌ " + clip(ask.question, cols - 2) + "\n"))
            out.append(("class:ask", "▌ "))
            out.append(("class:ask.key", "y"))
            out.append(("class:dim", " 允许 · "))
            out.append(("class:ask.key", "n"))
            out.append(("class:dim", " 拒绝\n"))
        elif running:
            #  一行说清「在干什么、干了多久、出了多少」：动词用强调色，括号里压暗
            secs = int(time.monotonic() - started)
            took = f"{secs // 60}m {secs % 60}s" if secs >= 60 else f"{secs}s"
            with shared.lock:
                emitted = shared.emitted
            size = f"{emitted / 1000:.1f}k" if emitted >= 1000 else str(emitted)
            doing = clip(tool or "作答中…", max(8, cols - 30))
            out.append(("class:accent", f"{frame} {doing}"))
            out.append(("class:dim", f" ({took} · ↓ {size} 字)\n"))
        for text in steers:
            out.append(("class:dim", clip(f"  ↳ 待送达：{text}", cols) + "\n"))
        if ask or running or steers:
            out.append(("", "\n"))
        out.append(("class:rule", "─" * cols + "\n"))
        out.append(("class:mark", f"{MARK_PROMPT} "))
        view["rows"] = sum(text.count("\n") for _, text in out)
        return FormattedText(out)

    def placeholder() -> FormattedText:
        if shared.approval is not None:
            return FormattedText([("class:placeholder", "先回答上面的确认")])
        hint = "继续输入可以插话" if shared.running else "输入消息，回车发送"
        return FormattedText([("class:placeholder", hint)])

    def toolbar() -> FormattedText:
        """输入行下方：分隔线 + 一行说明。

        左边是当前模式与此刻能按的键；右边是分支与改动量、上下文用量、模型名。
        放不下时从最不要紧的丢起：按键说明 → 改动量 → 分支 → 模型名，
        上下文用量留到最后。只占一行，不折行。
        """
        cols = columns()
        with shared.lock:
            emitted = shared.emitted
        used = min(99, emitted * 100 // CONTEXT_CHARS)     # 假数据：按已出字数折算
        mode = "▸▸ 逐项确认" if args.approve else "▸▸ 自动放行"
        keys = " (shift+tab 切换)" + (
            " · esc 打断 · enter 插话" if shared.running else " · enter 发送 · ctrl-c ×2 退出")
        ctx = f"上下文 {used}%"
        width = lambda t: sum(get_cwidth(c) for c in t)
        branch_name = clip(branch, 28)
        for show_keys, git, show_model in (
            (True, f"{branch_name} {DIFF}", True), (False, f"{branch_name} {DIFF}", True),
            (False, branch_name, True), (False, "", True), (False, "", False),
        ):
            left = keys if show_keys else ""
            before = (git + " · ") if git else ""
            after = f" · {MODEL} " if show_model else " "
            gap = cols - 2 - width(mode) - width(left) - width(before + ctx + after)
            if gap >= 2:
                break
        else:
            left, before, after, gap = "", "", "", max(0, cols - 2 - width(mode) - width(ctx))
        return FormattedText([
            ("class:rule", "─" * cols + "\n"),
            ("", "  "),
            ("class:mode", mode),
            ("class:dim", left + " " * gap + before),
            ("class:ask" if used >= 70 else "class:dim", ctx),   # 用量吃紧时变色提醒
            ("class:dim", after),
        ])

    async def pump() -> None:
        """界面线程的节拍：补打成行正文、驱动活区重画。"""
        interval = 1.0 / args.hz
        app = session.app
        out = app.output
        prev = time.monotonic()
        was_running = False
        tall = 0
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            stats["ticks"] += 1
            stats["max_lag_ms"] = max(stats["max_lag_ms"], (now - prev - interval) * 1e3)
            prev = now
            running = shared.running
            if app.output.get_size() != view["size"]:
                app._redraw()           # 信号可能还在路上：落笔前先对齐窗口大小
            cont = view["cont"]         # 落笔前的状态：此刻首行是不是续行
            data, used = take()
            if data:
                t0 = time.monotonic()
                if args.erase:
                    commit(app, data, sync=args.sync, cont=cont)
                else:
                    commit_in_place(app, data, used, sync=args.sync, cont=cont)
                stats["commits"] += 1
                stats["max_commit_ms"] = max(
                    stats["max_commit_ms"], (time.monotonic() - t0) * 1e3)
            elif running or was_running:
                app.invalidate()
            #  活区变矮（一轮结束、确认答完）时库不会收回空出来的行，输入行和
            #  下面的分隔线之间会留空。这种时刻很少，整块重画一次把高度归位。
            if view["rows"] < tall:
                commit(app, "", sync=args.sync, cont=view["cont"])
            tall = view["rows"]
            was_running = running

    async def main() -> None:
        task: asyncio.Task[None] | None = None

        def pre_run() -> None:
            nonlocal task
            view["size"] = session.app.output.get_size()
            task = session.app.create_background_task(pump())
            if args.auto:
                asyncio.get_running_loop().call_later(0.3, start_turn, args.auto)

        await session.prompt_async(
            message, key_bindings=kb, bottom_toolbar=toolbar, placeholder=placeholder,
            pre_run=pre_run)
        if task is not None:
            task.cancel()

    asyncio.run(main())
    shared.cancel.set()
    with shared.lock:                   # 退出时还没落的直接打出来，记录不缺尾
        rest, shared.pending = shared.pending, []
        if shared.tail:
            rest.append(shared.tail)
            shared.tail = ""
    for line in rest:
        transcript.append(view["open"] + line)
        view["open"] = ""
        print(line)
    if args.transcript_out:
        Path(args.transcript_out).write_text(
            "\n".join(transcript) + "\n", encoding="utf-8")
    if args.stats_out:
        Path(args.stats_out).write_text(
            " ".join(f"{k}={v:.1f}" if isinstance(v, float) else f"{k}={v}"
                     for k, v in stats.items()) + f" emitted={shared.emitted}\n",
            encoding="utf-8")
    return 0


# --------------------------------------------------------------------------
# 自检：在 tmux 里真跑一遍，按判据 1–4 给结论。判据 5（多终端）只能人工。
# --------------------------------------------------------------------------

def tmux(*argv: str) -> str:
    done = subprocess.run(
        ["tmux", "-L", "xy-proto", *argv], capture_output=True, check=False)
    return done.stdout.decode("utf-8", errors="replace")


def screen(session: str) -> str:
    return tmux("capture-pane", "-p", "-t", session)


def history(session: str) -> list[str]:
    raw = tmux("capture-pane", "-p", "-J", "-S", "-", "-t", session)
    return [line.rstrip() for line in raw.splitlines() if line.strip()]


def selfcheck(args: argparse.Namespace) -> int:
    work = Path(tempfile.mkdtemp(prefix="xy-proto-"))
    want, stat = work / "transcript.txt", work / "stats.txt"
    name = f"proto{os.getpid()}"
    typed = "typed-while-streaming-0123456789"
    cmd = " ".join(shlex.quote(p) for p in [
        sys.executable, str(Path(__file__).resolve()),
        "--chars", str(args.chars), "--cps", str(args.cps), "--hz", str(args.hz),
        "--transcript-out", str(want), "--stats-out", str(stat),
        *([] if args.sync else ["--no-sync"]),
        *(["--erase"] if args.erase else []),
    ])
    tmux("kill-server")
    tmux("new-session", "-d", "-s", name, "-x", "100", "-y", "30", cmd)
    tmux("set-option", "-t", name, "remain-on-exit", "on")
    tmux("set-option", "-t", name, "history-limit", "50000")
    time.sleep(1.5)
    tmux("send-keys", "-t", name, "-l", "开始")
    tmux("send-keys", "-t", name, "Enter")

    deadline = time.monotonic() + args.chars / args.cps + 20
    seen = missing = 0
    widths = itertools.cycle([100, 72, 120, 60, 90] if args.resize else [100])
    sent_typing = False
    while time.monotonic() < deadline:
        time.sleep(0.25)
        view = screen(name)
        if "enter 发送" in view and seen > 4:
            break
        seen += 1
        if MARK_STATUS not in view or MARK_PROMPT not in view:
            missing += 1
            (work / f"missing-{seen}.txt").write_text(view, encoding="utf-8")
        if args.resize and seen % 4 == 0:
            tmux("resize-window", "-t", name, "-x", str(next(widths)), "-y", "30")
        if not sent_typing and seen == 6:      # 流式中途逐键敲一行，轮末应原样送达
            for ch in typed:
                tmux("send-keys", "-t", name, "-l", ch)
            tmux("send-keys", "-t", name, "Enter")
            sent_typing = True
    time.sleep(0.5)
    tmux("send-keys", "-t", name, "-l", "/exit")
    tmux("send-keys", "-t", name, "Enter")
    for _ in range(40):
        time.sleep(0.25)
        if tmux("display-message", "-p", "-t", name, "#{pane_dead}").strip() == "1":
            break
    got = [ln for ln in history(name) if not ln.startswith("Pane is dead")]
    if args.keep:
        (work / "got.txt").write_text("\n".join(got) + "\n", encoding="utf-8")
    tmux("kill-server")

    if not want.exists():
        print("FAIL 原型没有正常退出，未写出预期记录；末屏：\n" + "\n".join(got[-15:]))
        return 1
    expect = [ln.rstrip() for ln in want.read_text(encoding="utf-8").splitlines()
              if ln.strip()]
    extra = [ln for ln in got if ln not in set(expect)]
    lost = [ln for ln in expect if ln not in set(got)]
    exact = got == expect
    keys_ok = any(typed in ln for ln in expect)
    results = [
        ("1 输入区与状态行全程在场", missing == 0, f"{seen} 次抽样中缺席 {missing} 次"),
        ("2 scrollback 无残影" + ("（含缩放）" if args.resize else "（未缩放）"),
         not extra, f"多出 {len(extra)} 行"),
        ("3 流式期间敲键不丢", keys_ok, f"敲入 {len(typed)} 键，{'原样送达' if keys_ok else '未在记录中找到'}"),
        ("4 留下的记录与预期逐行一致", exact,
         f"预期 {len(expect)} 行，实得 {len(got)} 行，缺 {len(lost)} 行"),
    ]
    for title, ok, note in results:
        print(f"{'PASS' if ok else 'FAIL'}  {title}：{note}")
    print("统计  " + stat.read_text(encoding="utf-8").strip())
    for label, rows in (("多出的行", extra), ("缺失的行", lost)):
        for row in rows[:5]:
            print(f"  {label}: {row[:90]}")
    print(f"现场  {work}")
    return 0 if all(ok for _, ok, _ in results) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chars", type=int, default=6000, help="每轮假正文的字符数")
    ap.add_argument("--cps", type=int, default=400, help="每秒吐多少字符")
    ap.add_argument("--hz", type=int, default=20, help="补打与重画的节拍")
    ap.add_argument("--approve", action="store_true", help="工具前弹一次确认")
    ap.add_argument("--auto", default="", help="启动后自动发这一句")
    ap.add_argument("--no-sync", dest="sync", action="store_false",
                    help="不用同步输出序列包裹补打")
    ap.add_argument("--erase", action="store_true",
                    help="补打改回「整块擦掉再重画」（对照用，会闪）")
    ap.add_argument("--transcript-out", default="")
    ap.add_argument("--stats-out", default="")
    ap.add_argument("--selfcheck", action="store_true", help="在 tmux 里自动验收")
    ap.add_argument("--keep", action="store_true", help="自检时把实得记录也留在现场目录")
    ap.add_argument("--no-resize", dest="resize", action="store_false",
                    help="自检时不缩放窗口")
    args = ap.parse_args()
    return selfcheck(args) if args.selfcheck else run_ui(args)


if __name__ == "__main__":
    raise SystemExit(main())
