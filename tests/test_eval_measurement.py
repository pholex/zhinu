"""eval 测量纪律的自证测试——不打网络，纯本地。

规则沿用 test_eval_assertions 的双向可证：
  · 该拦的要拦（基础设施故障不进通过率、噪声差异要被判语点名）
  · 该放的要放（真实差异不被误标成噪声、正常失败照常计 FAIL）
"""

from __future__ import annotations

import unittest

from xiaoyu.evals.runner import (
    INFRA_KINDS,
    _machine_resources,
    discrimination_note,
    regression_failures,
    summarize_by_model,
)


def _run(model: str, case: str, passed: bool, *, measurable: bool = True, infra=None,
         regression: bool = False) -> dict:
    """最小合法的 run_case 结果骨架。"""
    return {
        "case": case,
        "model": model,
        "regression": regression,
        "description": "",
        "passed": passed,
        "measurable": measurable,
        "infra": infra,
        "checks": [{"check": "c", "passed": passed, "detail": ""}],
        "error": None if measurable else "APIConnectionError: boom",
        "tool_calls": [],
        "tool_errors": 0,
        "model_calls": 1,
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "usage_reported": True,
        "usage_by_model": {},
        "cost": 0.001,
        "seconds": 1.0,
        "artifacts": None,
    }


class TestSummarize(unittest.TestCase):
    def test_infra_runs_excluded_from_pass_rate(self) -> None:
        results = [
            _run("m1", "a", True),
            _run("m1", "b", False, measurable=False, infra="transient"),
        ]
        (row,) = summarize_by_model(results)
        self.assertEqual((row["runs"], row["passed"], row["infra"]), (1, 1, 1))
        #  未测的那条要在错误列表里带 [未测] 标记，可诊断而非消失
        self.assertTrue(any("[未测]" in e for e in row["errors"]))

    def test_real_failure_still_counts(self) -> None:
        #  反向：能力失败（measurable=True）绝不能被洗成"未测"
        results = [_run("m1", "a", False)]
        (row,) = summarize_by_model(results)
        self.assertEqual((row["runs"], row["passed"], row["infra"]), (1, 0, 0))

    def test_flaky_counts_mixed_outcomes_only(self) -> None:
        results = [
            #  case a：时过时不过 → 抖动
            _run("m1", "a", True),
            _run("m1", "a", False),
            #  case b：稳定通过 → 不算
            _run("m1", "b", True),
            _run("m1", "b", True),
            #  case c：稳定失败 → 不算（那是真不会，不是抖）
            _run("m1", "c", False),
            _run("m1", "c", False),
        ]
        (row,) = summarize_by_model(results)
        self.assertEqual(row["flaky"], 1)


class TestDiscriminationNote(unittest.TestCase):
    def test_no_discrimination_when_all_equal(self) -> None:
        rows = summarize_by_model(
            [_run("m1", "a", True), _run("m2", "a", True)]
        )
        note = discrimination_note(rows, repeat=1)
        self.assertIsNotNone(note)
        self.assertIn("无区分度", note)

    def test_single_run_difference_is_unconfirmed(self) -> None:
        rows = summarize_by_model(
            [_run("m1", "a", True), _run("m2", "a", False)]
        )
        note = discrimination_note(rows, repeat=1)
        self.assertIsNotNone(note)
        self.assertIn("未证实", note)

    def test_gap_within_flaky_band_is_flagged(self) -> None:
        #  m1 比 m2 多过 1 个，但 m1 自己在 case b 上抖（1 过 1 不过）：
        #  差 1 ≤ 抖动带 1 → 不构成区分
        results = [
            _run("m1", "a", True),
            _run("m1", "a", True),
            _run("m1", "b", True),
            _run("m1", "b", False),
            _run("m2", "a", True),
            _run("m2", "a", True),
            _run("m2", "b", False),
            _run("m2", "b", False),
        ]
        rows = summarize_by_model(results)
        note = discrimination_note(rows, repeat=2)
        self.assertIsNotNone(note)
        self.assertIn("抖动带", note)

    def test_real_gap_beyond_band_stays_silent(self) -> None:
        #  反向：全部稳定、差距真实 → 不该有任何提醒（把真结论误标成噪声
        #  和把噪声当结论一样糟）
        results = [
            _run("m1", "a", True),
            _run("m1", "a", True),
            _run("m1", "b", True),
            _run("m1", "b", True),
            _run("m2", "a", False),
            _run("m2", "a", False),
            _run("m2", "b", False),
            _run("m2", "b", False),
        ]
        rows = summarize_by_model(results)
        self.assertIsNone(discrimination_note(rows, repeat=2))

    def test_single_model_never_notes(self) -> None:
        rows = summarize_by_model([_run("m1", "a", True)])
        self.assertIsNone(discrimination_note(rows, repeat=1))


