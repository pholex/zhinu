"""eval 框架的数据结构与断言原语。

一个 case = 一段初始文件 + 一句指令 + 一组机械可判的检查。
检查必须是客观的（文件内容、diff 范围、测试是否通过、用了哪个工具），
不要写"回答得好不好"这种主观判据。
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from xiaoyu.tools import _shell_argv


@dataclass
class Context:
    """一个 case 跑完之后交给检查函数的全部现场。"""

    workspace: Path
    #  setup 之后的文件快照：相对路径 → 内容
    before: dict[str, str]
    #  agent 的工具调用轨迹
    trace: list[dict]
    #  agent 打印出来的全部内容（含模型正文）
    transcript: str
    #  agent.send 抛出的异常（有值说明这次跑挂了）
    error: str | None = None

    def read(self, relative: str) -> str:
        path = self.workspace / relative
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8", errors="replace")

    def after(self) -> dict[str, str]:
        return snapshot(self.workspace)

    def tools_used(self) -> list[str]:
        return [entry["tool"] for entry in self.trace]

    def skills_loaded(self) -> list[str]:
        """本轮经 skill 工具加载了哪些技能（按 args["name"]，去重保序）。"""
        seen: list[str] = []
        for entry in self.trace:
            if entry.get("tool") != "skill":
                continue
            name = (entry.get("args") or {}).get("name", "")
            if name and name not in seen:
                seen.append(name)
        return seen


#  检查函数：返回 (是否通过, 说明)
Check = Callable[[Context], tuple[bool, str]]


@dataclass
class Case:
    name: str
    prompt: str
    #  在临时工作区里铺初始文件
    setup: Callable[[Path], None]
    checks: list[tuple[str, Check]]
    max_iterations: int = 20
    description: str = ""
    #  触发准确率类 case：要开技能，并铺一套隔离技能（name → SKILL.md 全文）。
    #  runner 把它们写进临时技能目录、经 XIAOYU_SKILLS_DIR 隔离加载，绝不碰
    #  跑分机器上真装的技能
    enable_skills: bool = False
    skills: dict[str, str] | None = None
    #  回归 case：断言"稳定行为，应保持 ~100%"。pass^k 门槛只对它生效——
    #  一次没过（k 次里任一次挂）就是回归门失败（退出码 2）。能力 case
    #  （默认 regression=False）起步低分正常，不进这道门
    regression: bool = False


def snapshot(root: Path) -> dict[str, str]:
    """把工作区里的文本文件抓成 相对路径 → 内容。"""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            result[str(path.relative_to(root))] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return result


# ---------- 断言原语 ----------


def file_contains(relative: str, needle: str) -> Check:
    def check(ctx: Context) -> tuple[bool, str]:
        text = ctx.read(relative)
        if not text:
            return False, f"{relative} 不存在或为空"
        return (needle in text), f"{relative} {'含有' if needle in text else '不含'} {needle!r}"

    return check


def file_not_contains(relative: str, needle: str) -> Check:
    def check(ctx: Context) -> tuple[bool, str]:
        text = ctx.read(relative)
        return (needle not in text), f"{relative} {'仍含有' if needle in text else '不含'} {needle!r}"

    return check


def file_exists(relative_glob: str) -> Check:
    def check(ctx: Context) -> tuple[bool, str]:
        hits = [p.name for p in ctx.workspace.glob(relative_glob) if p.is_file()]
        return bool(hits), f"匹配 {relative_glob} 的文件：{hits or '无'}"

    return check


def unchanged_except(*allowed: str) -> Check:
    """除了 allowed 里列出的文件，其它已有文件必须一字不改。"""

    def check(ctx: Context) -> tuple[bool, str]:
        after = ctx.after()
        touched = [
            name
            for name, content in ctx.before.items()
            if name not in allowed and after.get(name) != content
        ]
        return not touched, f"意外改动的文件：{touched or '无'}"

    return check


def max_changed_lines(relative: str, limit: int) -> Check:
    """目标文件的改动规模不能超过 limit 行（防止整文件重写）。"""

    def check(ctx: Context) -> tuple[bool, str]:
        import difflib

        before = ctx.before.get(relative, "").splitlines()
        after = ctx.read(relative).splitlines()
        delta = sum(
            1
            for line in difflib.unified_diff(before, after, n=0)
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        )
        return delta <= limit, f"{relative} 改动 {delta} 行（上限 {limit}）"

    return check


def python_syntax_ok(relative: str) -> Check:
    def check(ctx: Context) -> tuple[bool, str]:
        text = ctx.read(relative)
        if not text:
            return False, f"{relative} 不存在或为空"
        try:
            ast.parse(text)
        except SyntaxError as exc:
            return False, f"{relative} 语法错误：{exc}"
        return True, f"{relative} 语法 OK"

    return check


TESTS_PROBE = '''
"""跑工作区里的测试，unittest 和 pytest 两种风格都接受。

