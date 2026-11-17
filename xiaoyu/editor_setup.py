"""给 VS Code 系编辑器配 Shift+Enter（terminal-setup 子命令）。

问题：VS Code 内置终端默认把 Shift+Enter 和 Enter 发成同一个字节，收不到
区别，所以小羽的换行只能用 Alt-Enter——而这在很多人的肌肉记忆里是错的。

做法：往编辑器的 keybindings.json 里加一条，让 Shift+Enter 直接发出
`ESC CR`。**这正是 Alt-Enter 的字节形态**（prompt_toolkit 把 ESC 前缀解析成
Escape 键，于是命中小羽已有的 ("escape", "enter") 绑定），所以不必为它新增
任何输入约定——另一种常见做法是发 `\\` + CRLF，但那要求宿主实现"行尾反斜杠
续行"，小羽没有这个约定，用它会得到一个多余的反斜杠。

安全边界：这会改工作区之外的用户配置。所以
- 只在用户显式执行 `xiaoyu terminal-setup` 时发生；
- 先打印将要做的改动，确认后才写（`--yes` 跳过确认）；
- 已经存在 shift+enter 绑定就原样保留、只报告，绝不覆盖用户自己的配置；
- 写入前留 .bak 备份。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

#  ESC + CR：prompt_toolkit 解析成 (Escape, Enter)，命中既有的 Alt-Enter 绑定
SHIFT_ENTER_SEQUENCE = "\x1b\r"

_BINDING = {
    "key": "shift+enter",
    "command": "workbench.action.terminal.sendSequence",
    "args": {"text": SHIFT_ENTER_SEQUENCE},
    "when": "terminalFocus",
}


@dataclass(frozen=True)
class Editor:
    name: str
    config_dir: str  # 各平台用户配置目录下的文件夹名


EDITORS = (
    Editor("VS Code", "Code"),
    Editor("VS Code Insiders", "Code - Insiders"),
    Editor("Cursor", "Cursor"),
    Editor("Windsurf", "Windsurf"),
)


def _user_config_root() -> Path | None:
    """各平台放编辑器用户配置的根目录。"""
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else None
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")


def keybindings_path(editor: Editor) -> Path | None:
    root = _user_config_root()
    if root is None:
        return None
    return root / editor.config_dir / "User" / "keybindings.json"


def installed_editors() -> list[tuple[Editor, Path]]:
    """装了的编辑器（以 User 目录存在为准）及其 keybindings.json 路径。"""
    found: list[tuple[Editor, Path]] = []
    for editor in EDITORS:
        path = keybindings_path(editor)
        if path is not None and path.parent.is_dir():
            found.append((editor, path))
    return found


#  keybindings.json 是 JSONC：允许 // 行注释和尾逗号，json 模块不认
_LINE_COMMENT = re.compile(r"^\s*//.*$", re.MULTILINE)
_TRAILING_COMMA = re.compile(r",(\s*[\]}])")


def parse_keybindings(text: str) -> list[dict] | None:
    """解析 JSONC 形态的 keybindings.json。解析不了返回 None——
    这时宁可什么都不做，也不能把用户手写的配置覆盖掉。"""
    stripped = _TRAILING_COMMA.sub(r"\1", _LINE_COMMENT.sub("", text))
    if not stripped.strip():
        return []
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def has_shift_enter(bindings: list[dict]) -> bool:
    """用户是否已经绑过 shift+enter（不论绑给了谁）。

    只要绑过就不动——哪怕绑的是别的命令，那也是用户的选择，
    我们无权替他改。
    """
    return any(
        isinstance(item, dict) and str(item.get("key", "")).replace(" ", "").lower() == "shift+enter"
        for item in bindings
    )


@dataclass
class Plan:
    """对一个编辑器的处置：要么写入，要么说明为什么跳过。"""

    editor: Editor
    path: Path
    action: str  # "install" / "already" / "conflict" / "unreadable" / "remove"
    detail: str = ""


def plan_for(editor: Editor, path: Path) -> Plan:
    if not path.exists():
        return Plan(editor, path, "install", "新建 keybindings.json")
    text = path.read_text(encoding="utf-8", errors="replace")
    bindings = parse_keybindings(text)
    if bindings is None:
        return Plan(editor, path, "unreadable", "解析不了这个文件，不敢动它")
    if any(item == _BINDING for item in bindings):
        return Plan(editor, path, "already", "已经配好了")
    if has_shift_enter(bindings):
        return Plan(editor, path, "conflict", "你已经自己绑过 shift+enter，保持原样")
    return Plan(editor, path, "install", f"在现有 {len(bindings)} 条绑定后追加一条")


def make_plans() -> list[Plan]:
    return [plan_for(editor, path) for editor, path in installed_editors()]


def apply(plan: Plan) -> str:
    """执行一个 install 计划，返回给用户看的一行结果。"""
    if plan.action != "install":
        raise ValueError(f"只有 install 计划能执行：{plan.action}")
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    bindings: list[dict] = []
    if plan.path.exists():
        shutil.copy2(plan.path, plan.path.with_suffix(".json.bak"))
        bindings = parse_keybindings(plan.path.read_text(encoding="utf-8", errors="replace")) or []
    bindings.append(dict(_BINDING))
    plan.path.write_text(json.dumps(bindings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"{plan.editor.name}：已写入 {plan.path}"


def removal_plans() -> list[Plan]:
    """找出小羽写入过的那条绑定，生成移除计划（uninstall 用）。

    只认严格等于 _BINDING 的项——用户改动过的绑定（哪怕只改了 when）
    已经是他自己的配置，原样保留。解析不了的文件同样不动，
    和安装侧是同一条原则。
    """
    plans: list[Plan] = []
    for editor, path in installed_editors():
        if not path.exists():
            continue
        bindings = parse_keybindings(path.read_text(encoding="utf-8", errors="replace"))
        if bindings is None:
            continue
        if any(item == _BINDING for item in bindings):
            plans.append(Plan(editor, path, "remove", "移除小羽写入的 shift+enter 绑定"))
    return plans


def apply_removal(plan: Plan) -> str:
    """执行一个 remove 计划：只删严格等于 _BINDING 的项，其余原样保留。"""
    if plan.action != "remove":
        raise ValueError(f"只有 remove 计划能执行：{plan.action}")
    shutil.copy2(plan.path, plan.path.with_suffix(".json.bak"))
    bindings = parse_keybindings(plan.path.read_text(encoding="utf-8", errors="replace")) or []
    kept = [item for item in bindings if item != _BINDING]
    plan.path.write_text(json.dumps(kept, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"{plan.editor.name}：已移除 shift+enter 绑定（{plan.path}）"