class TestInfraKindsContract(unittest.TestCase):
    def test_infra_kinds_are_valid_classifier_kinds(self) -> None:
        #  INFRA_KINDS 必须是 errors.ALL_KINDS 的子集——分类器改名/删类时
        #  这里要跟着响，不能静默漏判
        from xiaoyu.errors import ALL_KINDS

        self.assertTrue(INFRA_KINDS <= set(ALL_KINDS), INFRA_KINDS - set(ALL_KINDS))
        #  能力相关的两类绝不能被划进基础设施：窗口用爆和跑挂是真实结局
        self.assertNotIn("context_overflow", INFRA_KINDS)
        self.assertNotIn("fatal", INFRA_KINDS)



class TestPassHatK(unittest.TestCase):
    def test_pass_hat_k_needs_all_runs_pass(self) -> None:
        results = [
            _run("m", "stable", True), _run("m", "stable", True),   # 两次全过 → 计入 pass^k
            _run("m", "flaky", True), _run("m", "flaky", False),    # 时过时不过 → 不计
            _run("m", "dead", False), _run("m", "dead", False),     # 全不过 → 不计
        ]
        (row,) = summarize_by_model(results)
        self.assertEqual(row["pass_hat_k"], 1)

    def test_no_measurable_run_is_not_pass_hat_k(self) -> None:
        results = [_run("m", "x", False, measurable=False, infra="rate_limit")]
        (row,) = summarize_by_model(results)
        self.assertEqual(row["pass_hat_k"], 0)


class TestRegressionGate(unittest.TestCase):
    def test_regression_case_must_pass_all_runs(self) -> None:
        results = [
            _run("m", "reg", True, regression=True),
            _run("m", "reg", False, regression=True),   # 一次没过 → 门失败
        ]
        (row,) = summarize_by_model(results)
        self.assertEqual((row["regression_total"], row["regression_failed"]), (1, 1))
        self.assertTrue(regression_failures([row]))

    def test_regression_all_pass_no_gate_failure(self) -> None:
        results = [_run("m", "reg", True, regression=True), _run("m", "reg", True, regression=True)]
        (row,) = summarize_by_model(results)
        self.assertEqual(row["regression_failed"], 0)
        self.assertEqual(regression_failures([row]), [])

    def test_capability_case_failing_is_not_a_gate_failure(self) -> None:
        #  能力 case（regression=False）失败不进门——起步低分正常
        results = [_run("m", "cap", False, regression=False)]
        (row,) = summarize_by_model(results)
        self.assertEqual(row["regression_total"], 0)
        self.assertEqual(regression_failures([row]), [])


class TestResources(unittest.TestCase):
    def test_resource_profile_shape(self) -> None:
        res = _machine_resources()
        self.assertIn("cpu_count", res)
        self.assertIn("memory_gb", res)
        self.assertIn("machine", res)
        #  cpu_count 恒为正整数或 None，绝不臆造
        self.assertTrue(res["cpu_count"] is None or res["cpu_count"] > 0)



class TestStateBasedGrading(unittest.TestCase):
    """看磁盘/git/测试/工具轨迹判分，别信 agent 自述——每个 case 至少要有一个
    非自述型判据。自述型（transcript_contains）只能配对出现，不能独撑一个 case。"""

    def test_every_case_has_a_state_based_check(self) -> None:
        from xiaoyu.evals.cases import CASES

        for case in CASES:
            has_state = any(
                not getattr(check, "self_report", False) for _label, check in case.checks
            )
            self.assertTrue(has_state, f"{case.name} 只有自述型判据，缺状态型证据")

    def test_transcript_contains_is_tagged_self_report(self) -> None:
        from xiaoyu.evals.harness import nothing_written, transcript_contains

        self.assertTrue(getattr(transcript_contains("x"), "self_report", False))
        #  状态型判据不带这个标（默认即状态型）
        self.assertFalse(getattr(nothing_written(), "self_report", False))


if __name__ == "__main__":
    unittest.main()
