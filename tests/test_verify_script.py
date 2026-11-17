"""scripts/verify.py：与 ci.yml 的对账、密钥扫描、计划与汇总（不跑真实步骤、不打网络）。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("verify_script", REPO / "scripts" / "verify.py")
verify = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
#  先登记进 sys.modules：dataclass 解析 from __future__ annotations 时要按模块名回查
sys.modules[_SPEC.name] = verify
_SPEC.loader.exec_module(verify)


class CiAlignmentTest(unittest.TestCase):
    def test_secret_pattern_is_the_one_in_ci(self):
        """ci.yml 的 grep -E 正则与脚本常量逐字相同——两处各改一套就是第二套扫描器。"""
        text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        match = re.search(r'grep -rniE "([^"]+)" /tmp/audit', text)
        self.assertIsNotNone(match, "ci.yml 里找不到密钥扫描那条 grep")
        self.assertEqual(match.group(1), verify.SECRET_PATTERN)

    def test_every_step_names_its_ci_counterpart(self):
        for step in verify.STEPS:
            with self.subTest(step=step.name):
                self.assertTrue(step.ci_ref)
                self.assertIn("job", step.ci_ref)


class SecretScanTest(unittest.TestCase):
    def test_finds_planted_key_and_passes_clean_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "clean.py").write_text("key = os.environ['X']\n", encoding="utf-8")
            self.assertEqual(verify.scan_secrets(root), [])
            #  拼接写法：源码里不能出现成串的密钥字面量
            planted = "token = '" + "sk-" + "abcdefghij123456" + "'\n"
            (root / "sub").mkdir()
            (root / "sub" / "leak.txt").write_text("ok\n" + planted, encoding="utf-8")
            hits = verify.scan_secrets(root)
            self.assertEqual(len(hits), 1)
            self.assertTrue(hits[0].startswith("sub/leak.txt:2: "), hits[0])

    def test_private_key_header_is_case_insensitive_like_grep_i(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "k.pem").write_text("-----begin rsa " + "private key-----\n", encoding="utf-8")  # 拼接：提交检查不认整串
            self.assertEqual(len(verify.scan_secrets(Path(tmp))), 1)


class PlanTest(unittest.TestCase):
    def test_only_filters_and_keeps_ci_order(self):
        names = [step.name for step, _ in verify.plan(["secrets", "unittest"])]
        self.assertEqual(names, ["unittest", "secrets"])

    def test_bwrap_step_is_platform_gated(self):
        with mock.patch.object(verify.sys, "platform", "darwin"):
            reasons = {step.name: reason for step, reason in verify.plan()}
        self.assertIsNotNone(reasons["bwrap"])
        self.assertTrue(all(reasons[name] is None for name in reasons if name != "bwrap"))
        with mock.patch.object(verify.sys, "platform", "linux"):
            self.assertIsNone(dict((s.name, r) for s, r in verify.plan())["bwrap"])

    def test_list_prints_plan_without_running(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = verify.main(["--list"])
        self.assertEqual(code, 0)
        for name in verify.STEP_NAMES:
            self.assertIn(name, out.getvalue())

    def test_unknown_step_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                verify.main(["--only", "nope"])
        self.assertEqual(caught.exception.code, 2)


class RunStepsTest(unittest.TestCase):
    """用假步骤驱动汇总与退出码：FAIL 才非零，WARN/SKIP 不算；步骤抛异常计 FAIL。"""

    @staticmethod
    def _steps(*items):
        return tuple(
            verify.Step(name, name, "fake job", run, platform_skip=skip)
            for name, run, skip in items
        )

    def _run(self, steps):
        ctx = verify.Context()
        out = io.StringIO()
        with mock.patch.object(verify, "STEPS", steps), contextlib.redirect_stdout(out):
            code = verify.run_steps(ctx)
        return code, ctx, out.getvalue()

    def test_warn_and_skip_do_not_fail(self):
        steps = self._steps(
            ("a", lambda ctx: verify.Result(verify.PASS), lambda: None),
            ("b", lambda ctx: verify.Result(verify.WARN, "慢"), lambda: None),
            ("c", lambda ctx: verify.Result(verify.SKIP, "缺工具"), lambda: None),
            ("d", lambda ctx: verify.Result(verify.PASS), lambda: "别的平台"),
        )
        code, ctx, out = self._run(steps)
        self.assertEqual(code, 0)
        self.assertEqual(ctx.results["d"].status, verify.SKIP)
        self.assertIn("别的平台", ctx.results["d"].detail)
        self.assertIn("门禁通过", out)

    def test_fail_and_exception_make_exit_nonzero(self):
        def boom(ctx):
            raise RuntimeError("步骤自己坏了")

        steps = self._steps(
            ("a", lambda ctx: verify.Result(verify.FAIL, "退出码 1"), lambda: None),
            ("b", boom, lambda: None),
            ("c", lambda ctx: verify.Result(verify.PASS), lambda: None),
        )
        code, ctx, out = self._run(steps)
        self.assertEqual(code, 1)
        self.assertEqual(ctx.results["b"].status, verify.FAIL)
        self.assertIn("步骤自己坏了", ctx.results["b"].detail)
        self.assertIn("门禁未过：a, b", out)

    def test_build_dependents_skip_without_artifacts(self):
        ctx = verify.Context()
        self.assertEqual(verify.step_twine(ctx).status, verify.SKIP)
        self.assertEqual(verify.step_wheel_smoke(ctx).status, verify.SKIP)


if __name__ == "__main__":
    unittest.main()