为什么不直接 `unittest discover`：那样只能收集 unittest.TestCase，
模型写成 pytest 风格的裸 test_* 函数会被判成 "NO TESTS RAN"。
指令里从没规定框架 —— 断言不该替模型选框架，只该确认测试真的跑通了。
"""
import importlib.util
import pathlib
import sys
import traceback
import unittest

files = sorted(pathlib.Path(".").glob("test_*.py"))
if not files:
    sys.exit("没有找到 test_*.py")

if importlib.util.find_spec("pytest") is not None:
    import pytest

    code = pytest.main(["-q", "--no-header", *[str(path) for path in files]])
    #  5 = 一个测试都没收集到，同样算失败
    if code != 0:
        sys.exit(f"pytest 退出码 {code}")
    print("pytest 通过")
    sys.exit(0)

loader = unittest.TestLoader()
suite = unittest.TestSuite()
collected = 0

for path in files:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[path.stem] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        traceback.print_exc()
        sys.exit(f"{path.name} 无法导入（依赖缺失或语法错误）")

    from_classes = loader.loadTestsFromModule(module)
    collected += from_classes.countTestCases()
    suite.addTest(from_classes)

    #  pytest 风格的裸函数：包成 FunctionTestCase 一起跑
    for name, value in vars(module).items():
        if name.startswith("test_") and callable(value) and not isinstance(value, type):
            suite.addTest(unittest.FunctionTestCase(value, description=f"{path.name}::{name}"))
            collected += 1

if collected == 0:
    sys.exit("收集到 0 个测试")

result = unittest.TextTestRunner(verbosity=1).run(suite)
if not result.wasSuccessful():
    sys.exit(f"{len(result.failures)} 失败 / {len(result.errors)} 错误")
print(f"{collected} 个测试全部通过")
'''


def tests_pass() -> Check:
    """工作区里的测试必须真的跑通（unittest / pytest 风格都算）。"""
    return python_snippet_ok(TESTS_PROBE, note="跑测试")


def python_snippet_ok(code: str, note: str = "") -> Check:
    """把一段 Python 探针跑起来（退出码 0 才算通过）。

    探针写到工作区**之外**的临时文件，用工作区作为 cwd 和 import 路径运行，
    这样既不用跟 shell 引号搏斗，也不会污染 nothing_written / unchanged_except 的快照。
    """

    def check(ctx: Context) -> tuple[bool, str]:
        script = Path(tempfile.mkdtemp(prefix="xiaoyu-probe-")) / "probe.py"
        script.write_text(textwrap.dedent(code), encoding="utf-8")
        env = dict(
            os.environ,
            PYTHONPATH=str(ctx.workspace),
            PYTHONDONTWRITEBYTECODE="1",
            #  子进程在 Windows 上默认 locale 编码，打印中文即炸
            PYTHONUTF8="1",
        )
        try:
            result = subprocess.run(
                #  Windows 上没有 python3 命令，用当前解释器最稳
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                cwd=str(ctx.workspace),
                env=env,
            )
        except subprocess.TimeoutExpired:
            return False, f"探针超时{' · ' + note if note else ''}"
        finally:
            shutil.rmtree(script.parent, ignore_errors=True)

        tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or [""]
        label = note or "行为探针"
        return result.returncode == 0, f"{label} exit={result.returncode} · {tail[0][:140]}"

    return check


def command_succeeds(command: str) -> Check:
    """在工作区里跑一条命令，退出码 0 才算通过。"""

    def check(ctx: Context) -> tuple[bool, str]:
        result = subprocess.run(
            _shell_argv(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            cwd=str(ctx.workspace),
        )
        tail = (result.stdout + result.stderr).strip().splitlines()[-1:] or [""]
        return result.returncode == 0, f"`{command}` exit={result.returncode} · {tail[0][:120]}"

    return check


def used_tool(name: str) -> Check:
    def check(ctx: Context) -> tuple[bool, str]:
        used = ctx.tools_used()
        return name in used, f"用到的工具：{used or '无'}"

    return check


def never_used_tool(name: str) -> Check:
    def check(ctx: Context) -> tuple[bool, str]:
        used = ctx.tools_used()
        return name not in used, f"用到的工具：{used or '无'}"

    return check


def loaded_skill(name: str) -> Check:
    """触发正例：模型应经 skill 工具加载指定技能（选对了）。"""

    def check(ctx: Context) -> tuple[bool, str]:
        loaded = ctx.skills_loaded()
        return name in loaded, f"加载的技能：{loaded or '无'}"

    return check


def no_skill_loaded() -> Check:
    """触发负例：模型不该加载任何技能（不该误触发）。"""

    def check(ctx: Context) -> tuple[bool, str]:
        loaded = ctx.skills_loaded()
        return not loaded, f"加载的技能：{loaded or '无'}"

    return check


def nothing_written() -> Check:
    """只读任务：不允许有任何文件变化。"""

    def check(ctx: Context) -> tuple[bool, str]:
        after = ctx.after()
        changed = [name for name in set(ctx.before) | set(after) if ctx.before.get(name) != after.get(name)]
        return not changed, f"发生变化的文件：{changed or '无'}"

    return check


def transcript_contains(needle: str) -> Check:
    """模型的**自述**里必须出现某个客观事实（数字、文件名等）。

    这是唯一的自我报告型判据（看 transcript 而非磁盘/git/测试/工具轨迹）——
    Anthropic 的评测纪律是"按最终环境状态判分，别信 agent 自述"。所以它标了
    `self_report=True`，且**必须与至少一个状态型判据配对**（见 harness 自证测试
    `每个 case 都要有状态型判据`），不能单独当一个 case 的全部证据。"""

    def check(ctx: Context) -> tuple[bool, str]:
        hit = needle in ctx.transcript
        return hit, f"回答中{'含有' if hit else '缺少'} {needle!r}"

    check.self_report = True  # type: ignore[attr-defined]

    return check


def no_tool_errors(max_errors: int = 0) -> Check:
    """工具调用不该反复报错——超过阈值说明模型在瞎试。"""

    def check(ctx: Context) -> tuple[bool, str]:
        failures = [entry["tool"] for entry in ctx.trace if not entry["ok"]]
        return len(failures) <= max_errors, f"失败的工具调用 {len(failures)} 次：{failures or '无'}"

    return check
