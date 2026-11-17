"""子进程文本模式必须显式声明编码——全仓静态扫描。

`text=True` 不带 `encoding` 时按 locale 解码/编码：POSIX 上恰好是 UTF-8，
所以本机永远绿；Windows 上是 cp1252/GBK，同一行代码就变成三种炸法——
写 stdin 时 UnicodeEncodeError（把 agent 直接炸掉）、读 stdout 时
UnicodeDecodeError（subprocess 的读取线程死掉、stdout 变 None）、
读 stderr 时中文被 backslashreplace 成 `\\uXXXX` 字面量（不报错，值悄悄错）。

这类 bug 只在 Windows CI 上出现，而且每处都得单独踩一遍——所以做成一条集中的
静态不变量，而不是等下一个调用点再挂一次 CI。编码是**显式选择**：全仓默认
UTF-8，个别要按用户终端编码理解的地方（`!` 直跑用户 shell）也得把
`encoding=locale.getpreferredencoding(False)` 写出来，让读代码的人看见这是选的。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCANNED = ("xiaoyu", "tests", "tests_ai")
#  这些调用都会起子进程；带 text/universal_newlines 就进入文本模式
_SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}
_TEXT_FLAGS = ("text", "universal_newlines")


def _violations(tree: ast.AST, label: str) -> list[str]:
    """返回 `<label>:<行号>` 列表：文本模式但没写 encoding= 的子进程调用。"""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _SPAWNERS:
            continue
        if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        text_mode = any(
            isinstance(keywords.get(flag), ast.Constant) and keywords[flag].value is True
            for flag in _TEXT_FLAGS
        )
        if text_mode and "encoding" not in keywords:
            found.append(f"{label}:{node.lineno}")
    return found


class SubprocessEncodingTest(unittest.TestCase):
    def test_all_text_mode_subprocess_calls_declare_encoding(self):
        offenders: list[str] = []
        scanned = 0
        for directory in _SCANNED:
            for path in sorted((_REPO / directory).rglob("*.py")):
                if "__pycache__" in path.parts:
                    continue
                scanned += 1
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                offenders += _violations(tree, str(path.relative_to(_REPO)))
        #  扫到的文件数量兜底：路径写错导致"零文件零违例"是最阴的假绿
        self.assertGreater(scanned, 50, "扫描范围可疑，几乎没读到文件")
        self.assertEqual(
            offenders,
            [],
            "以下子进程调用用了 text=True 却没写 encoding=，Windows 上按 locale "
            f"编码会炸或悄悄出错：{offenders}",
        )

    def test_detector_catches_a_planted_violation(self):
        """反向变异：扫描器本身得能 fail，别是一条永远为空的断言。"""
        bad = ast.parse("subprocess.run(['x'], text=True)\n")
        good = ast.parse("subprocess.run(['x'], text=True, encoding='utf-8')\n")
        bytes_mode = ast.parse("subprocess.run(['x'], capture_output=True)\n")
        self.assertEqual(_violations(bad, "planted"), ["planted:1"])
        self.assertEqual(_violations(good, "planted"), [])
        self.assertEqual(_violations(bytes_mode, "planted"), [])


if __name__ == "__main__":
    unittest.main()
