"""effort（推理深度）旋钮：一个名字出内核，三条协议各自翻译；子 agent 可单独声明。"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from xiaoyu import messages as msgs
from xiaoyu import responses
from xiaoyu.config import EFFORT_LEVELS, Config
from xiaoyu.serve_state import StateError, check_agent_config

from xiaoyu import agents as agents_mod
from xiaoyu.agents import load_agent_specs

from .test_agent_paths import AgentTestCase, chunk, usage_chunk
from .test_agents_spec import GOOD_SPEC, write_spec


class ProtocolTranslationTest(unittest.TestCase):
    USER = [{"role": "user", "content": "hi"}]

    def test_messages_puts_effort_under_output_config(self):
        """Anthropic：output_config.effort（GA，无 beta 头），不留平铺的 reasoning_effort。"""
        request = msgs.to_request("m", self.USER, None, True, {"reasoning_effort": "xhigh"})
        self.assertEqual(request["output_config"], {"effort": "xhigh"})
        self.assertNotIn("reasoning_effort", request)

    def test_messages_merges_with_existing_output_config(self):
        """structured outputs 的 format 也住 output_config：合并而非覆盖。"""
        request = msgs.to_request(
            "m", self.USER, None, True,
            {"reasoning_effort": "low", "output_config": {"format": {"type": "json_schema"}}},
        )
        self.assertEqual(
            request["output_config"], {"format": {"type": "json_schema"}, "effort": "low"}
        )

    def test_messages_without_effort_has_no_output_config(self):
        request = msgs.to_request("m", self.USER, None, True, {})
        self.assertNotIn("output_config", request)

    def test_responses_nests_effort_under_reasoning(self):
        request = responses.to_request("m", self.USER, None, {"reasoning_effort": "high"})
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertNotIn("reasoning_effort", request)

    def test_responses_empty_effort_is_dropped(self):
        request = responses.to_request("m", self.USER, None, {"reasoning_effort": ""})
        self.assertNotIn("reasoning", request)
        self.assertNotIn("reasoning_effort", request)


class ConfigTest(unittest.TestCase):
    def test_env_and_override(self):
        with mock.patch.dict(os.environ, {"XIAOYU_EFFORT": "Medium", "XIAOYU_BASE_URL": "http://x/v1"}):
            self.assertEqual(Config.from_env().effort, "medium")
            self.assertEqual(Config.from_env(effort="max").effort, "max")
            #  None 覆盖 = 没给，沿用 env
            self.assertEqual(Config.from_env(effort=None).effort, "medium")

    def test_default_empty(self):
        with mock.patch.dict(os.environ, {"XIAOYU_BASE_URL": "http://x/v1"}, clear=False):
            os.environ.pop("XIAOYU_EFFORT", None)
            self.assertEqual(Config.from_env().effort, "")


class AgentRequestTest(AgentTestCase):
    def _send(self) -> dict:
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        agent.send("hi")
        return self.client.chat.completions.calls[-1]

    def test_request_carries_effort_when_set(self):
        self.config.effort = "low"
        self.assertEqual(self._send()["reasoning_effort"], "low")

    def test_request_omits_effort_by_default(self):
        """不设就一个字段都不多：兼容端点对未知参数可能 400。"""
        self.assertNotIn("reasoning_effort", self._send())


class SubagentSpecTest(AgentTestCase):
    def _load(self):
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            return load_agent_specs(self.root)

    def test_spec_effort_parsed(self):
        write_spec(self.root / ".xiaoyu" / "agents", "scout", GOOD_SPEC + 'effort = "LOW"\n')
        specs, problems = self._load()
        self.assertEqual(problems, [])
        self.assertEqual(specs[0].effort, "low")

    def test_spec_bad_effort_rejected(self):
        write_spec(self.root / ".xiaoyu" / "agents", "scout", GOOD_SPEC + 'effort = "turbo"\n')
        specs, problems = self._load()
        self.assertEqual(specs, [])
        self.assertTrue(any("effort" in p for p in problems))


class ServeAgentConfigTest(unittest.TestCase):
    def test_effort_accepted_and_normalised(self):
        self.assertEqual(check_agent_config({"effort": "High"})["effort"], "high")
        self.assertEqual(check_agent_config({})["effort"], "")

    def test_bad_effort_rejected(self):
        with self.assertRaises(StateError):
            check_agent_config({"effort": "ultra"})

    def test_levels_are_union_of_three_lines(self):
        for level in ("low", "medium", "high", "xhigh", "max", "none", "minimal"):
            self.assertIn(level, EFFORT_LEVELS)


if __name__ == "__main__":
    unittest.main()
