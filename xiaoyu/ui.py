"""终端输出的小工具（零依赖，直接用 ANSI）。

着色一律走 `theme.py` 的语义 token：这里只负责把 Style 翻译成转义序列，
"警告是什么颜色"由 theme 决定。想加一处新配色就去 theme 加 token，不要在
这里新增裸颜色函数——那正是当初一百多处散落颜色的来源。

宽度同理：不要再写死 `preview(x, 160)`。80 列终端上 160 字符的"一行摘要"
会折成两行，折叠就白折了。用 `fit()` 按终端实际宽度算预算。
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import unicodedata

from . import theme

if os.name == "nt":
    #  让 conhost（传统 cmd）开启 VT 转义处理；Windows Terminal 本来就支持
    os.system("")

#  三个标准流一律钉死 UTF-8。默认是 locale 编码：Windows 的管道/重定向是
#  GBK/cp1252，POSIX 上 LANG=en_US.ISO8859-1 之类的老 locale 同理——打印中文
#  直接 UnicodeEncodeError（整个 CLI 的自有文案都是中文，等于开不了机），
#  stream-json / wire 两个协议面又都以 UTF-8 为契约。交互控制台本来就是 UTF-8
#  的机器不受影响；老 locale 的终端上顶多花屏，好过崩掉。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")
#  stdin 是解码方向（外部字节进程序），按编码纪律得有 replace 兜底：
#  wire 请求或 `xiaoyu < prompt.txt` 里坏一个字节，不该让整个会话起不来。
with contextlib.suppress(Exception):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

#  窄到这个宽度以下就不再为终端宽度让步了：再减就没有可读的信息量，
#  不如让终端自己去折行
_MIN_BUDGET = 40
#  拿不到真实终端尺寸时的假定（管道、CI、非 tty）
_FALLBACK = (80, 24)


def styled(token: str, text: str) -> str:
    """按语义 token 着色。token 名见 theme.py。"""
    if not _ENABLED:
        return text
    code = theme.style(token).ansi()
    if not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def secondary(text: str) -> str:
    """次要信息：提示、脚注、已折叠的内容。"""
    return styled("text.secondary", text)


def heading(text: str) -> str:
    return styled("text.heading", text)


def accent(text: str) -> str:
    """可交互/可点的东西：工具名、命令、路径。"""
    return styled("text.accent", text)


def success(text: str) -> str:
    return styled("status.success", text)


def warning(text: str) -> str:
    return styled("status.warning", text)


def error(text: str) -> str:
    return styled("status.error", text)


def prompt(text: str) -> str:
    """等待用户输入的提示符。"""
    return styled("prompt", text)


def color256(code: int, text: str) -> str:
    """256 色前景的直通口，只给横幅渐变用——它是纯装饰，逐行取色，
    套不进语义 token 的框子。"""
    if not _ENABLED:
        return text
    return f"\033[38;5;{code}m{text}\033[0m"


def display_width(text: str) -> int:
    """可见宽度：CJK 全角算 2 列。对齐用 len() 会让含中文的列全部错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """按可见宽度左对齐补空格。"""
    return text + " " * max(width - display_width(text), 0)


def term_size() -> tuple[int, int]:
    """终端宽高。拿不到（管道/CI）就用 80×24。"""
    size = shutil.get_terminal_size(fallback=_FALLBACK)
    return size.columns, size.lines


def term_width() -> int:
    return term_size()[0]


def budget(reserve: int = 0, width: int | None = None) -> int:
    """一行能放下的字符预算：终端宽度减去调用方已占用的前缀/缩进。

    `width` 显式传入时用它（rich 的 Console 有自己的宽度，测试里也会指定）。
    """
    return max((width if width is not None else term_width()) - reserve, _MIN_BUDGET)


