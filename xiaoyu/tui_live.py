"""常驻输入区前端（可选，`XIAOYU_LIVE_INPUT=1`）：模型作答期间输入框与状态行不消失。

`tui.py` 的前端里，输入行只在「轮到你说话」时存在，模型一跑它就没了，运行期
插话靠另一条线程逐键读。这里换一种占用终端的方式，渲染层、按键表、确认菜单
全部沿用 `tui.py` 的：

- **输入区从启动活到退出**。回车不结束 prompt，只取走文本；作答期间回车是插话。
- **agent 跑在工作线程**，仍是同步写法；主线程只管输入区。打断走
  `Agent.interrupt()`（终端在逐键模式，Ctrl-C 是按键不是信号）。
- **仍然不进 alt-screen**。所有输出先进 `LiveWriter`，由主线程按节拍补打到输入区
  上方、落进终端原生 scrollback，每个字符只落一次。没成行但已铺满一屏宽的部分按
  软折行落（写满一行后多写一个字符让终端自己折），复制出去仍是一整行。
- **等人拿主意的事嵌在输入区里**：确认菜单、提问面板画在输入行上方，按键与画面
  还是 `tui.py` 那一份（`MenuHost` 接缝），只是换了住处；规则编辑、附言、拒绝理由、
  自由回答、密码这些单行追问直接借用输入行，半截草稿先收起、问完放回。
- **要独占终端的事先让位**：斜杠命令、`!` 命令执行期间输入区整个收起
  （`LiveScreen.suspended`），做完再画回来。

终端细节上踩实的几条（改动前先读）：

- 补打不擦除、原地覆盖：正文直接盖在输入区最上面几行上，再让渲染器对着「旧的
  一帧去掉前几行」做差量重画。整块擦掉再画在不支持同步输出的终端上会闪。
- 输入区从屏幕首行开始时，「回到首行清到屏末」会被终端复用器当成清屏、把整屏
  推进历史；所以首行单独清行，其余行再清到屏末。
- 对续行（上一行软折下来的行）从行首清除会抹掉上一行的折行标记，把一个逻辑行
  拆成两行；所以续行只从行首字符之后清。渲染器重画第一帧前自带的那次清屏同理，
  已经清过的场合让它空转。
- 缩放：窗口一变，终端就已经把屏上内容按新宽度重排了。每次重画前先看窗口大小，
  变了就按同一套折行规则重算输入区的新起点再擦。空闲时缩放是干净的；流式输出
  中途缩放可能弄花正在输出的那一段（复用过的行在部分终端里仍记着旧宽度，行数
  无法推算），这是已知限制。

用到 prompt_toolkit 渲染器的几个私有成员（光标位置、上一帧、强制重画），升级该库
时要随版本核对；依赖版本在 pyproject 里是精确锁定的。
"""

from __future__ import annotations

import collections
import contextlib
import io
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Iterator

from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, to_formatted_text
from prompt_toolkit.key_binding import (
    ConditionalKeyBindings,
    DynamicKeyBindings,
    KeyBindings,
    merge_key_bindings,
)
from prompt_toolkit.keys import Keys
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from rich.text import Text

from . import attention, keys, modes, suggest, theme, ui
from .agent import Agent
from .errors import Interrupted
from .tui import NoticeCapture, Tui, search_select

ENV_SWITCH = "XIAOYU_LIVE_INPUT"

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
#  补打用户原话时的行首标记。选字的约束：占一格（不是东亚宽字符）、没有彩色
#  表情的变体（否则各终端宽度与颜色都不由我们定）
_SAID_MARK = "✽ "
_TICK = 1 / 30

# ---------------------------------------------------------------------------
# 带转义序列的文本：宽度与折行
# ---------------------------------------------------------------------------

_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC
    r"|\x1b[@-Z\\^_]"  # 两字节序列
    r"|\x1b"
)
_SGR = re.compile(r"\x1b\[[0-9;:]*m")
_SGR_RESETS = frozenset({"\x1b[0m", "\x1b[m"})


def pieces(text: str) -> Iterator[tuple[str, bool]]:
    """拆成（片段, 是否转义序列）：转义序列整段给出，其余逐字符。"""
    pos = 0
    for match in _ESCAPE.finditer(text):
        for char in text[pos : match.start()]:
            yield char, False
        yield match.group(0), True
        pos = match.end()
    for char in text[pos:]:
        yield char, False


def char_width(char: str, column: int) -> int:
    if char == "\t":
        return 8 - column % 8
    if char < " " or char == "\x7f":
        return 0
    return get_cwidth(char)


def visible_width(text: str) -> int:
    column = 0
    for chunk, escape in pieces(text):
        if not escape:
            column += char_width(chunk, column)
    return column


def split_rows(text: str, cols: int, active: str = "") -> tuple[list[str], str, str]:
    """把超过一屏宽的文本切成若干「铺满的行」和不足一行的余下部分。

    按终端的折行规则算：宽字符放不进行尾最后一格就整个挪到下一行；转义序列
    不占宽度，跟着它后面的字符走。返回（铺满的行, 余下部分, 余下部分开头时
    生效的颜色序列）——余下部分日后单独画时要先补上这段颜色。`active` 是
    `text` 开头时已经生效的颜色序列。
    """
    rows: list[str] = []
    start = pos = column = 0
    at_row_start = active
    for chunk, escape in pieces(text):
        if escape:
            if _SGR.fullmatch(chunk):
                active = "" if chunk in _SGR_RESETS else active + chunk
        else:
            width = char_width(chunk, column)
            if width and column + width > cols:
                rows.append(text[start:pos])
                start, column, at_row_start = pos, 0, active
                width = char_width(chunk, 0)
            column += width
        pos += len(chunk)
    return rows, text[start:], at_row_start


