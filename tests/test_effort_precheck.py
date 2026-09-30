"""/effort 设了一档当前模型登记过不认的深度：当场提醒，不等第一次请求的 400。"""

from __future__ import annotations

import contextlib
import io
import types
import unittest

from xiaoyu import cli
from xiaoyu.providers import Provider, Registry


def fake_agent(provider: str, model: str, effort: str = "") -> types.SimpleNamespace:
    registry = Registry(
        [Provider(provider, "https://x/v1", "sk-test", (model,))],
        clients={provider: object()},
    )
    config = types.SimpleNamespace(model=model, effort=effort)
    return types.SimpleNamespace(registry=registry, config=config)


class EffortMismatchTest(unittest.TestCase):
    def test_level_outside_the_registered_set_is_called_out(self) -> None:
        #  xai 的 grok-4.7 登记的是 low / medium / high / xhigh
        warning = cli.effort_mismatch(fake_agent("xai", "grok-4.7"), "max")
        self.assertIn("xai/grok-4.7", warning)
        self.assertIn("xhigh", warning)
        self.assertIn("max", warning)

    def test_registered_level_is_silent(self) -> None:
        self.assertEqual(cli.effort_mismatch(fake_agent("xai", "grok-4.7"), "high"), "")

    def test_unregistered_model_and_cleared_level_are_silent(self) -> None:
        self.assertEqual(cli.effort_mismatch(fake_agent("xai", "some-other-model"), "max"), "")
        self.assertEqual(cli.effort_mismatch(fake_agent("xai", "grok-4.7"), ""), "")

    def test_unresolvable_model_is_silent(self) -> None:
        agent = fake_agent("xai", "grok-4.7")
        agent.config.model = "nobody-serves-this"
        self.assertEqual(cli.effort_mismatch(agent, "max"), "")

    def test_slash_command_sets_the_level_and_prints_the_warning(self) -> None:
        agent = fake_agent("xai", "grok-4.7")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.handle_slash(agent, "/effort max")
        self.assertEqual(agent.config.effort, "max")  # 只提醒，不拦
        self.assertIn("多半会被上游拒绝", buffer.getvalue())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli.handle_slash(agent, "/effort high")
        self.assertNotIn("多半会被上游拒绝", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