#  终端控制字符：C0（保留 \t \n）、DEL、C1。ESC 开头的序列（改窗口标题、OSC 52
#  写剪贴板、光标移动清屏）靠 ESC 本身生效，C1 里的 0x9b/0x9d 在部分终端上是
#  8-bit 的 CSI/OSC。模型正文和工具输出里的这些字符可以来自任何被读到的文件或
#  网页，原样打到终端就是注入面。
_TERMINAL_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def strip_controls(text: str) -> str:
    """去掉会被终端解释的控制字符，只留 \\t 和 \\n。

    逐字符删除、不解析序列：流式正文按增量分片到达，一个转义序列可能被切在
    两片之间，按序列匹配会漏；删掉 ESC 后剩下的 "[31m" 之类只是无害的可见字符。
    """
    return _TERMINAL_CONTROLS.sub("", text)


#  完整的转义序列：OSC（ESC ] … 以 BEL 或 ESC \\ 结束）、CSI（ESC [ 或 8-bit 的
#  0x9b 起头，参数、中间字节、一个结尾字节）、其余两字符序列
_TERMINAL_SEQUENCES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|\x1b[@-Z\\^_]"
)


#  双向文本控制字符：嵌入/覆盖（U+202A–202E）、隔离（U+2066–2069）、方向标记
#  （U+200E/200F）。它们不是 C0/C1，上面的正则盖不到；审批框里一条
#  `rm -rf ‮…` 形态的命令，终端按双向算法渲染出来的顺序和真正执行的字节序
#  不一样——用户批的是看到的那条，跑的是另一条。转成可见的 \\uXXXX 而不是删：
#  删掉之后显示又"正常"了，用户反而不知道这条命令藏着东西
_BIDI_CONTROLS = re.compile("[‪-‮⁦-⁩‎‏]")


def escape_bidi(text: str) -> str:
    """把双向控制字符换成 \\uXXXX 字面形式，让它们在终端上现形。"""
    return _BIDI_CONTROLS.sub(lambda match: f"\\u{ord(match.group()):04x}", text)


def strip_sequences(text: str) -> str:
    """strip_controls 的整串版：先把完整的转义序列连参数一起摘掉，再删零散的控制字符，
    最后把双向控制字符转成可见形式（escape_bidi）。

    只给**完整的字符串**用（工具输出预览、参数、提示文案、审批框）：带颜色的命令
    输出里，只删 ESC 会留下一地 "[31m" "[0m"。流式正文的分片不能用——序列可能
    被切在两片之间，那条路继续逐字符删（正文里的双向字符也不转：阿拉伯文、
    希伯来文的正常段落用得着它们，而正文不是拿来批准执行的）。
    """
    return escape_bidi(strip_controls(_TERMINAL_SEQUENCES.sub("", text)))


def preview(value: object, limit: int = 100) -> str:
    """把工具参数压成一行短预览，**含省略号在内**不超过 limit 个字符。

    省略号必须算进预算里：limit 现在就是终端宽度，多吐一个字符这行就折行了，
    "压成一行"的意义随之落空。
    """
    text = strip_sequences(str(value).replace("\n", "⏎"))
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


#  键名像凭据的参数：值永远不进标题（标题会上屏、进宿主的 UI、进截图）
_CREDENTIAL_KEY = re.compile(
    r"(?i)(?:token|secret|passw(?:or)?d|passphrase|api[_-]?key|apikey|authorization"
    r"|^auth$|cookie|credential|private[_-]?key|session[_-]?id|bearer|signature)"
)
#  通用摘要里每个值的字符上限
_SUMMARY_VALUE_CAP = 60
#  已知工具：按顺序取这些参数里有值的拼起来。第一项是"这次调用在干什么"，
#  其余是限定（在哪搜、搜哪类文件）
_SUMMARY_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "bash": (("command", ""),),
    "monitor": (("command", ""),),
    "read_file": (("path", ""),),
    "write_file": (("path", ""),),
    "str_replace": (("path", ""),),
    "grep": (("pattern", ""), ("path", "in "), ("glob", "")),
    "list_files": (("pattern", ""), ("path", "in ")),
    "recall": (("id", "#"), ("pattern", "")),
    "explore": (("task", ""), ("query", ""), ("pattern", "")),
    "web_search": (("query", ""),),
    "search_tool": (("query", ""),),
    "browser": (("action", ""), ("url", ""), ("selector", ""), ("key", "")),
    "kill_task": (("task_id", ""),),
}


