"""模型清单、成本计算、横向对比聚合的测试。不打网络。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.evals import models
from xiaoyu.evals.runner import summarize_by_model

#  断言一律打在随包分发的示例数据上——真实候选清单是本机私有的（models.local.json，
#  不入库），测试不能依赖它，否则换台机器就红。
EXAMPLE_CANDIDATES, EXAMPLE_PRICES = models.load(models.EXAMPLE_PATH)


def write_data(directory: Path, *entries: dict) -> Path:
    path = directory / "models.json"
    path.write_text(json.dumps({"models": list(entries)}), encoding="utf-8")
    return path


class TestPrices(unittest.TestCase):
    def test_example_data_is_usable(self) -> None:
        self.assertTrue(EXAMPLE_CANDIDATES, "示例数据是没配过时的兜底，不能是空的")
        for candidate in EXAMPLE_CANDIDATES:
            price = EXAMPLE_PRICES.get(candidate.name)
            self.assertIsNotNone(price, f"{candidate.name} 缺单价，成本列会是空的")
            self.assertGreater(price["in"], 0, f"{candidate.name} 输入单价必须为正")
            self.assertGreater(price["out"], 0, f"{candidate.name} 输出单价必须为正")

    def test_cost_math(self) -> None:
        #  example-medium: in 3e-6, out 15e-6
        value = models.cost("example-medium", 1_000_000, 100_000, EXAMPLE_PRICES)
        self.assertIsNotNone(value)
        self.assertAlmostEqual(value, 3.0 + 1.5, places=6)

    def test_qualified_name_falls_back_to_bare_name(self) -> None:
        """Usage 的 key 是 `provider/model`，单价表按裸名建，得查得到。"""
        value = models.cost("gateway/example-medium", 1_000_000, 0, EXAMPLE_PRICES)
        self.assertAlmostEqual(value, 3.0, places=6)

    def test_unknown_model_has_no_cost(self) -> None:
        self.assertIsNone(models.cost("不存在的模型", 1000, 1000, EXAMPLE_PRICES))
        self.assertEqual(models.format_cost(None), "—")

    def test_relative_price_flags_the_expensive_ones(self) -> None:
        cheap = models.relative_price("example-small", EXAMPLE_PRICES)
        pricey = models.relative_price("example-large", EXAMPLE_PRICES)
        self.assertAlmostEqual(cheap, 1.0, places=6)
        #  最贵的输入单价应该比最便宜的高一个数量级以上，否则路由没意义
        self.assertGreater(pricey, 10)

    def test_no_duplicate_candidates(self) -> None:
        names = [candidate.name for candidate in EXAMPLE_CANDIDATES]
        self.assertEqual(len(names), len(set(names)))

    def test_broken_file_degrades_to_empty_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text("{ 这不是 JSON", encoding="utf-8")
            self.assertEqual(models.load(path), ([], {}))
            self.assertEqual(models.load(Path(tmp) / "根本不存在.json"), ([], {}))


class TestDataPath(unittest.TestCase):
    """找数据文件的顺序：$XIAOYU_EVAL_MODELS → models.local.json → models.example.json。"""

    def test_env_var_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = write_data(Path(tmp), {"name": "from-env", "in": 1e-6, "out": 2e-6})
            with mock.patch.dict(os.environ, {models.ENV_VAR: str(path)}):
                candidates, prices = models.load()
            self.assertEqual([c.name for c in candidates], ["from-env"])
            self.assertIn("from-env", prices)

    def test_local_beats_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local = write_data(Path(tmp), {"name": "from-local", "in": 1e-6, "out": 2e-6})
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(models.ENV_VAR, None)
                with mock.patch.object(models, "LOCAL_PATH", local):
                    self.assertEqual(models.data_path(), local)

    def test_example_is_the_last_resort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "不存在.json"
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(models.ENV_VAR, None)
                with mock.patch.object(models, "LOCAL_PATH", missing):
                    self.assertEqual(models.data_path(), models.EXAMPLE_PATH)


def make_result(model: str, case: str, passed: bool, **overrides) -> dict:
    result = {
        "case": case,
        "model": model,
        "description": "",
        "passed": passed,
        "checks": [{"check": "c1", "passed": passed, "detail": ""}],
        "error": None,
        "tool_calls": ["read_file"],
        "tool_errors": 0,
        "model_calls": 1,
        "prompt_tokens": 1000,
        "completion_tokens": 100,
        "usage_reported": True,
        "usage_by_model": {},
        "cost": 0.01,
        "seconds": 5.0,
        "artifacts": None,
    }
    result.update(overrides)
    return result


class TestSummarize(unittest.TestCase):
    def test_aggregates_per_model(self) -> None:
        results = [
            make_result("A", "case1", True),
            make_result("A", "case2", False),
            make_result("B", "case1", True),
            make_result("B", "case2", True),
        ]
        rows = summarize_by_model(results)
        self.assertEqual([row["model"] for row in rows], ["B", "A"], "通过多的排前面")
        self.assertEqual(rows[0]["passed"], 2)
        self.assertEqual(rows[1]["passed"], 1)
        self.assertEqual(rows[0]["prompt_tokens"], 2000)

    def test_cheaper_wins_when_pass_rate_ties(self) -> None:
        results = [
            make_result("expensive", "case1", True, cost=1.0),
            make_result("cheap", "case1", True, cost=0.01),
        ]
        rows = summarize_by_model(results)
        self.assertEqual(rows[0]["model"], "cheap", "同样能干活时便宜的排前面")

    def test_missing_cost_sorts_last_but_does_not_crash(self) -> None:
        results = [
            make_result("no-price", "case1", True, cost=None),
            make_result("priced", "case1", True, cost=0.5),
        ]
        rows = summarize_by_model(results)
        self.assertEqual(rows[0]["model"], "priced")
        self.assertFalse(rows[1]["has_cost"])
        self.assertEqual(models.format_cost(None), "—")

    def test_collects_errors_for_broken_models(self) -> None:
        """有些模型可能完全不支持 tool calling —— 报告里必须看得见原因。"""
        results = [
            make_result(
                "broken", "case1", False, error="BadRequestError: tools not supported"
            )
        ]
        rows = summarize_by_model(results)
        self.assertEqual(rows[0]["passed"], 0)
        self.assertIn("tools not supported", rows[0]["errors"][0])


if __name__ == "__main__":
    unittest.main()
