"""eval 断言的自证测试——不打网络，纯本地。

规则：每个 case 的断言必须双向可证。
  · 喂"已知正确答案"→ 全部 PASS（否则是假失败，会让你去改本来正确的 agent）
  · 喂"看似完成但实际错"→ 必须 FAIL（否则这个 case 白测）
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xiaoyu.evals.cases import (
    CASE_FIX_AND_TEST,
    CASE_MULTI_FILE_RENAME,
    CASE_READONLY_ANSWER,
    CASE_TARGETED_EDIT,
    CASES,
)
from xiaoyu.evals.harness import Case, Context, snapshot

GOOD_CALC = '''def add(a: float, b: float) -> float:
    return a + b


def div(a: float, b: float) -> float:
    if b == 0:
        raise ValueError("division by zero is not allowed")
    return a / b
'''

GOOD_CALC_TEST = '''import unittest

from calc import add, div


class TestCalc(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_div(self):
        self.assertEqual(div(6, 3), 2)

    def test_div_zero(self):
        with self.assertRaises(ValueError):
            div(1, 0)


if __name__ == "__main__":
    unittest.main()
'''

#  反例：除零返回 inf 而不是抛异常，测试文件也是空壳
BAD_CALC = GOOD_CALC.replace(
    '        raise ValueError("division by zero is not allowed")', '        return float("inf")'
)
BAD_CALC_TEST = '''import unittest


class TestNothing(unittest.TestCase):
    def test_placeholder(self):
        pass


if __name__ == "__main__":
    unittest.main()
'''


def evaluate(case: Case, mutate, tools: tuple[str, ...], transcript: str = "") -> dict[str, str]:
    """在临时工作区里跑一遍 case 的全部断言，返回 {失败的断言: 说明}。"""
    root = Path(tempfile.mkdtemp(prefix="xiaoyu-assert-")).resolve()
    case.setup(root)
    before = snapshot(root)
    mutate(root)
    ctx = Context(
        workspace=root,
        before=before,
        trace=[{"tool": name, "ok": True, "args": {}, "output": ""} for name in tools],
        transcript=transcript,
    )
    failures: dict[str, str] = {}
    for label, check in case.checks:
        passed, detail = check(ctx)
        if not passed:
            failures[label] = detail
    return failures


class TestCaseDefinitions(unittest.TestCase):
    def test_all_cases_have_checks(self) -> None:
        for case in CASES:
            self.assertTrue(case.checks, f"{case.name} 没有任何断言")
            self.assertTrue(case.prompt.strip(), f"{case.name} 没有指令")

    def test_case_names_unique(self) -> None:
        names = [case.name for case in CASES]
        self.assertEqual(len(names), len(set(names)))


#  pytest 风格：裸函数 + 裸 assert。故意不 import pytest ——
#  断言必须在"环境里没有 pytest"时也能跑这种测试。
PYTEST_STYLE_TEST = '''from calc import add, div


def test_add():
    assert add(2, 3) == 5


def test_div():
    assert div(6, 3) == 2


def test_div_by_zero():
    try:
        div(1, 0)
    except ValueError:
        return
    raise AssertionError("应该抛异常")
'''

FAILING_TEST = '''from calc import add


def test_wrong():
    assert add(2, 3) == 999
'''

NO_TESTS_FILE = '''from calc import add


def helper():
    return add(1, 1)
'''


class TestFixAndTestAssertions(unittest.TestCase):
    def test_correct_solution_passes(self) -> None:
        def good(root: Path) -> None:
            (root / "calc.py").write_text(GOOD_CALC, encoding="utf-8")
            (root / "test_calc.py").write_text(GOOD_CALC_TEST, encoding="utf-8")

        failures = evaluate(CASE_FIX_AND_TEST, good, ("read_file", "write_file", "bash"))
        self.assertEqual(failures, {}, f"正确答案被判失败：{failures}")

    def test_pytest_style_tests_also_pass(self) -> None:
        """指令没规定框架，pytest 风格的裸函数必须同样算通过。

        回归测试：早期用 `unittest discover` 判定，12 个模型里 9 个写 pytest 风格
        被误判成 NO TESTS RAN —— 那是断言在替模型选框架。
        """

        def good(root: Path) -> None:
            (root / "calc.py").write_text(GOOD_CALC, encoding="utf-8")
            (root / "test_calc.py").write_text(PYTEST_STYLE_TEST, encoding="utf-8")

        failures = evaluate(CASE_FIX_AND_TEST, good, ("read_file", "write_file", "bash"))
        self.assertEqual(failures, {}, f"pytest 风格被误判失败：{failures}")

    def test_failing_tests_are_caught(self) -> None:
        def bad(root: Path) -> None:
            (root / "calc.py").write_text(GOOD_CALC, encoding="utf-8")
            (root / "test_calc.py").write_text(FAILING_TEST, encoding="utf-8")

        failures = evaluate(CASE_FIX_AND_TEST, bad, ("read_file", "write_file", "bash"))
        self.assertIn("测试实际能跑通", failures)

    def test_test_file_without_any_test_is_caught(self) -> None:
        """有测试文件但里面没有测试 —— 不能算"跑通"。"""

        def bad(root: Path) -> None:
            (root / "calc.py").write_text(GOOD_CALC, encoding="utf-8")
            (root / "test_calc.py").write_text(NO_TESTS_FILE, encoding="utf-8")

        failures = evaluate(CASE_FIX_AND_TEST, bad, ("read_file", "write_file", "bash"))
        self.assertIn("测试实际能跑通", failures)

    def test_returning_inf_is_caught(self) -> None:
        def bad(root: Path) -> None:
            (root / "calc.py").write_text(BAD_CALC, encoding="utf-8")
            (root / "test_calc.py").write_text(BAD_CALC_TEST, encoding="utf-8")

        failures = evaluate(CASE_FIX_AND_TEST, bad, ("read_file", "write_file", "bash"))
        self.assertIn("除零会抛异常、正常除法不受影响", failures)


class TestTargetedEditAssertions(unittest.TestCase):
    OLD = "            time.sleep(1)"
    NEW = "            if attempt < MAX_RETRIES - 1:\n                time.sleep(2 ** attempt)"

    def _patch(self, root: Path, new: str) -> None:
        path = root / "http_client.py"
        text = path.read_text(encoding="utf-8")
        assert text.count(self.OLD) == 1
        path.write_text(text.replace(self.OLD, new), encoding="utf-8")

    def test_correct_solution_passes(self) -> None:
        failures = evaluate(
            CASE_TARGETED_EDIT,
            lambda root: self._patch(root, self.NEW),
            ("read_file", "str_replace", "bash"),
        )
        self.assertEqual(failures, {}, f"正确答案被判失败：{failures}")

    def test_wrong_backoff_is_caught(self) -> None:
        #  改成了指数退避但最后一次仍然多睡一次 → 序列变成 [1,2,4,8]
        failures = evaluate(
            CASE_TARGETED_EDIT,
            lambda root: self._patch(root, "            time.sleep(2 ** attempt)"),
            ("read_file", "str_replace", "bash"),
        )
        self.assertIn("最后一次不再等待", failures)

    def test_whole_file_rewrite_is_caught(self) -> None:
        def rewrite(root: Path) -> None:
            self._patch(root, self.NEW)
            #  模拟"顺手重排了整个文件"
            path = root / "http_client.py"
            path.write_text(
                path.read_text(encoding="utf-8").replace("占位工具函数", "helper"), encoding="utf-8"
            )

        failures = evaluate(
            CASE_TARGETED_EDIT, rewrite, ("read_file", "write_file")
        )
        self.assertIn("改动不超过 8 行", failures)
        self.assertIn("没有整文件重写", failures)


class TestReadonlyAssertions(unittest.TestCase):
    def test_correct_answer_passes(self) -> None:
        failures = evaluate(
            CASE_READONLY_ANSWER, lambda root: None, ("bash", "read_file"), transcript="一共 7 个"
        )
        self.assertEqual(failures, {}, f"正确答案被判失败：{failures}")

    def test_touching_files_is_caught(self) -> None:
        def touch(root: Path) -> None:
            (root / "pkg" / "misc.py").write_text("# 手痒改了\n", encoding="utf-8")

        failures = evaluate(
            CASE_READONLY_ANSWER, touch, ("bash", "write_file"), transcript="一共 7 个"
        )
        self.assertIn("一个文件都没动", failures)
        self.assertIn("没调用 write_file", failures)

    def test_wrong_count_is_caught(self) -> None:
        failures = evaluate(
            CASE_READONLY_ANSWER, lambda root: None, ("bash",), transcript="一共 5 个"
        )
        self.assertIn("答对数量 7", failures)


class TestRenameAssertions(unittest.TestCase):
    @staticmethod
    def _rename(root: Path, files: tuple[str, ...]) -> None:
        for name in files:
            path = root / name
            path.write_text(
                path.read_text(encoding="utf-8").replace("fetch_user", "load_user"),
                encoding="utf-8",
            )

    def test_full_rename_passes(self) -> None:
        failures = evaluate(
            CASE_MULTI_FILE_RENAME,
            lambda root: self._rename(root, ("core.py", "api.py", "test_core.py")),
            ("read_file", "str_replace", "bash"),
        )
        self.assertEqual(failures, {}, f"正确答案被判失败：{failures}")

    def test_partial_rename_is_caught(self) -> None:
        #  只改了定义处，调用方和测试没动 → 测试会挂
        failures = evaluate(
            CASE_MULTI_FILE_RENAME,
            lambda root: self._rename(root, ("core.py",)),
            ("read_file", "str_replace", "bash"),
        )
        self.assertIn("api.py 无残留", failures)
        self.assertIn("测试仍然通过", failures)


class TestTestsProbeFallback(unittest.TestCase):
    """探针有两条分支：有 pytest 走 pytest，没有则退回 unittest + 裸函数。

    系统 python3 里现在（意外）装了 pytest，所以上面那些用例走的是 pytest 分支。
    这里用当前解释器（venv，没装 pytest）专门覆盖回退分支。
    """

    def _run_probe(self, files: dict[str, str]) -> subprocess.CompletedProcess:
        from xiaoyu.evals.harness import TESTS_PROBE

        workspace = Path(tempfile.mkdtemp(prefix="xiaoyu-probe-fallback-")).resolve()
        for name, content in files.items():
            (workspace / name).write_text(content, encoding="utf-8")
        probe = workspace.parent / f"{workspace.name}-probe.py"
        probe.write_text(TESTS_PROBE, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(probe)],
            capture_output=True,
            text=True,
            #  Windows 默认 locale 编码，探针打印中文会炸（与 harness 的探针同款处理）
            encoding="utf-8",
            errors="replace",
            cwd=str(workspace),
            env={**os.environ, "PYTHONPATH": str(workspace), "PYTHONUTF8": "1"},
            timeout=60,
        )

    def test_no_pytest_in_this_interpreter(self) -> None:
        self.assertIsNone(
            importlib.util.find_spec("pytest"), "本组用例要求当前解释器没有 pytest"
        )

    def test_unittest_style_passes(self) -> None:
        result = self._run_probe({"calc.py": GOOD_CALC, "test_calc.py": GOOD_CALC_TEST})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_bare_functions_pass(self) -> None:
        result = self._run_probe({"calc.py": GOOD_CALC, "test_calc.py": PYTEST_STYLE_TEST})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("全部通过", result.stdout)

    def test_failing_bare_function_fails(self) -> None:
        result = self._run_probe({"calc.py": GOOD_CALC, "test_calc.py": FAILING_TEST})
        self.assertNotEqual(result.returncode, 0)

    def test_zero_tests_fails(self) -> None:
        result = self._run_probe({"calc.py": GOOD_CALC, "test_calc.py": NO_TESTS_FILE})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("收集到 0 个测试", result.stdout + result.stderr)

    def test_unimportable_test_file_fails(self) -> None:
        result = self._run_probe(
            {"calc.py": GOOD_CALC, "test_calc.py": "import definitely_not_installed_pkg\n"}
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