def _summary_value(value: object) -> str:
    if isinstance(value, dict):
        return "{…}" if value else "{}"
    if isinstance(value, (list, tuple)):
        return f"[{len(value)} 项]" if value else "[]"
    text = " ".join(str(value).split())
    return text if len(text) <= _SUMMARY_VALUE_CAP else text[: _SUMMARY_VALUE_CAP - 1] + "…"


def tool_summary(name: str, args: object) -> str:
    """一次工具调用"在干什么"的一行摘要（不含工具名、不截到终端宽度）。

    终端的工具行、ACP 的 tool_call 标题、确认框都从这里取：各写一份的时候，
    终端对 grep 只显示路径（搜什么看不见），MCP 工具把整个参数字典连同
    token 一起打上屏。规则：
    - 已知工具取最能说明意图的参数（见 _SUMMARY_FIELDS）；
    - use_tool 取被调工具的名字，再按同样规则摘它的入参；
    - 其余（MCP、插件）逐个 `键=值`：键名像凭据的整个跳过，值各自封顶，
      嵌套结构只报形状。
    """
    if not isinstance(args, dict):
        return _summary_value(args)
    if name == "use_tool":
        inner = str(args.get("tool_name") or "").strip()
        rest = tool_summary(inner, args.get("tool_input") or {}) if inner else ""
        return " ".join(part for part in (inner, rest) if part)
    fields = _SUMMARY_FIELDS.get(name)
    if fields is not None:
        #  值原样给（多行命令的换行由调用方决定怎么画：终端画成 ⏎，ACP 压成空格）
        parts = [
            f"{prefix}{args[key]}"
            for key, prefix in fields
            if args.get(key) not in (None, "", [], {})
        ]
        if parts:
            return " ".join(parts)
    elif "path" in args and isinstance(args["path"], str) and args["path"]:
        #  不认识的工具带着 path：多半是文件类，路径就是意图
        return args["path"]
    pairs = [
        f"{key}={_summary_value(value)}"
        for key, value in args.items()
        if not _CREDENTIAL_KEY.search(str(key)) and value not in (None, "", [], {})
    ]
    return " ".join(pairs)


#  升权档位 → 用户看得懂的一句后果。档位名本身（danger-full-access）对不熟
#  沙箱的人只是个字符串，确认框里得把"批下去会怎样"写明白
_ESCALATION_MEANING: dict[str, str] = {
    "allow-network": "仍套沙箱，但本次放行网络",
    "danger-full-access": "本次不套沙箱，命令能写你有权限的任何路径",
}
#  理由是模型给的一句话：封顶、剥转义序列，和其它上屏的模型文本同一待遇
_JUSTIFICATION_CAP = 160


def escalation_notice(args: object) -> list[str]:
    """bash 调用带了沙箱升权申请时，确认框要点名的几行（没申请 = 空列表）。

    升权是"必问"三项之一，确认框却只画命令本身的话，用户按下的那个"允许"
    批的是什么他并不知道——看着是在批一条命令，实际批的是"这次不套沙箱"。
    这几行把档位、后果、模型给的理由一起摆到按键之前，TUI 与纯 CLI 两套
    确认框共用，别各写一份。
    """
    if not isinstance(args, dict):
        return []
    tier = str(args.get("sandbox_permissions") or "").strip()
    if not tier:
        return []
    meaning = _ESCALATION_MEANING.get(tier)
    head = f"申请沙箱升权：{preview(tier, 40)}"
    if meaning:
        head += f"——{meaning}"
    lines = [head]
    reason = str(args.get("justification") or "").strip()
    lines.append(
        f"模型给的理由：{preview(reason, _JUSTIFICATION_CAP)}" if reason else "模型没有给理由"
    )
    return lines


def fit(value: object, reserve: int = 0, width: int | None = None) -> str:
    """按终端实际宽度压成一行（`preview` 的自适应版）。"""
    return preview(value, budget(reserve, width))
