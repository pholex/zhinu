"""终端能力探测。

目前只探一样东西：**终端背景色**（OSC 11 查询），用来把 theme 的 auto 模式
落到 dark 还是 light。这是深浅两套配色唯一可靠的判据——环境变量里没有这个
信息，`COLORFGBG` 只有少数终端设置且经常是陈旧值。

**刻意不做 kitty 键盘协议探测**：该协议能拿到 Shift+Enter 之类的
组合键，但 prompt_toolkit 3.0.53 的 vt100 解析器完全不认 kitty 的 CSI-u
序列（`grep kitty` 它的 input/ 目录零命中）。开了协议 = 所有按键变成它解不开
的转义串，整个输入层报废。Shift+Enter 走 editor_setup 那条路解决。

探测的代价与风险：
- 要临时把终端切到 raw 模式读回答，期间用户的抢跑输入可能被吃掉几个字符。
  所以只在启动时、打完横幅之前做一次，那个窗口用户还没开始打字；
- 不支持 OSC 11 的终端不会回答，select 超时后原样返回，不留痕迹；
- termios 状态在 finally 里恢复，异常路径也不会把终端丢在 raw 模式。

`XIAOYU_THEME` 显式指定了 dark/light 时整个探测跳过——用户说了算，不必
再问终端。
"""

from __future__ import annotations

import contextlib
import os
import re
import sys

#  OSC 11 查询与回答。回答形如 ESC ] 11 ; rgb:rrrr/gggg/bbbb ST（或 BEL 结尾）
_QUERY = "\033]11;?\033\\"
_ANSWER = re.compile(rb"\x1b\]11;rgb:([0-9a-fA-F]+)/([0-9a-fA-F]+)/([0-9a-fA-F]+)")

#  等终端回答的秒数。给太长会让启动明显卡顿，给太短慢终端来不及答；
#  本地终端的往返在毫秒级，150ms 是很宽裕的余量
_TIMEOUT = 0.15
#  相对亮度超过这个值算浅色背景（0~1）
_LIGHT_ABOVE = 0.5


def _scale(component: str) -> float:
    """OSC 11 的每个分量是 1~4 位十六进制，按位数归一化到 0~1。"""
    return int(component, 16) / (16 ** len(component) - 1)


def relative_luminance(red: float, green: float, blue: float) -> float:
    """sRGB 相对亮度（ITU-R BT.709 权重）。绿色对人眼亮度贡献最大，
    简单取平均会把纯蓝背景误判成浅色。"""
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def parse_background(payload: bytes) -> float | None:
    """从终端回答里解析出相对亮度；不是合法回答就返回 None。"""
    match = _ANSWER.search(payload)
    if match is None:
        return None
    with contextlib.suppress(ValueError, ZeroDivisionError):
        return relative_luminance(*(_scale(part.decode()) for part in match.groups()))
    return None


def query_background() -> float | None:
    """问终端要背景色，返回相对亮度。非 tty / 不支持 / 超时都返回 None。"""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        import select
        import termios
        import tty
    except ImportError:  # Windows 没有 termios，探测直接放弃
        return None

    try:
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
    except Exception:  # noqa: BLE001 - 拿不到真实终端描述符（被替身接管、已关闭…）就放弃
        return None
    payload = b""
    try:
        #  必须显式指定 TCSANOW。tty.setraw 默认用 TCSAFLUSH，它有两个问题：
        #  ① **会丢弃待读输入**——用户在这 150ms 里抢跑打的字直接没了，
        #     而"尽量别吃掉抢跑输入"正是这次探测最需要小心的地方；
        #  ② 在 macOS 的 pty 上会直接阻塞（实测挂死，不是超时是永久卡住），
        #     启动时挂起比探测不到背景色严重得多。
        tty.setraw(fd, termios.TCSANOW)
        sys.stdout.write(_QUERY)
        sys.stdout.flush()
        #  逐字节读到终止符为止：一次 read 未必拿全，而多读一个字节就可能
        #  吃掉用户的抢跑输入，所以见到 ST/BEL 立刻停手
        while select.select([fd], [], [], _TIMEOUT)[0]:
            chunk = os.read(fd, 1)
            if not chunk:
                break
            payload += chunk
            if payload.endswith(b"\a") or payload.endswith(b"\033\\"):
                break
    except Exception:  # noqa: BLE001 - 探测失败就当终端不支持，绝不能影响启动
        return None
    finally:
        #  同样用 TCSANOW：TCSADRAIN 要等输出排空，同样有阻塞的可能。
        #  复原后 lflag 可能多出 PENDIN 位——那是内核维护的"有待重显输入"
        #  状态，不是我们设的模式，echo/icanon 都已如实还原。
        with contextlib.suppress(Exception):
            termios.tcsetattr(fd, termios.TCSANOW, saved)
    return parse_background(payload)


def detect_theme_mode() -> str | None:
    """探测出的主题模式（"dark"/"light"），拿不到返回 None。"""
    luminance = query_background()
    if luminance is None:
        return None
    return "light" if luminance > _LIGHT_ABOVE else "dark"


def autodetect() -> str | None:
    """按需探测并应用到 theme。返回实际应用的模式；没探到就不动（保持默认 dark）。

    `XIAOYU_THEME=dark|light` 显式指定时直接跳过——用户的话是最终判据。
    """
    from . import theme

    if os.environ.get("XIAOYU_THEME", "auto").strip().lower() in ("dark", "light"):
        return None
    mode = detect_theme_mode()
    if mode is not None:
        theme.set_mode(mode)
    return mode
