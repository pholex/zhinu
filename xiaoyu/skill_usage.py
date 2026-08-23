"""技能使用账本：记录哪些技能的**正文真的被送达过**，用来给索引排序。

为什么要它：装了很多技能（光 aws-core 插件包就十几个，加用户自己的常过
50）时，索引会撞上 2% 预算触发降级——而降级丢的是**尾部**。现状的尾部顺序
是"按来源分组 + 组内文件名排序"，纯属偶然：真正在用的技能可能恰好排在尾部
被丢掉，装了却永不被模型看见。按使用度排序让"用过的"排在前面、优先存活。

记账纪律（只算"正文送达"）：
- **只有 skill 工具成功加载正文**才算一次 hit（`record_load`）。索引里列出名字
  不算、/skills 浏览不算、加载失败不算——那些都不是"用了这个技能"。
- **隐式加载也算**：模型常绕过 skill 工具、直接 `cat …/SKILL.md` 或跑技能目录
  下的脚本。这类 bash 命令按 argv 里的路径归因到技能（`implicit_loads`），
  每轮每技能只记一次。不记的话账本系统性漏掉模型最爱直读的技能——而账本
  正是索引排序依据，漏记会自我强化成"越常用越排后"。归因是 best-effort
  的宽松解析（shlex），这是排序信号不是安全判定，宁可多记不可漏记。

排序键：`(hits 降序, 最近使用/文件 mtime 降序)`。未用过的技能 hits=0，靠
SKILL.md 的 mtime 兜底排序——刚装的新技能不会被一堆"老而没用过"的技能压在
最底下（冷启动可发现性），又不需要任何魔法时间窗。
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import re
import shlex
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

from .config import user_config_dir

if TYPE_CHECKING:
    from .skills import Skill

#  账本落盘位置。坏了/读不出一律当空账本——排序退化回来源顺序，不影响功能。
_USAGE_FILE = "skill-usage.json"
#  写盘去抖：进程内合并到内存计数，每这么久最多落盘一次，避免连续加载狂写盘。
_FLUSH_INTERVAL = 5.0
#  账本条目上限：技能被删/改名后旧条目会残留，超过就按最近使用裁剪，防无界增长。
_MAX_ENTRIES = 500

_lock = threading.Lock()
_counts: dict[str, dict] | None = None  # {name: {"hits": int, "last": epoch}}
_dirty = False
_last_flush = 0.0


def _path() -> Path:
    return user_config_dir() / _USAGE_FILE


def _load() -> dict[str, dict]:
    global _counts
    if _counts is not None:
        return _counts
    data: dict[str, dict] = {}
    with contextlib.suppress(OSError, ValueError):
        raw = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            for name, entry in raw.items():
                if isinstance(entry, dict) and isinstance(entry.get("hits"), int):
                    data[str(name)] = {
                        "hits": entry["hits"],
                        "last": float(entry.get("last", 0.0)),
                    }
    _counts = data
    return data


def _flush_locked() -> None:
    global _dirty, _last_flush
    counts = _counts or {}
    #  超限时按 last 保留最近使用的一批
    if len(counts) > _MAX_ENTRIES:
        keep = sorted(counts.items(), key=lambda kv: kv[1].get("last", 0.0), reverse=True)
        counts = dict(keep[:_MAX_ENTRIES])
        _counts.clear()
        _counts.update(counts)
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        #  同目录唯一临时名（带 pid）+ 原子改名：并发写（子 agent/七襄）不撞
        tmp = path.with_name(f".{_USAGE_FILE}.{os.getpid()}")
        tmp.write_text(json.dumps(counts, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        _dirty = False
        _last_flush = time.monotonic()
    except OSError:
        #  写不进去（只读盘/配额）不抛：账本是锦上添花，丢了只是排序退化
        pass


def record_load(name: str) -> None:
    """记一次"技能正文被成功送达"。best-effort，绝不抛。"""
    global _dirty
    with _lock:
        counts = _load()
        entry = counts.setdefault(name, {"hits": 0, "last": 0.0})
        entry["hits"] += 1
        entry["last"] = time.time()
        _dirty = True
        if time.monotonic() - _last_flush >= _FLUSH_INTERVAL:
            _flush_locked()


#  命令段分隔：按这些切开后各段独立 shlex。不求严格（引号内的 && 切错只影响
#  归因，不影响执行），求的是 `cat a && python b` 两段都看得到。
_SEGMENT_SPLIT = re.compile(r"\|\||&&|[;|\n]")


def implicit_loads(command: str, skills: list["Skill"], cwd: Path | None = None) -> list[str]:
    """从一条 bash 命令里找出被隐式使用的技能名（去重、按出现顺序）。

    判定：argv 中任一词解析成路径后落在某技能目录（SKILL.md 所在目录）之内——
    读正文、读 references/、跑 scripts/ 都算"用了这个技能"。`~` 与 `$HOME`
    手工展开（技能目录几乎总在 home 下）；其它变量不展开，解析不了的段跳过。
    """
    if not skills or not command.strip():
        return []
    home = os.path.expanduser("~")
    roots: list[tuple[str, str]] = []
    for skill in skills:
        try:
            roots.append((os.path.realpath(skill.path.parent), skill.name))
        except OSError:
            continue
    found: list[str] = []
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            words = shlex.split(segment, comments=True)
        except ValueError:
            continue
        for word in words:
            if "/" not in word and word != "SKILL.md":
                continue
            word = word.replace("$HOME", home).replace("${HOME}", home)
            if word.startswith("~"):
                word = os.path.expanduser(word)
            path = word if os.path.isabs(word) else os.path.join(str(cwd or Path.cwd()), word)
            try:
                real = os.path.realpath(path)
            except (OSError, ValueError):
                continue
            for root, name in roots:
                if real == root or real.startswith(root + os.sep):
                    if name not in found:
                        found.append(name)
                    break
    return found


def flush() -> None:
    """把内存里未落盘的计数写下去（进程退出/测试收尾用）。"""
    with _lock:
        if _dirty:
            _flush_locked()


#  去抖窗口内的尾部 hit 靠退出时兜底落盘（丢了只是排序退化，但便宜就顺手做）
atexit.register(flush)


def counts() -> dict[str, dict]:
    """当前使用计数快照（只读用途，返回浅拷贝）。"""
    with _lock:
        return {name: dict(entry) for name, entry in _load().items()}


def _reset_for_test() -> None:
    """测试隔离：清进程内缓存，强制下次从盘重读。"""
    global _counts, _dirty, _last_flush
    with _lock:
        _counts = None
        _dirty = False
        _last_flush = 0.0


def ranked(skills: list["Skill"], usage: dict[str, dict] | None = None) -> list["Skill"]:
    """按使用度给技能排序：用得多的在前；未用过的按 SKILL.md mtime 新的在前。

    稳定、确定（无随机）：同一账本 + 同一批文件必得同一顺序，索引在会话内不抖
    （system prompt 是 prefix cache 的前缀）。usage 省略时现读账本。
    """
    if usage is None:
        usage = counts()

    def mtime(skill: "Skill") -> float:
        try:
            return skill.path.stat().st_mtime
        except OSError:
            return 0.0

    #  hits 主序、last/mtime 次序都降序；原顺序做最终稳定兜底（Python sort 稳定，
    #  等键项保持输入相对次序，即来源分组顺序）
    def key(skill: "Skill") -> tuple[int, float]:
        entry = usage.get(skill.name)
        if entry and entry.get("hits", 0) > 0:
            return (entry["hits"], entry.get("last", 0.0))
        return (0, mtime(skill))

    return sorted(skills, key=key, reverse=True)