def first_cell(text: str) -> tuple[str, int]:
    """开头到第一个占位字符为止的那一截，及该字符的宽度。"""
    taken, column = "", 0
    for chunk, escape in pieces(text):
        taken += chunk
        if not escape and (width := char_width(chunk, column)):
            return taken, width
    return taken, 0


def displayable(text: str) -> str:
    """给输入区首行画尾巴用：只留颜色序列，tab 展成空格，其余控制字符丢掉。"""
    out: list[str] = []
    column = 0
    for chunk, escape in pieces(text):
        if escape:
            if _SGR.fullmatch(chunk):
                out.append(chunk)
            continue
        width = char_width(chunk, column)
        if chunk == "\t":
            out.append(" " * width)
        elif width:
            out.append(chunk)
        column += width
    return "".join(out)


# ---------------------------------------------------------------------------
# 输出汇集
# ---------------------------------------------------------------------------


class LiveWriter:
    """输入区挂着期间顶替 `sys.stdout`：任何线程写进来的文字先攒着，由主线程
    按节拍补打到输入区上方。

    成行的进 `pending`，没成行的留在 `tail`。别的线程在某条线程的半行还开着时
    打进来的整行（比如主线程的一句提示）排到那半行前面，不和它粘在一起。
    `passthrough` 打开时原样写到真正的 stdout：输入区还没起来、已经收起让位、
    或者已经退出。
    """

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, real: Any) -> None:
        self.real = real
        self.lock = threading.Lock()
        self.pending: list[str] = []
        self.tail = ""
        self.passthrough = True
        self._tail_owner: int | None = None
        #  最近落下的一整行是不是空行（要不要再空一行由调用方据此决定）
        self.ends_blank = True

    def write(self, text: str) -> int:
        if not text:
            return 0
        if self.passthrough:
            return self.real.write(text)
        text = text.replace("\r\n", "\n").replace("\r", "")
        me = threading.get_ident()
        with self.lock:
            if self.tail and self._tail_owner != me and text.endswith("\n"):
                self.pending.extend(text[:-1].split("\n"))
                return len(text)
            *lines, self.tail = (self.tail + text).split("\n")
            self.pending.extend(lines)
            if lines or self.tail:
                self.ends_blank = not self.tail and not visible_width(lines[-1])
            self._tail_owner = me if self.tail else None
        return len(text)

    def flush(self) -> None:
        if self.passthrough:
            self.real.flush()

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.real.fileno()

    def writable(self) -> bool:
        return True

    def drain(self) -> None:
        """把还攒着的原样写到真正的 stdout（退出、让位前收尾）。"""
        with self.lock:
            lines, self.pending = self.pending, []
            tail, self.tail = self.tail, ""
        for line in lines:
            self.real.write(line + "\n")
        if tail:
            self.real.write(tail + "\n")
        self.real.flush()


# ---------------------------------------------------------------------------
# 输入区在终端里的位置管理
# ---------------------------------------------------------------------------


