"""把小羽接进 shell 启动文件（`xiaoyu term install` / `term uninstall`）。

一段里写两样：终端集成（`term init`：@x / @c 与记命令的钩子）和 Tab 补全
（`completion`）。用户眼里这是同一件事——"把小羽接进 shell"——分成两条命令
各贴一行，只会让人漏掉一半（zsh 还得自己先 compinit）。

安全边界与 editor_setup 同一套：这会改工作区之外的用户配置，所以
- 只在用户显式执行 `xiaoyu term install` 时发生；
- 先打印将要做的改动，确认后才写（`--yes` 跳过确认）；
- 只认、只改自己写下的那一段（首尾各一行标记）；用户手写过 `term init`
  那一行就原样保留、只报告，绝不替他改；
- 写入前留 .bak 备份；读写都不碰换行符（Windows 上 Python 默认把写出的
  \\n 换成 \\r\\n，在 bash 的启动文件里混进 \\r 只会坏事）。

各平台的差异：
- macOS：bash 写 ~/.bash_profile（Terminal / iTerm 开登录 shell，不读 .bashrc）；
- Linux 与 Windows 上的 Git Bash：bash 写 ~/.bashrc；
- zsh 认 $ZDOTDIR；补全依赖 compinit，没人调过就由这一段补上；
- PowerShell 不在这里：$PROFILE 的位置随版本（5.1 / 7）与宿主而变，猜错了
  写进去也不生效；小羽也没有 PowerShell 补全。照文档贴一行即可。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

#  install 能写的 shell；PowerShell 见模块说明
SHELLS = ("zsh", "bash", "fish")

BEGIN = "# >>> xiaoyu 终端集成（xiaoyu term install 写入，xiaoyu term uninstall 移除）>>>"
END = "# <<< xiaoyu 终端集成 <<<"

#  用户自己手写的那一行：不在标记段里、但调了 `xiaoyu … term init`
_MANUAL = re.compile(r"^[^#\n]*\bxiaoyu\b[^\n]*\bterm\s+init\b", re.MULTILINE)
_COMPLETION = re.compile(r"^[^#\n]*\bxiaoyu\b[^\n]*\bcompletion\s+(?:zsh|bash|fish)\b", re.MULTILINE)

#  zsh 的补全脚本要 compdef：oh-my-zsh 之类早就 compinit 过了就别再来一遍（拖慢启动）
_ZSH_COMPINIT = "(( $+functions[compdef] )) || { autoload -Uz compinit && compinit }"


def detect_shell() -> str | None:
    """从 $SHELL 认出登录 shell；认不出（Windows 原生终端、没设、冷门 shell）返回 None。"""
    name = Path(os.environ.get("SHELL", "")).name
    if name.endswith(".exe"):  # Git Bash 下偶见 /usr/bin/bash.exe
        name = name[:-4]
    return name if name in SHELLS else None


def rc_path(shell: str) -> Path:
    """这个 shell 每次开交互终端都会读的那个启动文件（各平台差异见模块说明）。"""
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


def _run_line(shell: str, command: str) -> str:
    return f"{command} | source" if shell == "fish" else f'eval "$({command})"'


def integration_lines(
    shell: str,
    launcher: str,
    *,
    name: str | None = None,
    command_not_found: bool = False,
    natural: bool = False,
    completion: bool = True,
) -> tuple[list[str], list[str]]:
    """标记段里的那几行，以及给用户看的说明（为什么少了哪一行）。

    每开一个终端现生成脚本，升级小羽后自动用上新版。
    """
    quote = shlex.quote
    if shell == "fish":
        from .term import _fish_quote as quote
    lines: list[str] = []
    notes: list[str] = []
    if completion:
        #  补全挂在命令名 xiaoyu 上：PATH 里没有这个命令（venv 没激活、pipx 的
        #  bin 不在 PATH），补全脚本装上了也永远触发不到
        if launcher != "xiaoyu":
            notes.append("PATH 上找不到 xiaoyu 命令，Tab 补全没写（补全挂在命令名上，触发不到）")
        else:
            if shell == "zsh":
                lines.append(_ZSH_COMPINIT)
            lines.append(_run_line(shell, f"xiaoyu completion {shell}"))
    args = [launcher, "term", "init", shell]
    if name:
        args += ["--name", quote(name)]
    if command_not_found:
        args.append("--command-not-found")
    if natural:
        args.append("--natural")
    lines.append(_run_line(shell, " ".join(args)))
    return lines, notes


def block(lines: list[str]) -> str:
    return "\n".join([BEGIN, *lines, END]) + "\n"


def _read(path: Path) -> str:
    #  newline="" 原样读：CRLF 文件里的 \r 留在文本里，写回去还是 CRLF
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        return handle.read()


def _write(path: Path, text: str) -> None:
    #  newline="" 原样写：不让 Windows 把我们的 \n 换成 \r\n
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _find_block(text: str) -> tuple[int, int] | None | str:
    """标记段的 [起, 止)（止含 END 行的换行）；没有返回 None，残缺返回 "broken"。"""
    start = text.find(BEGIN)
    end = text.find(END)
    if start < 0 and end < 0:
        return None
    if start < 0 or end < start or text.count(BEGIN) > 1 or text.count(END) > 1:
        return "broken"
    stop = end + len(END)
    for newline in ("\r\n", "\n"):
        if text.startswith(newline, stop):
            stop += len(newline)
            break
    return start, stop


@dataclass
class Plan:
    """对一个启动文件的处置：要么写，要么说明为什么不动。"""

    path: Path
    action: str  # install / update / already / manual / broken / remove
    detail: str = ""
    block: str = ""
    notes: list[str] = field(default_factory=list)


def plan_install(shell: str, lines: list[str], path: Path | None = None) -> Plan:
    path = rc_path(shell) if path is None else path
    wanted = block(lines)
    if not path.exists():
        return Plan(path, "install", "新建文件并写入", wanted)
    text = _read(path)
    found = _find_block(text)
    if found == "broken":
        return Plan(path, "broken", "小羽的标记段不完整（被手改过？），不敢动它")
    if found is not None:
        start, stop = found
        if text[start:stop].replace("\r\n", "\n") == wanted:
            return Plan(path, "already", "已经配好了")
        return Plan(path, "update", "替换之前写入的那一段", wanted)
    if _MANUAL.search(text):
        notes = []
        #  term init 永远是最后一行，前面的都属于补全（zsh 还有 compinit 那行）
        completion = lines[:-1]
        if completion and not _COMPLETION.search(text):
            notes.append("要 Tab 补全的话，在你那行 term init 前面手动加上：")
            notes.extend("  " + line for line in completion)
        return Plan(
            path, "manual", "里面已经有你自己写的 term init，保持原样（要改就手动改那一行）",
            notes=notes,
        )
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
        _write(path, plan.block)
        return f"已写入 {path}"
    text = _read(path)
    eol = _eol(text)
    wanted = plan.block.replace("\n", eol)
    _backup(path)
    found = _find_block(text)
    if isinstance(found, tuple):
        start, stop = found
        text = text[:start] + wanted + text[stop:]
    else:
        if text and not text.endswith("\n"):
            text += eol
        text += (eol if text else "") + wanted
    _write(path, text)
    return f"已写入 {path}（原文件备份在 {path.name}.bak）"


def removal_plans() -> list[Plan]:
    """找出写过标记段的启动文件，生成移除计划（term uninstall / xiaoyu uninstall 用）。

    只认完整的标记段；用户手写的 term init 行是他自己的配置，原样保留。
    """
    plans: list[Plan] = []
    for path in _candidate_paths():
        if path.is_file() and isinstance(_find_block(_read(path)), tuple):
            plans.append(Plan(path, "remove", "移除小羽写入的终端集成"))
    return plans


def apply_removal(plan: Plan) -> str:
    if plan.action != "remove":
        raise ValueError(f"只有 remove 计划能执行：{plan.action}")
    text = _read(plan.path)
    found = _find_block(text)
    if not isinstance(found, tuple):
        return f"{plan.path} 里已经没有小羽写入的段落"
    _backup(plan.path)
    start, stop = found
    head = text[:start]
    #  install 追加时在前面垫过一个空行，一起收走
    eol = _eol(text)
    if head.endswith(eol + eol):
        head = head[: -len(eol)]
    _write(plan.path, head + text[stop:])
    return f"已从 {plan.path} 移除终端集成（原文件备份在 {plan.path.name}.bak）"


@dataclass
class Found:
    path: Path
    kind: str  # marked（小羽写的）/ manual（用户手写的）
    completion: bool  # 有没有接上 Tab 补全


def installed_in() -> list[Found]:
    """哪些启动文件接入了终端集成，补全接没接上。"""
    found: list[Found] = []
    for path in _candidate_paths():
        if not path.is_file():
            continue
        text = _read(path)
        located = _find_block(text)
        if isinstance(located, tuple):
            found.append(Found(path, "marked", bool(_COMPLETION.search(text))))
        elif _MANUAL.search(text):
            found.append(Found(path, "manual", bool(_COMPLETION.search(text))))
    return found
