"""委托出去的 agent 沿用主会话的备用模型链。

子 agent 的配置是逐个字段挑着传的，漏传的字段取默认值——备用链漏了就是空，
于是主会话撞上持续限流会换模型继续，它派出去的子 agent 却直接报失败。
批量扇出最容易撞限流，恰恰是这条路径没有保护。
"""

from __future__ import annotations

import ast
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.agents import AgentSpec, RunStore, execute_delegation

from .test_agent_paths import AgentTestCase, chunk, usage_chunk
from .test_errors import rate_limit_error

PACKAGE = Path(__file__).resolve().parents[1] / "xiaoyu"

SPEC = AgentSpec(
    name="worker", description="干活", system_prompt="在 {workspace} 干活",
    tools=("read_file", "grep", "list_files"),
)


class DerivedConfigSentinel(unittest.TestCase):
    def test_every_config_derived_from_another_passes_the_chain(self) -> None:
        """从另一份配置派生出来的 Config（认 `base_url=<某对象>.base_url`）必须带上
        fallback_models。新加一处委托、又照着旧写法挑字段传时，这里当场变红。"""
        derived = 0
        missing: list[str] = []
        for path in sorted(PACKAGE.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "Config":
                    continue
                keywords = {item.arg: item.value for item in node.keywords if item.arg}
                base = keywords.get("base_url")
                if not (isinstance(base, ast.Attribute) and base.attr == "base_url"):
                    continue
                derived += 1
                if "fallback_models" not in keywords:
                    missing.append(f"{path.name}:{node.lineno}")
        #  一处都没认出来 = 判据失效（写法变了），不能当成全部通过
        self.assertGreaterEqual(derived, 3, "没认出派生配置的构造点——检查判据")
        self.assertEqual(missing, [], "派生的 Config 没传 fallback_models")


class DelegationFallbackTest(AgentTestCase):
    def delegate(self, script: list, **extra):
        agent = self.build(script)
        handle: list = []
        with mock.patch("xiaoyu.agent.Agent._sleep"), contextlib.redirect_stdout(io.StringIO()):
            result = execute_delegation(
                SPEC, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, RunStore(),
                task="做点事", on_agent=handle.append, **extra,
            )
        return result, handle[0]

    def test_chain_reaches_the_sub_agent_as_a_copy(self) -> None:
        self.config.fallback_models = ["backup-a", "backup-b"]
        _, sub = self.delegate([[chunk(content="做完了")]])
        self.assertEqual(sub.config.fallback_models, ["backup-a", "backup-b"])
        #  各拿各的列表：子 agent 那边动了它，不该改到主会话头上
        self.assertIsNot(sub.config.fallback_models, self.config.fallback_models)

    def test_chain_follows_the_main_session_even_when_model_is_named(self) -> None:
        self.config.fallback_models = ["backup-a"]
        _, sub = self.delegate([[chunk(content="做完了")]], model_override="special-model")
        self.assertEqual(
            [route.model for route in sub.model_chain()], ["special-model", "backup-a"]
        )

    def test_no_chain_configured_means_no_chain(self) -> None:
        _, sub = self.delegate([[chunk(content="做完了")]])
        self.assertEqual(sub.config.fallback_models, [])

    def test_sustained_rate_limit_is_survived_and_reported(self) -> None:
        self.config.fallback_models = ["backup-model"]
        result, _ = self.delegate([
            rate_limit_error(),
            rate_limit_error(),
            rate_limit_error(),
            [chunk(content="备用模型做完了"), usage_chunk(100, 10)],
        ])
        self.assertEqual(result.failure, "")
        self.assertEqual(result.answer, "备用模型做完了")
        models = [call["model"] for call in self.client.completions.calls]
        self.assertEqual(models, ["main-model"] * 3 + ["backup-model"])
        #  结论里报的是实际干活的模型，并点明发生过降级
        self.assertEqual(result.model, "backup-model")
        self.assertTrue(
            any("main-model" in note and "backup-model" in note for note in result.notes),
            result.notes,
        )

    def test_archive_keeps_the_named_model_for_resume(self) -> None:
        self.config.fallback_models = ["backup-model"]
        store = RunStore()
        agent = self.build([
            rate_limit_error(),
            rate_limit_error(),
            rate_limit_error(),
            [chunk(content="备用模型做完了"), usage_chunk(100, 10)],
        ])
        with mock.patch("xiaoyu.agent.Agent._sleep"), contextlib.redirect_stdout(io.StringIO()):
            execute_delegation(
                SPEC, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, store, task="做点事",
            )
        (run,) = store.values()
        #  续跑时先回去试点名的那个，与主会话回探首选模型同一个道理
        self.assertEqual(run.model, "main-model")

    def test_without_a_chain_the_failure_still_surfaces(self) -> None:
        result, _ = self.delegate([rate_limit_error()] * 3)
        self.assertIn("RateLimitError", result.failure)
        self.assertEqual(result.model, "main-model")
        self.assertEqual(result.notes, [])


if __name__ == "__main__":
    unittest.main()