class LiveScreen:
    """管「输入区上方补打正文」这件事：补打、擦除、缩放对齐、让位。

    只在主线程（prompt_toolkit 的事件循环）里动终端；别的线程只经
    `suspended()` 请求让位。状态：

    - `open`   当前逻辑行已经落进终端的部分
    - `carry`  `open` 末尾有几个字符和尾巴同在一个物理行上：它们已经落下，但随
               输入区首行一起重画（只有缩放把行重排之后才非零）
    - `lead`   首行开头要先补上的颜色序列
    - `cont`   首行是续行时，行首字符的宽度
    """

    def __init__(self, app: Any, writer: LiveWriter) -> None:
        self.app = app
        self.writer = writer
        self.open = ""
        self.carry = 0
        self.lead = ""
        self.cont = 0
        self.tail = ""  # 上一拍取到的尾巴，首行照它画
        self.size: Any = None
        self.active = False
        self._paused = False
        self._gate = threading.RLock()
        self._loop: Any = None
        self._plain_render = app.renderer.render
        self.animate: Callable[[], bool] = lambda: False

    # ---- 接线 ----

    def install(self) -> None:
        """接管渲染入口、缩放与高度查询。在输入区的 pre_run 里调。"""
        import asyncio

        app = self.app
        self._loop = asyncio.get_running_loop()
        self.size = app.output.get_size()
        renderer = app.renderer

        #  不向终端查询光标位置：库拿到「光标下方还剩几行」后会把输入区撑到屏幕
        #  底部，屏幕没写满时输入行和下面的分隔线之间就隔着一片空白。但库只在
        #  「高度已知」时才画下方那一栏，所以把查询换成直接报一个最小高度。
        def height_known() -> None:
            renderer._min_available_height = 1  # noqa: SLF001 - 见模块说明末段

        renderer.request_absolute_cursor_position = height_known
        renderer.render = self._render
        app._on_resize = app._redraw  # noqa: SLF001 - 缩放交给渲染入口处理
        self.active = True
        app.create_background_task(self._pump())

    def first_row(self) -> str:
        """输入区首行该画的文字：已落下但同在这一行的那截 + 还没落下的尾巴。"""
        carried = self.open[len(self.open) - self.carry :] if self.carry else ""
        return displayable(self.lead + carried + self.tail)

    def cols(self) -> int:
        return max(8, self.app.output.get_size().columns)

    # ---- 节拍 ----

    async def _pump(self) -> None:
        import asyncio

        was_animating = False
        while True:
            await asyncio.sleep(_TICK)
            if self._paused or not self.active:
                continue
            with contextlib.suppress(Exception):
                self.tick()
                animating = self.animate()
                if animating or was_animating:
                    self.app.invalidate()
                was_animating = animating

    def tick(self, final: bool = False) -> None:
        app = self.app
        if app.output.get_size() != self.size:
            app._redraw()  # noqa: SLF001 - 信号可能还在路上：落笔前先对齐窗口大小
        cont = self.cont  # 落笔前的状态：此刻首行是不是续行
        data, used = self._take(final)
        if data:
            self._commit_in_place(data, used, cont)
        elif self.tail_changed:
            app.invalidate()
        #  输入区变矮（一轮结束、补全菜单收起、多行输入提交）时库不会收回空出来
        #  的行，输入行和下面的分隔线之间会留空。整块重画一次把高度归位。
        last = app.renderer._last_screen  # noqa: SLF001
        if last is not None and not final:
            size = app.output.get_size()
            wanted = app.layout.container.preferred_height(size.columns, size.rows).preferred
            if wanted < last.height:
                self._commit_erasing("")

    tail_changed = False

    def _take(self, final: bool) -> tuple[str, int]:
        """取走该落的正文，返回（要写的字节，占了几个物理行）。"""
        cols = self.cols()
        writer = self.writer
        carried = self.open[len(self.open) - self.carry :] if self.carry else ""
        loose = ""
        with writer.lock:
            lines, writer.pending = writer.pending, []
            if final and writer.tail:
                lines.append(writer.tail)
                writer.tail = ""
            rows: list[str] = []
            rest = active = ""
            if not lines:
                rows, rest, active = split_rows(carried + writer.tail, cols, self.lead)
                if rows:
                    writer.tail = rest
                elif writer.tail and not visible_width(writer.tail):
                    #  只有控制序列（标题、响铃、锚点）：不等换行，立刻放出去
                    loose, writer.tail = writer.tail, ""
            self.tail_changed = writer.tail != self.tail
            self.tail = writer.tail

        data, used = "", 0
        lead = self.lead
        for line in lines:
            wrapped, last, _ = split_rows(lead + carried + line, cols)
            data += "".join(wrapped) + last + "\x1b[0m\x1b[K\r\n"
            used += len(wrapped) + 1
            lead = carried = ""
            self.open, self.carry, self.cont, self.lead = "", 0, 0, ""
        if rows:
            #  铺满的行 + 下一个字符触发终端折行，再回到行首（那个字符留给首行重画）
            head, width = first_cell(rest)
            data += lead + "".join(rows) + head + "\x1b[0m\r"
            used += len(rows)
            self.open += "".join(rows)[len(carried) :]
            self.carry, self.cont, self.lead = 0, width, active
        return data + loose, used

    # ---- 落笔 ----

    def _batched(self) -> "contextlib.AbstractContextManager[None]":
        """攒成一次写出，并用同步输出序列包起来（不认的终端会忽略）。"""
        out = self.app.output

        @contextlib.contextmanager
        def scope() -> Iterator[None]:
            flush, out.flush = out.flush, lambda: None
            try:
                out.write_raw("\x1b[?2026h")
                yield
                out.write_raw("\x1b[?2026l")
            finally:
                out.flush = flush
                out.flush()

        return scope()

    def _commit_in_place(self, data: str, rows: int, cont: int) -> None:
        """不擦除的补打：正文盖在输入区最上面的 rows 行上，输入区整体下移。"""
        app = self.app
        out, renderer = app.output, app.renderer
        prev = renderer._last_screen  # noqa: SLF001
        if prev is None:
            self._commit_erasing(data, cont)
            return
        with self._batched():
            out.write_raw("\r")
            out.cursor_up(renderer._cursor_pos.y)  # noqa: SLF001
            out.reset_attributes()
            out.enable_autowrap()
            out.write_raw(data)
            #  告诉渲染器：屏上现在是旧的一帧去掉前 rows 行。它照常做差量重画，
            #  没变的格子一个字节都不写
            for name in ("data_buffer", "zero_width_escapes"):
                old = getattr(prev, name)
                new = type(old)(old.default_factory)
                for y, row in old.items():
                    if y >= rows:
                        new[y - rows] = row
                setattr(prev, name, new)
            prev.height = max(0, prev.height - rows)
            renderer._cursor_pos = type(renderer._cursor_pos)(x=0, y=0)  # noqa: SLF001
            renderer._last_style = None  # noqa: SLF001
            app._request_absolute_cursor_position()  # noqa: SLF001
            app._redraw()  # noqa: SLF001

    def _commit_erasing(self, data: str, cont: int | None = None) -> None:
        """整块重画式的补打：擦掉输入区 → 写正文 → 重画。会闪，只在需要把输入区
        高度归位时用。"""
        with self._batched():
            self._erase(self.cont if cont is None else cont)
            self.app.output.write_raw(data)
            self.app._redraw()  # noqa: SLF001

    def _erase(self, cont: int, up: int | None = None) -> None:
        """把光标挪到输入区首行并擦掉整个输入区，渲染器归零。只写缓冲、不冲刷。"""
        app = self.app
        out, renderer = app.output, app.renderer
        out.write_raw("\r")
        out.cursor_up(renderer._cursor_pos.y if up is None else up)  # noqa: SLF001
        if cont:
            out.write_raw(f"\x1b[{cont}C\x1b[J\r")
        else:
            out.write_raw("\x1b[2K\x1b[1B\x1b[J\x1b[1A")
        out.reset_attributes()
        out.enable_autowrap()
        renderer.reset()
        app._request_absolute_cursor_position()  # noqa: SLF001

    # ---- 渲染入口：缩放对齐 ----

    def _render(self, app: Any, layout: Any, is_done: bool = False) -> None:
        out = app.output
        if not is_done and self.size is not None and out.get_size() != self.size:
            self._resync()
        if app.renderer._last_screen is not None or is_done:  # noqa: SLF001
            self._plain_render(app, layout, is_done)
            return
        #  擦过之后的第一帧：库会再从行首清一次屏，那一下同样会抹掉续行的折行
        #  标记；我们已经清过，让它空转
        erase_down, out.erase_down = out.erase_down, lambda: None
        try:
            self._plain_render(app, layout, is_done)
        finally:
            out.erase_down = erase_down

    def _resync(self) -> None:
        """窗口大小变了：按终端重排后的样子找到输入区的新起点，擦掉。

        终端把每个逻辑行按新宽度重新折行。输入区里的行各自是一个逻辑行，比新
        窗口宽的会变成多行；首行若是续行，则连同上面已落下的部分一起重排。这里
        用同一套折行规则重算：重排后落在最后一个物理行之前的字符都算已落下，
        最后一行那一截留给首行继续画。
        """
        app = self.app
        self.size = app.output.get_size()
        cols = max(8, self.size.columns)
        screen, pos = app.renderer._last_screen, app.renderer._cursor_pos  # noqa: SLF001
        if screen is None:
            return
        writer = self.writer
        with writer.lock:
            shown = self.tail if writer.tail.startswith(self.tail) else ""
            rows, last, active = split_rows(self.open + shown, cols)
            above = sum(len(row) for row in rows)  # 重排后落在最后一行之前的字符数
            moved = max(0, above - len(self.open))  # 其中原本还在尾巴里的
            self.open += writer.tail[:moved]
            writer.tail = writer.tail[moved:]
            self.carry = len(self.open) - above
            self.tail = writer.tail
            self.lead = active
            self.cont = first_cell(last)[1] if above and last else 0

        def reflowed(y: int) -> int:
            row = screen.data_buffer.get(y) or {}
            width = max((x + c.width for x, c in row.items() if c.char.strip()), default=0)
            return max(1, -(-width // cols))

        up = 1 + sum(reflowed(y) for y in range(1, pos.y)) + pos.x // cols
        self._erase(self.cont, up=up)

    # ---- 让位 ----

    @contextlib.contextmanager
    def suspended(self) -> Iterator[None]:
        """把终端让给别人用（确认菜单、斜杠命令、子进程）：输入区收起、输出改为
        直写，做完再画回来。从工作线程调；可重入，嵌套时只有最外层动终端。"""
        if not self.active or self._loop is None:
            yield
            return
        with self._gate:
            outermost = not self._paused
            if outermost:
                self._hop(self._leave)
            try:
                yield
            finally:
                if outermost:
                    self._hop(self._enter)

    def _hop(self, action: Callable[[], None]) -> None:
        done = threading.Event()

        def run() -> None:
            try:
                action()
            finally:
                done.set()

        try:
            self._loop.call_soon_threadsafe(run)
        except RuntimeError:  # 事件循环已经关了（正在退出）
            return
        done.wait(5)

    def _leave(self) -> None:
        app = self.app
        if not app.is_running:
            return
        self.tick(final=True)
        with self._batched():
            self._erase(self.cont)
        self._paused = True
        app._running_in_terminal = True  # noqa: SLF001 - 让位期间库不许重画
        self._detached = app.input.detach()
        self._detached.__enter__()
        self._cooked = app.input.cooked_mode()
        self._cooked.__enter__()
        self.writer.passthrough = True

    def _enter(self) -> None:
        app = self.app
        self.writer.passthrough = False
        with contextlib.suppress(Exception):
            self._cooked.__exit__(None, None, None)
        with contextlib.suppress(Exception):
            self._detached.__exit__(None, None, None)
        app._running_in_terminal = False  # noqa: SLF001
        self._paused = False
        if not app.is_running:
            return
        app.renderer.reset()
        app._request_absolute_cursor_position()  # noqa: SLF001
        app._redraw()  # noqa: SLF001

    def stop(self) -> None:
        self.active = False


# ---------------------------------------------------------------------------
# 前端
# ---------------------------------------------------------------------------


class _Panel:
    """嵌在输入区里、等人拿主意的一件事。工作线程开、等；主线程画、收。

    `menu`：按键与画面由菜单自己给，画在输入行上方，输入行只读。
    `text`：借用输入行问一行字，`label` 顶替提示符；`stash` 是被收起的草稿。
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.done = threading.Event()
        self.result: Any = None
        self.bindings: Any = None
        self.fragments: Callable[[], Any] = list
        self.label: list[tuple[str, str]] = []
        self.default = ""
        self.password = False
        self.stash: Any = None
        self.saved: dict[str, Any] = {}


class _PanelHost:
    """菜单宿主（`tui.MenuHost` 的三个口子）：菜单画进输入区，不另起应用。"""

    def __init__(self, front: "LiveTui") -> None:
        self.front = front

    def run(self, bindings: Any, fragments: Callable[[], Any], height: int) -> Any:
        panel = _Panel("menu")
        panel.bindings, panel.fragments = bindings, fragments
        result = self.front._show(panel)  # noqa: SLF001 - 同模块搭档
        return None if result is _CANCELLED else result

    def finish(self, result: Any) -> None:
        self.front._resolve(result)  # noqa: SLF001

    def above(self, action: Callable[[], None]) -> None:
        action()  # 打印本来就落在输入区上方


class LiveTui(Tui):
    """常驻输入区的交互前端。渲染层、按键、确认与提问都继承自 `Tui`。"""

    def __init__(self, permissions: Any, console: Any = None) -> None:
        super().__init__(permissions, console)
        self._screen: LiveScreen | None = None
        self._writer: LiveWriter | None = None
        self._jobs: queue.Queue[tuple[str, bool] | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        #  None=空闲；"turn"=模型在作答（回车是插话）；"job"=在跑斜杠命令等
        self._busy: str | None = None
        self._turn_started = 0.0
        #  作答期间敲的斜杠命令：工作线程在事件之间的安全点取走
        self._commands: collections.deque[str] = collections.deque()
        self._activity: Any | None = None
        self._last_interrupt = 0.0
        self._exit_code = 0
        #  底部那一行右侧的事实：分支与改动量、上下文用量。工作线程在回合之间刷新
        self._facts = {"git": "", "context": ""}
        self._panel: _Panel | None = None  # 正画在输入区里的
        self._asking: _Panel | None = None  # 工作线程正在等的（退出时要放它走）
        self.sink.activity = self._set_activity
        #  运行期的输入行就是输入区本身，不再往 spinner 下面画草稿
        self.sink.live_footer = None

    # ---- 主循环 ----

    def _run_loop(self, agent: Agent) -> int:
        import asyncio

        self.agent = agent
        agent.on_notification = self._on_notification
        self.sink.bash_timeout = agent.config.bash_timeout
        self.sink.request_timeout = int(agent.config.request_timeout)
        from pathlib import Path

        self.sink.workspace = Path(agent.config.workspace)
        self._startup_notices(agent)

        session = self._input_session()
        #  补全菜单要的空间按需留（_live_tick），平时输入行紧贴下面的分隔线
        session.reserve_space_for_menu = 0
        app = session.app
        app.erase_when_done = True
        menu_open = Condition(lambda: self._panel is not None and self._panel.kind == "menu")
        #  菜单挂着时输入行的键全部停用，只剩菜单自己的；排在后面的优先
        bindings = merge_key_bindings(
            [
                ConditionalKeyBindings(
                    merge_key_bindings([session.key_bindings, self._live_bindings()]), ~menu_open
                ),
                self._panel_bindings(menu_open),
                DynamicKeyBindings(lambda: self._panel.bindings if menu_open() else None),
            ]
        )
        style = Style.from_dict(
            {**theme.ptk_style(), "bottom-toolbar": "noreverse", "bottom-toolbar.text": "noreverse"}
        )
        real = sys.stdout
        writer = self._writer = LiveWriter(real)
        writer.ends_blank = False  # 开场提示刚打过，第一句话上方也要空一行
        screen = self._screen = LiveScreen(app, writer)
        screen.animate = lambda: self._busy is not None
        self._worker = threading.Thread(target=self._work, name="live-worker", daemon=True)
        threading.Thread(target=self._refresh_facts, name="live-facts", daemon=True).start()

        def pre_run() -> None:
            screen.install()
            writer.passthrough = False
            self._prime_prompt()
            #  菜单挂着时草稿留在原处但改不动，光标收起
            session.default_buffer.read_only = menu_open
            app.layout.current_window.always_hide_cursor = menu_open
            session.default_buffer.on_completions_changed += self._on_completions
            self._worker.start()

        async def main() -> None:
            with contextlib.suppress(EOFError, KeyboardInterrupt):
                await session.prompt_async(
                    self._live_message,
                    bottom_toolbar=self._live_toolbar,
                    placeholder=self._live_placeholder,
                    key_bindings=bindings,
                    style=style,
                    pre_run=pre_run,
                )

        sys.stdout = writer  # type: ignore[assignment]
        try:
            asyncio.run(main())
        finally:
            screen.stop()
            if (asking := self._asking) is not None:
                asking.done.set()  # 还在等人拿主意就退出了：按取消放行
            writer.passthrough = True
            sys.stdout = real
            if self._busy == "turn":
                agent.interrupt()
            self._jobs.put(None)
            if self._worker.is_alive():
                self._worker.join(timeout=3)
            writer.drain()
        print()
        return self._exit_code

    def _on_completions(self, _buffer: Any) -> None:
        session = self._session
        if session is not None:
            session.reserve_space_for_menu = 6 if session.default_buffer.complete_state else 0

    # ---- 工作线程 ----

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            line, literal = job
            self._busy = "job"
            leave = False
            try:
                leave = self._handle(line, literal)
            except Exception as exc:  # noqa: BLE001 - 一行出错不该带走整个界面
                self.console.print(Text(f"出错了：{type(exc).__name__}: {exc}", style="status.error"))
            finally:
                self._busy = None
                self._activity = None
            self._refresh_facts()
            if leave:
                self._leave(0)
                return

    def _handle(self, line: str, literal: bool) -> bool:
        """处理输入区交出来的一行；返回 True = 该退出了。路由同 `Tui._run_loop`。"""
        from .cli import handle_slash, skill_prompt

        agent = self.agent
        assert agent is not None and self._screen is not None
        action = keys.classify_input(line, literal=literal)
        if action.kind == "empty":
            return False
        if action.kind == "usage":
            self.console.print(Text(f"  {action.hint}", style="text.secondary"))
            return False
        recall = action.args
        items, self._suggestions = self._suggestions, []
        if action.kind == "send" and not literal:
            selected = suggest.resolve_selection(action.args, items)
            if selected != action.args:
                self.console.print(Text(f"  ↳ {selected}", style="text.secondary"))
                action = keys.InputAction("send", selected)
                recall = selected
        if action.kind == "slash":
            expanded = skill_prompt(agent, action.args)
            if expanded is None:
                #  斜杠命令用 print 直出，有的还要读一行输入、弹选择菜单：整段让位
                with self._screen.suspended():
                    leave = handle_slash(agent, action.args, select=search_select)
                if leave:
                    return True
                if queued := "\n".join(agent.drain_steers()).strip():
                    self.console.print(
                        Text("  （已把该轮原话填回输入行，可改可直接回车发送）", style="text.secondary")
                    )
                    self._prefill(queued)
                return False
            if not expanded:
                return False
            action = keys.InputAction("send", expanded)
        if action.kind == "shell":
            with self._screen.suspended():
                self._run_shell(action.args)
            return False
        if action.kind == "memo":
            self._note_memory(action.args)
            return False
        self._last_input = recall
        self._turn(action.args)
        return False

    def _turn(self, line: str) -> None:
        from .cli import background_status, turn_stats_line

        agent = self.agent
        assert agent is not None
        self.sink.begin_turn()
        attention.running()
        self._turn_started = time.monotonic()
        self._busy = "turn"
        try:
            with NoticeCapture(self.sink):
                agent.send(self._content_of(line))
            self.sink._flush_ro_group()  # noqa: SLF001 - 同包前端搭档
        except (KeyboardInterrupt, Interrupted):
            self.sink.interrupt()
            agent.close_open_tool_calls("用户按 Ctrl-C 中断了本轮。")
            self.console.print(Text("\n[已中断，可以继续输入]", style="status.warning"))
        except Exception as exc:  # noqa: BLE001 - 一次请求失败不该退出
            self.sink.interrupt()
            if agent.session_log:
                agent.session_log.event("error", error=f"{type(exc).__name__}: {exc}")
            self.console.print(
                Text(f"\n请求失败：{type(exc).__name__}: {exc}", style="status.error")
            )
        finally:
            self._busy = "job"
            self._activity = None
            self._run_midturn_commands()
        if note := background_status(agent):
            self.console.print(Text(note, style="text.secondary"))
        if stats := turn_stats_line(agent, interactive=True):
            self.console.print(Text(f"  {stats}", style="text.secondary"))
        self._print_expand_hint()
        attention.waiting(attention.WAITING_INPUT)
        #  没赶上本轮的插话与要等轮末的命令：填回输入行，不自动提交
        held, self._held_commands = self._held_commands, []
        queued = "\n".join(part for part in [*agent.drain_steers(), *held] if part).strip()
        if queued:
            self.console.print(
                Text("  （已接住你刚才输入的内容，确认后回车发送）", style="text.secondary")
            )
            self._prefill(queued)
        print()

    def _run_midturn_commands(self) -> None:
        """作答期间敲的斜杠命令：能当场跑的在事件之间跑掉，其余留到轮末。"""
        if not self._commands or threading.current_thread() is not self._worker:
            return
        from .cli import MIDTURN_COMMANDS, handle_slash

        for _ in range(len(self._commands)):
            try:
                line = self._commands.popleft()
            except IndexError:
                break
            self.sink._flush_ro_group()  # noqa: SLF001 - 同包前端搭档
            if line.split()[0] not in MIDTURN_COMMANDS:
                self._held_commands.append(line)
                self.console.print(
                    Text(f"  ↳ {line} 要等本轮结束，已留到输入行", style="text.secondary")
                )
                continue
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                handle_slash(self.agent, line)
            self.console.print(Text(f"  ↳ {line}", style="text.secondary"))
            if output := buffer.getvalue().rstrip("\n"):
                self.console.print(Text.from_ansi(output))

    def _refresh_facts(self) -> None:
        agent = self.agent
        if agent is None:
            return
        with contextlib.suppress(Exception):
            limit = int(agent.config.context_limit)
            if limit > 0:
                self._facts["context"] = f"上下文 {min(99, agent.context_tokens() * 100 // limit)}%"
        self._facts["git"] = _git_facts(str(agent.config.workspace))

    # ---- 主线程：输入 ----

    def _hand_off(self, buffer: Any) -> None:
        panel = self._panel
        if panel is not None:
            self._literal_submit = False
            if panel.kind == "text":
                self._resolve(buffer.text)  # 追问的回答原样交回，空的也算
            return
        text = buffer.text.strip()
        literal, self._literal_submit = self._literal_submit, False
        if not text:
            buffer.reset()
            return
        buffer.append_to_history()
        buffer.reset()
        agent = self.agent
        if self._busy == "turn" and agent is not None:
            #  作答中：命令形状的单行是对程序说的，其余是插话
            if "\n" not in text and keys.classify_input(text).kind == "slash":
                self._commands.append(text)
            else:
                agent.steer(text)
            return
        self._echo(text, literal)
        self._jobs.put((text, literal))

    def _echo(self, text: str, literal: bool) -> None:
        """输入行不留在原地了，把这一行补打到上方，scrollback 里才有「我说了什么」。

        回头翻记录时最先要找的就是「我在哪儿问的」：上方空一行把它和前面的输出
        隔开；对模型说的话行首立一个强调色的花瓣星号、正文加粗（多行只标第一行，
        其余缩进对齐），命令（/、!、#）是对程序说的，保持原样不抢眼。
        """
        writer = self._writer
        if writer is not None and not writer.ends_blank:
            print()
        said = keys.classify_input(text, literal=literal).kind == "send"
        if said:
            rows = [
                Text.assemble((_SAID_MARK if index == 0 else "  ", "text.accent"), (row, "bold"))
                for index, row in enumerate(text.split("\n"))
            ]
            self.console.print(Text("\n").join(rows))
        else:
            body = text.replace("\n", "\n  ")
            self.console.print(Text.assemble(("› ", "prompt"), (body, "")))

    def _above(self, action: Callable[[], None]) -> None:
        action()

    def _prefill(self, text: str) -> None:
        session, screen = self._session, self._screen
        if session is None or screen is None or screen._loop is None:  # noqa: SLF001
            return

        def fill() -> None:
            buffer = session.default_buffer
            merged = f"{buffer.text}\n{text}" if buffer.text.strip() else text
            buffer.text = merged
            buffer.cursor_position = len(merged)

        with contextlib.suppress(RuntimeError):
            screen._loop.call_soon_threadsafe(fill)  # noqa: SLF001

    def _leave(self, code: int) -> None:
        self._exit_code = code
        session, screen = self._session, self._screen
        if session is None or screen is None or screen._loop is None:  # noqa: SLF001
            return

        def leave() -> None:
            if session.app.is_running:
                session.app.exit(result="")

        with contextlib.suppress(RuntimeError):
            screen._loop.call_soon_threadsafe(leave)  # noqa: SLF001

    def _live_bindings(self) -> KeyBindings:
        """只属于常驻输入区的键：打断与退出。其余按键仍来自按键表（keys.BINDINGS）。"""
        bindings = KeyBindings()

        def interrupt(event: Any) -> None:
            agent = self.agent
            if self._panel is not None:
                self._resolve(_CANCELLED)
                return
            if self._busy == "turn" and agent is not None:
                agent.interrupt()
                return
            if self._busy is not None:
                return
            now = time.monotonic()
            if now - self._last_interrupt < self._EXIT_WINDOW:
                event.app.exit(result="")
                return
            self._last_interrupt = now
            #  清掉的半截输入先存进 Esc-Esc 的取回槽：误按一下不该让它凭空消失
            buffer = event.app.current_buffer
            draft = buffer.text.strip()
            hint = "  再按一次 Ctrl-C 退出"
            if draft:
                self._last_input = draft
                hint += "（刚才的输入 Esc Esc 可取回）"
            buffer.reset()
            self.console.print(Text(hint, style="text.secondary"))

        bindings.add("c-c")(interrupt)
        bindings.add("<sigint>")(interrupt)
        return bindings

    # ---- 主线程：跨线程来的事 ----

    def _set_activity(self, line: Any | None) -> None:
        self._activity = line

    def _on_suggestions(self, items: list[str]) -> None:
        if self._busy == "turn":
            return
        self._suggestions = items
        self.console.print(Text(f"  {suggest.render_line(items)}", style="text.secondary"))

    def _on_notification(self, item: dict[str, Any]) -> None:
        if not item.get("wake") or self._busy == "turn":
            return
        lines = str(item.get("text", "")).strip().splitlines()
        if lines:
            self.console.print(
                Text(f"  ⟲ {lines[0]}（发下一条消息时送达模型）", style="text.secondary")
            )

    # ---- 等人拿主意的交互：嵌在输入区里 ----
    #
    #  确认、提问、密码的流程仍是 `Tui` 的（工作线程里同步地问、等、接着走），
    #  这里只顶替它们落到终端的那两个口子：菜单宿主与单行追问。

    def _menu_host(self) -> Any:
        return _PanelHost(self)

    def _free_text(self) -> Callable[[str], str | None]:
        return lambda draft: self._ask_line(
            [(theme.ptk("menu.title"), "  自由回答：")], default=draft
        )

    def _confirm_prompt(self, message: Any, **kwargs: Any) -> str:
        answer = self._ask_line(
            list(message),
            default=str(kwargs.get("default") or ""),
            password=bool(kwargs.get("is_password")),
        )
        if answer is None:
            raise EOFError
        return answer

    def _prompt_abandoned(self) -> None:
        """追问借用的是输入行，放弃后它原地变回输入行，没有要收尾的半行。"""

    def _ask_line(
        self, label: list[tuple[str, str]], default: str = "", password: bool = False
    ) -> str | None:
        """借输入行问一行字；放弃（Esc / Ctrl-C / 输入区已不在）返回 None。"""
        panel = _Panel("text")
        panel.label, panel.default, panel.password = label, default, password
        result = self._show(panel)
        return None if result is _CANCELLED else result

    def _show(self, panel: _Panel) -> Any:
        """工作线程：把面板交给主线程画出来，等到有结果。"""
        screen = self._screen
        loop = screen._loop if screen is not None else None  # noqa: SLF001
        if screen is None or loop is None or not screen.active:
            return _CANCELLED
        panel.result = _CANCELLED
        self._asking = panel
        try:
            loop.call_soon_threadsafe(self._open, panel)
            if not screen.active:  # 排上队之前输入区刚好收了
                return _CANCELLED
            panel.done.wait()
        except RuntimeError:  # 事件循环已经关了（正在退出）
            return _CANCELLED
        finally:
            self._asking = None
        return panel.result

    def _open(self, panel: _Panel) -> None:
        session = self._session
        if session is None or panel.done.is_set():
            return
        self._panel = panel
        if panel.kind == "text":
            buffer = session.default_buffer
            panel.stash = buffer.document
            #  追问的是规则、理由、密码：不要斜杠补全、幽灵建议与行首着色
            for name in ("completer", "auto_suggest", "lexer"):
                panel.saved[name] = getattr(session, name)
                setattr(session, name, None)
            session.is_password = panel.password
            buffer.reset(Document(panel.default, len(panel.default)))
        session.app.invalidate()

    def _resolve(self, result: Any) -> None:
        """主线程：面板有了结果，收起来，放工作线程走。"""
        panel, session = self._panel, self._session
        if panel is None:
            return
        self._panel = None
        if panel.kind == "text" and session is not None:
            for name, value in panel.saved.items():
                setattr(session, name, value)
            session.is_password = False
            #  reset 连撤销栈一起清：密码不留在输入行的任何角落
            session.default_buffer.reset(panel.stash or Document())
        panel.result = result
        panel.done.set()
        if session is not None:
            session.app.invalidate()

    def _panel_bindings(self, menu_open: Any) -> KeyBindings:
        """面板挂着时才生效的键。菜单：没被菜单认领的键一律吃掉（否则落进输入
        行）；单行追问：Esc / 空行上的 Ctrl-D 放弃。"""
        bindings = KeyBindings()
        text_open = Condition(lambda: self._panel is not None and self._panel.kind == "text")
        empty = Condition(lambda: self._session is None or not self._session.default_buffer.text)

        def swallow(event: Any) -> None:
            return None

        def cancel(event: Any) -> None:
            self._resolve(_CANCELLED)

        for key in (Keys.Any, Keys.BracketedPaste, "c-d"):
            bindings.add(key, filter=menu_open)(swallow)
        bindings.add("<sigint>", filter=menu_open)(cancel)
        #  Esc 立刻生效：不等「是不是某个组合键的开头」那一秒多
        bindings.add("escape", filter=menu_open | text_open, eager=True)(cancel)
        bindings.add("c-d", filter=text_open & empty)(cancel)
        return bindings

    # ---- 主线程：画输入区 ----

    def _ansi(self, text: Text) -> str:
        """把带样式的 rich 文本编成颜色序列（不经 console.print，不落笔）。"""
        console = self.console
        color_system = console._color_system  # noqa: SLF001 - rich 没有公开的取法
        return "".join(
            segment.style.render(segment.text, color_system=color_system)
            if segment.style and color_system is not None
            else segment.text
            for segment in text.render(console, end="")
        )

    def _live_message(self) -> list[tuple[str, str]]:
        """输入行上方，自上而下：没成行的尾巴、进行中的事、待送达的话、分隔线。"""
        screen = self._screen
        cols = screen.cols() if screen is not None else 80
        dim, accent = theme.ptk("text.secondary"), theme.ptk("text.accent")
        out: list[tuple[str, str]] = []
        #  这一行常在（空着也占位），输入区的行数才稳定
        out.extend(to_formatted_text(ANSI(screen.first_row() if screen is not None else "")))
        out.append(("", "\n"))
        rows: list[list[tuple[str, str]]] = []
        frame = _SPINNER[int(time.monotonic() * 10) % len(_SPINNER)]
        activity = self._activity
        panel = self._panel
        if panel is not None:
            #  等人拿主意：不转圈；菜单就画在这儿，单行追问在下面顶替提示符
            if panel.kind == "menu":
                rows.append([*to_formatted_text(panel.fragments())])
        elif activity is not None:
            with contextlib.suppress(Exception):
                line = self._ansi(activity.__rich__()).split("\n")[0]
                rows.append([(accent, f"{frame} "), *to_formatted_text(ANSI(line))])
        elif self._busy == "turn":
            took = int(time.monotonic() - self._turn_started)
            rows.append([(accent, f"{frame} 作答中…"), (dim, f" ({_elapsed(took)})")])
        agent = self.agent
        waiting = [f"待执行：{line}" for line in list(self._commands)]
        if agent is not None and self._busy == "turn":
            waiting += [f"待送达：{text}" for _, text in agent.pending_steers()]
        for item in waiting[-3:]:
            shown = ui.preview(item.replace("\n", " ⏎ "), max(8, cols - 6))
            rows.append([(dim, f"  ↳ {shown}")])
        if rows:
            out.append(("", "\n"))
            for row in rows:
                out.extend(row)
                out.append(("", "\n"))
            if panel is None:
                out.append(("", "\n"))
        out.append((dim, "─" * cols + "\n"))
        if panel is not None and panel.kind == "text":
            out.extend(panel.label)
        else:
            out.append((theme.ptk("prompt"), "› "))
        return out

    def _live_placeholder(self) -> list[tuple[str, str]]:
        panel = self._panel
        if panel is not None:
            hint = "先在上面选一项" if panel.kind == "menu" else ""
        else:
            hint = "继续输入可以插话" if self._busy == "turn" else "输入消息，回车发送"
        return [(theme.ptk("text.secondary"), hint)]

    def _live_toolbar(self) -> list[tuple[str, str]]:
        """输入行下方：分隔线 + 一行说明。

        左边是当前模式与此刻能按的键；右边是分支与改动量、上下文用量、模型名。
        放不下时从最不要紧的丢起：按键说明 → 改动量 → 分支 → 模型名，上下文
        用量留到最后。只占一行，不折行。
        """
        screen = self._screen
        cols = screen.cols() if screen is not None else 80
        dim = theme.ptk("text.secondary")
        agent = self.agent
        mode = modes.get(agent.mode if agent is not None else modes.DEFAULT)
        label = f"{mode.marker} {mode.label}".strip()
        panel = self._panel
        if panel is not None:
            #  菜单自带一行按键说明；单行追问只有这两个键
            hints = "" if panel.kind == "menu" else " · enter 确认 · esc 取消"
        else:
            hints = " (shift+tab 切换)" + (
                " · ctrl-c 打断 · enter 插话"
                if self._busy == "turn"
                else " · enter 发送 · ctrl-c ×2 退出"
            )
        model = str(agent.config.model) if agent is not None else ""
        git, context = self._facts["git"], self._facts["context"]
        branch = git.split(" ")[0] if git else ""
        width = ui.display_width
        for keys_shown, git_shown, model_shown in (
            (hints, git, model),
            ("", git, model),
            ("", branch, model),
            ("", "", model),
            ("", "", ""),
        ):
            right = " · ".join(part for part in (git_shown, context, model_shown) if part)
            gap = cols - 3 - width(label) - width(keys_shown) - width(right)
            if gap >= 2:
                break
        else:
            keys_shown, right, gap = "", context, 1
        return [
            (dim, "─" * cols + "\n"),
            ("", "  "),
            (theme.ptk(mode.style), label),
            (dim, f"{keys_shown}{' ' * max(1, gap)}{right} "),
        ]


_CANCELLED: Any = object()  # 面板被放弃（与「回答了空串」「选了 None 值」区分开）


def _elapsed(seconds: int) -> str:
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def _git_facts(workspace: str) -> str:
    """「分支 +增 −删」，不在仓库里或 git 不可用时是空串。"""

    def git(*argv: str) -> str:
        done = subprocess.run(
            ["git", "-C", workspace, *argv],
            capture_output=True,
            timeout=2,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        return done.stdout.decode("utf-8", errors="replace").strip() if done.returncode == 0 else ""

    try:
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if not branch:
            return ""
        stat = git("diff", "--shortstat")
    except (OSError, subprocess.SubprocessError):
        return ""
    added = re.search(r"(\d+) insertion", stat)
    removed = re.search(r"(\d+) deletion", stat)
    if not (added or removed):
        return ui.preview(branch, 28)
    return f"{ui.preview(branch, 28)} +{added.group(1) if added else 0} −{removed.group(1) if removed else 0}"


def enabled(environ: Any = None) -> bool:
    """用户有没有打开常驻输入区（默认关）。"""
    import os

    flag = (environ if environ is not None else os.environ).get(ENV_SWITCH, "")
    return flag.strip().lower() in ("1", "true", "yes", "on")
