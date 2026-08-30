"""Approver 契约的归一化与 Allow/Deny 全功能形态（嵌入 SDK 化第二批）。

不打网络，假 client 注入。锁住四件事：

1. 简写形态语义不变：True / (True, 附言) / 非空 str / False
2. (False, "理由") 的理由不再被静默丢弃——它就是拒绝理由，回灌模型
3. Deny(reason) 与非空 str 等价；Allow() 与 True 等价、Allow(note=…) 与 (True, 附言) 等价
4. Allow(updated_args=…)：批准并改写参数——实际执行、trace、tool.running
   事件用的都是改写后的参数（宿主"包沙箱再放行"的通道）
"""

from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path

from xiaoyu.agent import Agent, Allow, Deny, normalize_verdict
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox
from xiaoyu.config import Config

from tests.test_agent_paths import FakeClient, chunk
from tests.test_embedding_smoke import _chunk_with_tool_call


class TestNormalizeVerdict(unittest.TestCase):
    def test_shorthand_forms_keep_semantics(self) -> None:
        self.assertEqual(normalize_verdict(True), (True, "", "", None))
        self.assertEqual(normalize_verdict(False), (False, "", "", None))
        self.assertEqual(normalize_verdict(""), (False, "", "", None))
        self.assertEqual(normalize_verdict("别动这个文件"), (False, "", "别动这个文件", None))
        self.assertEqual(normalize_verdict((True, "顺便跑下测试")), (True, "顺便跑下测试", "", None))

    def test_false_with_reason_is_no_longer_dropped(self) -> None:
        self.assertEqual(
            normalize_verdict((False, "飞书渠道不放行 bash")),
            (False, "", "飞书渠道不放行 bash", None),
        )

    def test_string_flag_in_tuple_follows_bare_string_semantics(self) -> None:
        self.assertEqual(normalize_verdict(("不行", "")), (False, "", "不行", None))

    def test_allow_deny_objects(self) -> None:
        self.assertEqual(normalize_verdict(Allow()), (True, "", "", None))
        self.assertEqual(normalize_verdict(Allow(note="加 -n 试跑")), (True, "加 -n 试跑", "", None))
        self.assertEqual(normalize_verdict(Deny()), (False, "", "", None))
        self.assertEqual(normalize_verdict(Deny(reason="超出授权")), (False, "", "超出授权", None))

    def test_allow_updated_args_is_copied(self) -> None:
        original = {"command": "echo hi"}
        approved, _, _, updated = normalize_verdict(Allow(updated_args=original))
        self.assertTrue(approved)
        self.assertEqual(updated, original)
        self.assertIsNot(updated, original)  # 防宿主复用字典时被后续 pop 污染


class VerdictAgentTestCase(unittest.TestCase):
    """走完整 _execute 路径：bash 调用一次，approver 说了算。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=False,
            #  钉确认档：这里测的是审批管线本身，不能跟着出厂起始档（auto）漂
            mode="default",
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build_with_bash_call(self, approver, command: str = "echo ORIGINAL") -> Agent:
        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments=f'{{"command": "{command}"}}'),
        )
        script = [[_chunk_with_tool_call(tool_call)], [chunk(content="done")]]
        return Agent(
            self.config,
            Toolbox(self.config),
            registry=Registry.for_client(FakeClient(script)),
            approver=approver,
        )

    def tool_messages(self, agent: Agent) -> list[str]:
        return [m["content"] for m in agent.messages if m.get("role") == "tool"]

    def test_false_with_reason_reaches_model(self) -> None:
        agent = self.build_with_bash_call(lambda name, args: (False, "宿主策略：先走审批卡"))
        agent.send("跑一下")
        self.assertIn("宿主策略：先走审批卡", self.tool_messages(agent)[-1])

    def test_deny_reason_reaches_model(self) -> None:
        agent = self.build_with_bash_call(lambda name, args: Deny(reason="超出本渠道授权"))
        agent.send("跑一下")
        self.assertIn("超出本渠道授权", self.tool_messages(agent)[-1])

    def test_allow_note_is_appended_to_output(self) -> None:
        agent = self.build_with_bash_call(lambda name, args: Allow(note="下次改用 explore"))
        agent.send("跑一下")
        output = self.tool_messages(agent)[-1]
        self.assertIn("ORIGINAL", output)  # 真的执行了
        self.assertIn("下次改用 explore", output)

    def test_allow_updated_args_rewrites_execution(self) -> None:
        """批准并改写：执行的是宿主给的参数，模型请求的原参数不再出现。"""
        agent = self.build_with_bash_call(
            lambda name, args: Allow(updated_args={"command": "echo REWRITTEN"})
        )
        agent.send("跑一下")
        output = self.tool_messages(agent)[-1]
        self.assertIn("REWRITTEN", output)
        self.assertNotIn("ORIGINAL", output)
        #  trace 记的也是改写后的参数——审计看到的必须是实际执行的东西
        bash_traces = [t for t in agent.trace if t["tool"] == "bash"]
        self.assertEqual(bash_traces[-1]["args"], {"command": "echo REWRITTEN"})


if __name__ == "__main__":
    unittest.main()
