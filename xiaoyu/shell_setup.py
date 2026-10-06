"""把终端集成写进 shell 启动文件（`xiaoyu term install` / `term uninstall`）。

`term init` 只负责把脚本打到 stdout，"放进 ~/.zshrc" 那一步原来要用户自己
动手：找对文件、写对引号、别重复加。这里把它做成一条命令。

安全边界与 editor_setup 同一套：这会改工作区之外的用户配置，所以
- 只在用户显式执行 `xiaoyu term install` 时发生；
- 先打印将要做的改动，确认后才写（`--yes` 跳过确认）；
- 只认、只改自己写下的那一段（首尾各一行标记）；用户自己手写过
  `term init` 那一行就原样保留、只报告，绝不替他改；
- 写入前留 .bak 备份。

PowerShell 不在这里：$PROFILE 的位置随版本（5.1 / 7）与宿主而变，猜错了
写进去也不生效，不如让用户照文档贴一行。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

#  install 能写的 shell；PowerShell 见模块说明
SHELLS = ("zsh", "bash", "fish")

BEGIN = "# >>> xiaoyu 终端集成（xiaoyu term install 写入，xiaoyu term uninstall 移除）>>>"
END = "# <<< xiaoyu 终端集成 <<<"

#  用户自己手写的那一行：不在标记段里、但调了 `xiaoyu … term init`
_MANUAL = re.compile(r"^[^#\n]*\bxiaoyu\b[^\n]*\bterm\s+init\b", re.MULTILINE)


def detect_shell() -> str | None:
    """从 $SHELL 认出登录 shell；认不出（Windows、没设、冷门 shell）返回 None。"""
    name = Path(os.environ.get("SHELL", "")).name
    return name if name in SHELLS else None


def rc_path(shell: str) -> Path:
    """这个 shell 每次开交互终端都会读的那个启动文件。

    bash 在 macOS 上写 ~/.bash_profile：Terminal / iTerm 开的是登录 shell，
    只读 .bash_profile，不读 .bashrc（Linux 的终端开的是非登录 shell，反过来）。
    """
    home = Path.home()
    if shell == "zsh":
        return Path(os.environ.get("ZDOTDIR") or home) / ".zshrc"
    if shell == "bash":
        return home / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    if shell == "fish":
        return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "fish" / "config.fish"
    raise ValueError(f"term install 不支持 {shell}（可选 {', '.join(SHELLS)}）")


def _candidate_paths() -> list[Path]:
    """uninstall / doctor 要看的所有启动文件（两个平台的 bash 文件都看）。"""
    home = Path.home()
    paths = [rc_path("zsh"), home / ".bash_profile", home / ".bashrc", rc_path("fish")]
    return list(dict.fromkeys(paths))


def init_line(
    shell: str,
    launcher: str,
    *,
    name: str | None = None,
    command_not_found: bool = False,
    natural: bool = False,
) -> str:
    """启动文件里那一行：每开一个终端现生成脚本，升级小羽后自动用上新版。"""
    quote = shlex.quote
    if shell == "fish":
        from .term import _fish_quote as quote
    args = [launcher, "term", "init", shell]
    if name:
        args += ["--name", quote(name)]
    if command_not_found:
        args.append("--command-not-found")
    if natural:
        args.append("--natural")
    command = " ".join(args)
    return f"{command} | source" if shell == "fish" else f'eval "$({command})"'


def block(line: str) -> str:
    return f"{BEGIN}\n{line}\n{END}\n"


def _find_block(text: str) -> tuple[int, int] | None | str:
    """标记段的 [起, 止)（止含 END 行的换行）；没有返回 None，残缺返回 "broken"。"""
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 and end < 0:
        return None
    if start < 0 or end < start or text.count(BEGIN) > 1 or text.count(END) > 1:
        return "broken"
    stop = end + len(END)
    if text[stop : stop + 1] == "\n":
        stop += 1
    return start, stop


@dataclass
class Plan:
    """对一个启动文件的处置：要么写，要么说明为什么不动。"""

    path: Path
    action: str  # install / update / already / manual / broken / remove
    detail: str = ""
    block: str = ""


def plan_install(shell: str, line: str, path: Path | None = None) -> Plan:
    path = rc_path(shell) if path is None else path
    wanted = block(line)
    if not path.exists():
        return Plan(path, "install", "新建文件并写入", wanted)
    text = path.read_text(encoding="utf-8", errors="replace")
    found = _find_block(text)
    if found == "broken":
        return Plan(path, "broken", "小羽的标记段不完整（被手改过？），不敢动它")
    if found is not None:
        start, stop = found
        if text[start:stop] == wanted:
            return Plan(path, "already", "已经配好了")
        return Plan(path, "update", "替换之前写入的那一段", wanted)
    if _MANUAL.search(text):
        return Plan(path, "manual", "里面已经有你自己写的 term init，保持原样（要改就手动改那一行）")
    return Plan(path, "install", "追加到文件末尾", wanted)


def _backup(path: Path) -> None:
    shutil.copy2(path, path.with_name(path.name + ".bak"))


def apply(plan: Plan) -> str:
    """执行 install / update 计划，返回给用户看的一行结果。"""
    if plan.action not in ("install", "update"):
        raise ValueError(f"只有 install / update 计划能执行：{plan.action}")
    path = plan.path
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(plan.block, encoding="utf-8")
        return f"已写入 {path}"
    text = path.read_text(encoding="utf-8", errors="replace")
    _backup(path)
    found = _find_block(text)
    if isinstance(found, tuple):
        start, stop = found
        text = text[:start] + plan.block + text[stop:]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += ("\n" if text else "") + plan.block
    path.write_text(text, encoding="utf-8")
    return f"已写入 {path}（原文件备份在 {path.name}.bak）"


def removal_plans() -> list[Plan]:
    """找出写过标记段的启动文件，生成移除计划（term uninstall / xiaoyu uninstall 用）。

    只认完整的标记段；用户手写的 term init 行是他自己的配置，原样保留。
    """
    plans: list[Plan] = []
    for path in _candidate_paths():
        if not path.is_file():
            continue
        if isinstance(_find_block(path.read_text(encoding="utf-8", errors="replace")), tuple):
            plans.append(Plan(path, "remove", "移除小羽写入的终端集成"))
    return plans


def apply_removal(plan: Plan) -> str:
    if plan.action != "remove":
        raise ValueError(f"只有 remove 计划能执行：{plan.action}")
    text = plan.path.read_text(encoding="utf-8", errors="replace")
    found = _find_block(text)
    if not isinstance(found, tuple):
        return f"{plan.path} 里已经没有小羽写入的段落"
    _backup(plan.path)
    start, stop = found
    head = text[:start]
    #  install 追加时在前面垫过一个空行，一起收走
    if head.endswith("\n\n"):
        head = head[:-1]
    plan.path.write_text(head + text[stop:], encoding="utf-8")
    return f"已从 {plan.path} 移除终端集成（原文件备份在 {plan.path.name}.bak）"


def installed_in() -> list[tuple[Path, str]]:
    """哪些启动文件接入了终端集成：("marked" 小羽写的 / "manual" 用户手写的)。"""
    found: list[tuple[Path, str]] = []
    for path in _candidate_paths():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if isinstance(_find_block(text), tuple):
            found.append((path, "marked"))
        elif _MANUAL.search(text):
            found.append((path, "manual"))
    return found
