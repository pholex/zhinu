"""交互模式：确认 / auto / plan —— 一张表定义顺序、标签与放行规则。

小羽原先的授权只有两极：**逐条确认**和 `--yolo` 全放行。中间空着，实践里就只有
两种活法——一路按 y，或者在一次性目录里裸奔。auto 档补的是中间地带：
常规改动不问，越界时才拦住你。**出厂起始档就是 auto**（`INITIAL`）：沙箱兜得住的
不问，是绝大多数人打开小羽想要的样子；要每条都过目的人 `XIAOYU_MODE=default` 或
Shift+Tab 切到确认档。`default` 这个稳定标识沿用（进配置、进日志），只是它不再
是"默认"，给用户看的标签叫「确认」。

**auto 的前提是沙箱，不是信任。** `sandbox.py` 那层 Seatbelt/bubblewrap 原本只做
纵深防御的兜底，auto 档把它变成"自动放行的依据"——命令跑在只能写工作区的策略里，
所以不必逐条问。沙箱不可用（Windows、没装 bwrap、`--no-sandbox`）时前提不成立，
auto 档就只剩"工作区内改文件免确认"，bash 照旧逐条问，且**必须明说**（见 `describe`）。

⚠️ **auto 档不解决凭据外泄**：沙箱默认全盘可读、允许联网，所以 auto 档下模型可以
不经询问地读 `~/.ssh`、`~/.aws` 并 curl 出去。介意就 `XIAOYU_SANDBOX_NETWORK=0`
（断网）或干脆别用 auto 档。这是明确接受的取舍，不是疏漏。

照 `keys.py` 的路子把模式做成数据：顺序、标签、提示符标记、说明只在这张表里写一次，
TUI 提示符 / `/mode` / `/keys` / 启动横幅全从它渲染，不再有第二处会漂移的文案。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import command_check

DEFAULT = "default"
AUTO = "auto"
PLAN = "plan"
#  出厂起始档（Config.mode 的默认值、退出 plan 无处可回时的落点）。
#  名字刻意不叫 DEFAULT：那个是"确认档"的稳定标识，历史包袱，改不得
INITIAL = AUTO


@dataclass(frozen=True)
class Mode:
    """一个模式。`name` 是稳定标识（进配置、进事件日志），改文案不改它。"""

    name: str
    marker: str  # 提示符前缀，空串=默认档不加装饰
    label: str  # 给用户看的短名
    desc: str  # 一句话说明：这一档到底会不会问你
    style: str = "prompt"  # theme token，两个前端取同一个（颜色不在前端各写一份）


MODES: tuple[Mode, ...] = (
    Mode(DEFAULT, "", "确认", "写文件和执行命令逐条确认"),
    Mode(
        AUTO, "⏵⏵", "auto",
        "工作区内改文件、沙箱内跑命令都自动放行；危险命令、提权、写到工作区外仍要你确认",
        style="text.accent",
    ),
    Mode(
        PLAN, "⏸", "plan", "只读规划态，只调研不动手，模型交计划后要你批准才执行",
        style="status.warning",
    ),
)

#  Shift+Tab 的循环顺序。刻意不含 --yolo：那一档没有沙箱兜底，
#  不该是"手滑多按一下"就能进的。
CYCLE: tuple[str, ...] = tuple(item.name for item in MODES)

BY_NAME: dict[str, Mode] = {item.name: item for item in MODES}


def get(name: str) -> Mode:
    """按名字取模式；名字不认识退回确认档（配置里写错了不该炸主循环，
    退到最保守的一档而不是出厂的 auto——写错配置不该变成放权）。"""
    return BY_NAME.get(name, BY_NAME[DEFAULT])


def next_mode(current: str) -> str:
    """循环里的下一档。"""
    try:
        index = CYCLE.index(current)
    except ValueError:
        return CYCLE[0]
    return CYCLE[(index + 1) % len(CYCLE)]


def marker(name: str) -> str:
    return get(name).marker


def label(name: str) -> str:
    return get(name).label


def prompt_prefix(name: str) -> str:
    """提示符前缀（含尾随空格）；确认档为空，auto/plan 带标记——auto 虽是出厂
    起始档，"命令会自动跑"这件事值得一直挂在眼前。

    模式标记长在提示符上而不是常驻状态栏——bottom_toolbar 只在输入行挂起时
    存在、模型一跑就消失，那条路小羽走过又拆了（见 tui.py 模块文档）。
    """
    mode = get(name)
    return f"{mode.marker} {mode.label} " if mode.marker else ""


def desc(name: str, *, sandbox_ready: bool = True) -> str:
    """一句话说明。auto 档而沙箱不可用时必须说实话：静默降级（用户以为命令
    自动跑了，其实每条还在等他按键，或者反过来以为有沙箱护着）是最坏的失败
    方式。describe 与 ACP 的模式下拉（acp.mode_state）共用这一份文案。
    """
    mode = get(name)
    if mode.name == AUTO and not sandbox_ready:
        return (
            "本机沙箱不可用，只有工作区内的改文件会自动放行，"
            "bash 命令仍逐条确认（沙箱是 auto 放行命令的前提）"
        )
    return mode.desc


def describe(name: str, *, sandbox_ready: bool = True) -> str:
    """切进某一档时给用户的那句话。"""
    mode = get(name)
    text = desc(name, sandbox_ready=sandbox_ready)
    body = f"{mode.marker} {mode.label} 档：{text}" if mode.marker else f"{mode.label}档：{text}"
    return body.strip()


def help_text() -> str:
    """`/mode` 不带参数时打印的三档说明。

    对齐按可见宽度算："确认"两个汉字占 4 列而 len() 只有 2，用 len 会错位
    （与 keys.help_text 同一个坑、同一个解法）。
    """
    from . import ui

    width = max(ui.display_width(item.label) for item in MODES)
    return "\n".join(f"  {ui.pad(item.label, width)}  {item.desc}" for item in MODES)


# ---------- auto 档的放行判定 ----------

#  auto 档认识的工具名。其余（browser / MCP / 插件工具）一律 fail-closed 走确认：
#  它们的副作用在沙箱管辖之外，"沙箱兜底所以不用问"这个前提对它们不成立。
_EDIT_TOOLS = frozenset({"write_file", "str_replace"})


def auto_approves(
    name: str,
    args: dict[str, Any],
    *,
    outside_workspace: bool,
    sandbox_ready: bool,
) -> bool:
    """auto 档是否免确认这次调用。fail-closed：不在白名单里的一律 False。

    调用方负责把两个外部事实喂进来，本函数保持纯净（好测、不依赖 Agent）：
    - `outside_workspace`：路径参数是否逃出工作区（`Toolbox.outside_workspace`）
    - `sandbox_ready`：这次 bash 真的会被套沙箱吗（必须用 `sandbox.enabled`，
      与 `tools.py` 执行时**同一个谓词**——否则会出现"以为在沙箱里所以放行了、
      实际没套上"）

    不负责 deny 规则、plan mode、危险命令硬拦截：那些在 `Agent._execute` 里
    先于本函数生效，本函数只回答"这次要不要弹确认框"。
    """
    if name in _EDIT_TOOLS:
        #  改动在工作区内 = 可见、可 git 回滚。越界就该有人看着。
        return not outside_workspace
    if name == "bash":
        if not sandbox_ready:
            return False
        command = str(args.get("command", "") or "")
        if not command.strip():
            return False
        #  沙箱救不了工作区内的 `rm -rf`：写权限本来就开着
        if command_check.dangerous_command(command):
            return False
        #  提权是唯一可能捅穿沙箱的动作（多半也会直接失败），别让它无声发生
        if command_check.privileged_command(command):
            return False
        return True
    #  读工具越界（read_file/grep/list_files 读到工作区外）走到这里：
    #  保住"读 ~/.ssh 会弹窗"这条线，auto 档不放行。
    return False
