"""Models API 能力字段：按需探测 max_input_tokens/image_input，自校正上下文上限、
不覆盖用户显式设定，硬编码表退为零往返兜底。"""

from __future__ import annotations

import types
import unittest
from unittest import mock

from xiaoyu.config import Config
from xiaoyu.providers import Provider, Registry


GATEWAY = "网关"


def _caps_client(caps_by_id: dict[str, dict]):
    """带 models.retrieve 的假 client：返回带 capabilities/max_input_tokens 的对象。"""

    def retrieve(mid: str):
        c = caps_by_id.get(mid, {})
        return types.SimpleNamespace(
            id=mid,
            model_dump=lambda: {"id": mid, **c},
        )

    client = types.SimpleNamespace()
    client.with_options = lambda **kw: client
    client.models = types.SimpleNamespace(retrieve=retrieve)
    return client


class ExtractTest(unittest.TestCase):
    def test_extract_limit_and_image(self):
        obj = types.SimpleNamespace(model_dump=lambda: {
            "max_input_tokens": 1000000,
            "capabilities": {"image_input": {"supported": True}},
        })
        caps = Registry._extract_caps(obj)
        self.assertEqual(caps, {"max_input_tokens": 1000000, "image_input": True})

    def test_missing_fields_stay_none(self):
        obj = types.SimpleNamespace(model_dump=lambda: {"id": "x"})
        caps = Registry._extract_caps(obj)
        self.assertEqual(caps, {"max_input_tokens": None, "image_input": None})


class ProbeTest(unittest.TestCase):
    def _registry(self, caps):
        return Registry(
            [Provider(GATEWAY, "u", "k", (), GATEWAY)],
            clients={GATEWAY: _caps_client(caps)},
        )

    def test_probe_caches(self):
        reg = self._registry({"m": {"max_input_tokens": 500000}})
        self.assertIsNone(reg.cached_caps("m"))
        got = reg.probe_model_caps("m")
        self.assertEqual(got["max_input_tokens"], 500000)
        self.assertEqual(reg.cached_caps("m")["max_input_tokens"], 500000)

    def test_probe_failure_is_silent(self):
        client = _caps_client({})
        client.models.retrieve = mock.Mock(side_effect=RuntimeError("boom"))
        reg = Registry([Provider(GATEWAY, "u", "k", (), GATEWAY)], clients={GATEWAY: client})
        self.assertEqual(reg.probe_model_caps("m"), {})


class ApplyProbedLimitTest(unittest.TestCase):
    def _cfg(self, **kw):
        return Config(base_url="x", model="m", workspace=None, **kw)  # type: ignore[arg-type]

    def test_applies_when_no_override(self):
        cfg = self._cfg()
        self.assertTrue(cfg.apply_probed_limit(500000))
        self.assertEqual(cfg.context_limit_override, 500000)
        self.assertTrue(cfg.context_limit_probed)

    def test_never_overrides_user_setting(self):
        cfg = self._cfg(context_limit_override=200000)  # 用户显式设
        self.assertFalse(cfg.apply_probed_limit(500000))
        self.assertEqual(cfg.context_limit_override, 200000)

    def test_later_probe_updates_earlier_probe(self):
        cfg = self._cfg()
        cfg.apply_probed_limit(400000)
        self.assertTrue(cfg.apply_probed_limit(500000))  # 探测写的可被探测更新
        self.assertEqual(cfg.context_limit_override, 500000)

    def test_same_value_no_change_but_marks_probed(self):
        cfg = self._cfg()
        cfg.apply_probed_limit(500000)
        self.assertFalse(cfg.apply_probed_limit(500000))
        self.assertTrue(cfg.context_limit_probed)

    def test_zero_or_none_ignored(self):
        cfg = self._cfg()
        self.assertFalse(cfg.apply_probed_limit(None))
        self.assertFalse(cfg.apply_probed_limit(0))
        self.assertIsNone(cfg.context_limit_override)


class RefreshCapabilitiesTest(unittest.TestCase):
    def _agent(self, caps):
        from xiaoyu.agent import Agent
        from xiaoyu.tools import Toolbox
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        cfg = Config(base_url="x", model="claude-opus-5",
                     workspace=Path(self._tmp.name).resolve(),
                     enable_skills=False, enable_agents=False, enable_hooks=False,
                     enable_plugins=False, enable_mcp=False)
        reg = Registry.for_client(_caps_client(caps), name="anthropic")
        return Agent(cfg, Toolbox(cfg), registry=reg, quiet=True), cfg

    def test_self_corrects_and_reports(self):
        #  硬编码表把 claude-opus-5 记成 1_000_000；这里探到不同值 → 自校正 + 漂移告警
        agent, cfg = self._agent({"claude-opus-5": {"max_input_tokens": 777777,
                                                     "capabilities": {"image_input": {"supported": True}}}})
        notes = agent.refresh_capabilities()
        joined = " ".join(notes)
        self.assertIn("777777", joined)
        self.assertIn("自校正", joined)
        self.assertIn("收图", joined)
        self.assertEqual(cfg.context_limit, 777777)
        self._tmp.cleanup()

    def test_matches_table_no_correction_note(self):
        agent, cfg = self._agent({"claude-opus-5": {"max_input_tokens": 1000000}})
        notes = agent.refresh_capabilities()
        self.assertFalse(any("自校正" in n for n in notes))  # 与表一致，不用校正
        self._tmp.cleanup()

    def test_no_caps_returns_nothing(self):
        agent, cfg = self._agent({})  # retrieve 返回空 dump → 无 max_input_tokens
        self.assertEqual(agent.refresh_capabilities(), [])
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
