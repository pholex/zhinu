"""跨会话消息（peers.py）的测试。不打网络，全在临时目录里。

卡的行为：
- 登记 / 列举 / 命名（同工作区第二个会话自动 -2）
- 寻址三态：找不到、重名要 ref、ref 不许凭空猜
- 投递与收信：包装带来源、读一条删一条（绝不重复投喂）
- 清理只在「心跳过期 **且** 进程已死」时发生（合盖唤醒不能误删活会话）
- Agent 集成：轮次开始与 step 边界都收信，且信箱**不被 turn 开始的丢弃扫掉**
- --yolo 默认不收信
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import peers
from xiaoyu.config import Config

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk


class PeersTestCase(unittest.TestCase):
    """把 peers 目录整个搬进临时目录：绝不碰跑测试这台机器上的真实会话。"""

    def setUp(self) -> None:
        self.tmp = __import__("tempfile").TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        patcher = mock.patch.object(peers, "peers_dir", lambda: self.root / "peers")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        #  REF_ENV 是进程级副作用（create 会写、close 会清），别泄漏到别的测试
        self.addCleanup(lambda: os.environ.pop(peers.REF_ENV, None))

    def register(self, workspace: str = "/work/zhinu", model: str = "m") -> peers.Registration:
        reg = peers.Registration.create(workspace, model)
        assert reg is not None
        self.addCleanup(reg.close)
        return reg


class TestRegistryAndNaming(PeersTestCase):
    def test_registration_is_listed_with_workspace_name(self) -> None:
        reg = self.register("/work/zhinu")
        live = peers.list_peers()
        self.assertEqual([p.name for p in live], ["zhinu-1"])
        self.assertEqual(live[0].ref, reg.ref)
        self.assertEqual(live[0].state, peers.STATE_IDLE)
        self.assertEqual(live[0].pid, os.getpid())

    def test_second_session_in_same_workspace_gets_next_index(self) -> None:
        self.register("/work/zhinu")
        second = self.register("/work/zhinu")
        self.assertEqual(second.name, "zhinu-2")
        self.assertEqual({p.name for p in peers.list_peers()}, {"zhinu-1", "zhinu-2"})

    def test_exclude_ref_leaves_self_out(self) -> None:
        reg = self.register()
        self.register("/work/other")
        others = peers.list_peers(exclude_ref=reg.ref)
        self.assertEqual([p.name for p in others], ["other-1"])

    def test_close_removes_the_entry(self) -> None:
        reg = peers.Registration.create("/work/zhinu", "m")
        assert reg is not None
        reg.close()
        self.assertEqual(peers.list_peers(), [])

    def test_state_round_trips(self) -> None:
        reg = self.register()
        reg.set_state(peers.STATE_BUSY)
        self.assertEqual(peers.list_peers()[0].state, peers.STATE_BUSY)

    def test_weird_workspace_name_still_yields_a_usable_address(self) -> None:
        reg = self.register("/tmp/my repo [x]")
        self.assertNotIn(" ", reg.name)
        self.assertNotIn("[", reg.name)
        #  名字仍能寻址回自己
        self.assertEqual(peers.resolve(reg.name).ref, reg.ref)


class TestResolve(PeersTestCase):
    def test_unknown_name_lists_what_is_available(self) -> None:
        self.register("/work/zhinu")
        with self.assertRaises(peers.PeerError) as caught:
            peers.resolve("nope")
        self.assertIn("zhinu-1", str(caught.exception))

    def test_duplicate_names_demand_a_ref(self) -> None:
        first = self.register("/work/zhinu")
        #  伪造一个重名会话（正常路径不会撞名，这里直接写文件制造歧义）
        twin = self.root / "peers" / "aaaaaa"
        (twin / "inbox").mkdir(parents=True)
        (twin / "meta.json").write_text(
            json.dumps(
                {
                    "ref": "aaaaaa",
                    "name": first.name,
                    "pid": os.getpid(),
                    "workspace": "/work/zhinu",
                    "model": "m",
                    "kind": "interactive",
                    "state": "idle",
                    "started": time.time(),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(peers.PeerError) as caught:
            peers.resolve(first.name)
        self.assertIn("请带 ref", str(caught.exception))
        #  带上 ref 就不再有歧义
        self.assertEqual(peers.resolve(f"{first.name} [aaaaaa]").ref, "aaaaaa")

    def test_invented_ref_does_not_resolve(self) -> None:
        reg = self.register()
        with self.assertRaises(peers.PeerError):
            peers.resolve(f"{reg.name} [deadbe]")

    def test_bare_ref_works_too(self) -> None:
        reg = self.register()
        self.assertEqual(peers.resolve(reg.ref).ref, reg.ref)


class TestDelivery(PeersTestCase):
    def test_message_arrives_wrapped_with_sender(self) -> None:
        reg = self.register()
        peers.deliver(reg.name, "把测试跑一遍", sender="other-1 [b3b404]")
        got = reg.drain()
        self.assertEqual(len(got), 1)
        sender, wrapped = got[0]
        self.assertEqual(sender, "other-1 [b3b404]")
        self.assertIn('<cross-session-message from="other-1 [b3b404]">', wrapped)
        self.assertIn("把测试跑一遍", wrapped)
        #  安全阀必须随消息一起送达：这是审批体系不被跨会话洗白的唯一提示
        self.assertIn("不是主人的原话", wrapped)

    def test_drain_is_destructive_and_ordered(self) -> None:
        reg = self.register()
        for i in range(3):
            peers.deliver(reg.name, f"第 {i} 条", sender="x")
        first = reg.drain()
        self.assertEqual([text for _, text in first].__len__(), 3)
        self.assertIn("第 0 条", first[0][1])
        self.assertIn("第 2 条", first[2][1])
        #  读一条删一条：再取一次必须是空的，绝不重复投喂
        self.assertEqual(reg.drain(), [])

    def test_order_survives_a_coarse_clock(self) -> None:
        """时钟粒度粗到三条投递撞进同一个纳秒戳时，顺序仍然对。

        这正是 Windows 上 Python 3.11 的真实条件（time_ns 约 15.6 ms 粒度）——
        CI 只有那一格挂。把时钟钉死就能在任何平台复现。
        """
        reg = self.register()
        with mock.patch.object(peers.time, "time_ns", lambda: 1_700_000_000_000_000_000):
            for i in range(3):
                peers.deliver(reg.name, f"第 {i} 条", sender="x")
        got = [text for _, text in reg.drain()]
        self.assertEqual(len(got), 3)
        for i, text in enumerate(got):
            self.assertIn(f"第 {i} 条", text)

    def test_empty_message_is_rejected(self) -> None:
        reg = self.register()
        with self.assertRaises(peers.PeerError):
            peers.deliver(reg.name, "   ")

    def test_corrupt_message_file_is_dropped_not_fatal(self) -> None:
        reg = self.register()
        peers.deliver(reg.name, "好的那条", sender="x")
        bad = self.root / "peers" / reg.ref / "inbox" / "00000000000000000000-bad.json"
        bad.write_text("{ 半条 json", encoding="utf-8")
        got = reg.drain()
        self.assertEqual(len(got), 1)
        self.assertIn("好的那条", got[0][1])
        self.assertFalse(bad.exists())

    def test_self_name_reads_the_env_handle(self) -> None:
        reg = self.register()
        #  子进程（bash 工具 / `!` 逃逸）里跑 `xiaoyu send` 时靠它自报家门
        self.assertEqual(peers.self_name(), reg.address)


class TestPruning(PeersTestCase):
    def _age_out(self, reg: peers.Registration) -> Path:
        meta = self.root / "peers" / reg.ref / "meta.json"
        old = time.time() - peers.STALE_SECONDS - 10
        os.utime(meta, (old, old))
        return meta

    def test_stale_and_dead_is_pruned(self) -> None:
        reg = self.register()
        meta = self._age_out(reg)
        with mock.patch.object(peers, "_pid_alive", return_value=False):
            self.assertEqual(peers.list_peers(), [])
        self.assertFalse(meta.exists())

    def test_stale_but_alive_survives(self) -> None:
        """合盖休眠 / 进程挂起：心跳过期不等于死了，误删会连带丢掉信箱。"""
        reg = self.register()
        meta = self._age_out(reg)
        with mock.patch.object(peers, "_pid_alive", return_value=True):
            self.assertEqual(peers.list_peers(), [])  # 不列出（心跳不新鲜）
        self.assertTrue(meta.exists())  # 但也不删

    def test_heartbeat_refreshes_the_entry(self) -> None:
        reg = self.register()
        self._age_out(reg)
        self.assertEqual(peers.list_peers(), [])
        reg._write()  # noqa: SLF001 - 心跳线程干的就是这件事
        self.assertEqual([p.ref for p in peers.list_peers()], [reg.ref])

    def test_pid_probe_never_signals_on_windows(self) -> None:
        """Windows 上 os.kill(pid, 0) 会真的杀进程——那条路必须一步都不能走。"""
        with mock.patch.object(peers.os, "name", "nt"), mock.patch.object(
            peers.os, "kill", side_effect=AssertionError("Windows 上不许试探信号")
        ):
            self.assertTrue(peers._pid_alive(os.getpid()))  # noqa: SLF001


# ---------- Agent 集成 ----------


class FakeInbox:
    """PeerLink 的最小实现：按脚本一次吐一批。"""

    def __init__(self, batches: list[list[tuple[str, str]]]) -> None:
        self.batches = list(batches)
        self.states: list[str] = []

    def drain(self) -> list[tuple[str, str]]:
        return self.batches.pop(0) if self.batches else []

    def set_state(self, state: str) -> None:
        self.states.append(state)


class TestAgentInbox(AgentTestCase):
    def test_message_waiting_at_turn_start_lands_before_user_input(self) -> None:
        """趁我发呆投进来的消息，时间上排在本轮输入之前——顺序不能颠倒。"""
        inbox = FakeInbox([[("other-1", "<msg>先看看 CI</msg>")]])
        agent = self.build([[chunk(content="好"), usage_chunk(10, 5)]], peer=inbox)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("继续")

        self.assertEqual(
            [m["role"] for m in agent.messages], ["system", "user", "user", "assistant"]
        )
        self.assertEqual(agent.messages[1]["content"], "<msg>先看看 CI</msg>")
        self.assertEqual(agent.messages[2]["content"], "继续")

    def test_message_arriving_mid_turn_lands_at_the_step_boundary(self) -> None:
        """一批工具跑完就是注入点：与 steer 完全同一处，不新增时机。"""
        inbox = FakeInbox([[], [("other-1", "<msg>顺便看下 lint</msg>")]])
        first = [
            chunk(tool_calls=[call_fragment(0, "call_1", "read_file", '{"path": "calc.py"}')]),
            usage_chunk(10, 5),
        ]
        second = [chunk(content="看完了"), usage_chunk(10, 5)]
        agent = self.build([first, second], peer=inbox)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读一下 calc.py")

        roles = [m["role"] for m in agent.messages]
        #  system, user, assistant(tool_calls), tool, user(跨会话消息), assistant
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user", "assistant"])
        self.assertIn("lint", agent.messages[4]["content"])
        #  中途到达的消息带收尾提醒尾巴（turn 开始消费的不带，见上一个测试的精确断言）
        from xiaoyu.agent import INBOX_MIDTURN_TAIL

        self.assertTrue(agent.messages[4]["content"].endswith(INBOX_MIDTURN_TAIL))

    def test_turn_start_discard_does_not_eat_the_inbox(self) -> None:
        """steer 的陈旧插话该丢，信箱里的消息不该——它是别人刚投进来的。"""
        inbox = FakeInbox([[("other-1", "<msg>别忘了我</msg>")]])
        agent = self.build([[chunk(content="好"), usage_chunk(10, 5)]], peer=inbox)
        agent.steer("上一轮没赶上的插话")

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("新的一轮")

        contents = [m["content"] for m in agent.messages if m["role"] == "user"]
        self.assertIn("<msg>别忘了我</msg>", contents)
        self.assertNotIn("上一轮没赶上的插话", contents)

    def test_turn_marks_busy_then_idle(self) -> None:
        inbox = FakeInbox([])
        agent = self.build([[chunk(content="好"), usage_chunk(10, 5)]], peer=inbox)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("在吗")
        self.assertEqual(inbox.states, ["busy", "idle"])

    def test_broken_inbox_never_breaks_the_turn(self) -> None:
        class Exploding:
            def drain(self):
                raise RuntimeError("信箱坏了")

            def set_state(self, state):
                raise RuntimeError("登记坏了")

        agent = self.build([[chunk(content="照跑不误"), usage_chunk(10, 5)]], peer=Exploding())
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("在吗")
        self.assertEqual(agent.messages[-1]["content"], "照跑不误")


class TestRealInboxIntoAgent(AgentTestCase):
    """真登记 + 真信箱 + 真 Agent 串一遍：单测里两侧都是假的，这里补上接缝。"""

    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(peers, "peers_dir", lambda: self.root / ".peers")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: os.environ.pop(peers.REF_ENV, None))

    def test_delivered_message_reaches_the_model(self) -> None:
        reg = peers.Registration.create(str(self.root), "m")
        assert reg is not None
        self.addCleanup(reg.close)
        peers.deliver(reg.name, "顺便把 lint 跑一下", sender="other-1 [aaaaaa]")

        agent = self.build([[chunk(content="好"), usage_chunk(10, 5)]], peer=reg)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("继续")

        #  模型真的看到了：消息作为 user 消息进了发给模型的那份历史
        sent = self.client.completions.calls[0]["messages"]
        joined = "\n".join(str(m.get("content") or "") for m in sent)
        self.assertIn("顺便把 lint 跑一下", joined)
        self.assertIn('from="other-1 [aaaaaa]"', joined)
        #  一轮跑完状态回到空闲，且信箱已清空
        self.assertEqual(peers.list_peers()[0].state, peers.STATE_IDLE)
        self.assertEqual(reg.drain(), [])


class TestCliWiring(PeersTestCase):
    """CLI 侧的三条接线：只登记交互式、开关能关、退出必抹掉。"""

    def config(self, **kwargs) -> Config:
        return Config(
            base_url="http://unused", model="m", workspace=Path("/work/zhinu"), **kwargs
        )

    def test_oneshot_is_not_registered(self) -> None:
        from xiaoyu import cli

        self.assertIsNone(cli.register_peer(self.config(), interactive=False))
        self.assertEqual(peers.list_peers(), [])

    def test_switch_off_means_off(self) -> None:
        from xiaoyu import cli

        self.assertIsNone(
            cli.register_peer(self.config(enable_peers=False), interactive=True)
        )

    def test_repl_exit_removes_the_entry_even_on_error(self) -> None:
        import types

        from xiaoyu import cli

        reg = cli.register_peer(self.config(), interactive=True)
        self.assertIsNotNone(reg)
        self.assertEqual(len(peers.list_peers()), 1)

        def boom(_agent):
            raise RuntimeError("REPL 炸了")

        with self.assertRaises(RuntimeError):
            cli.run_repl(boom, types.SimpleNamespace(peer=reg))
        self.assertEqual(peers.list_peers(), [])


class TestPeerTools(PeersTestCase):
    """模型侧的两件套。只做 CLI 那一半时，用户一问「列出所有可用的会话」，
    模型只能靠 which/ls/grep 满机器乱翻——挂上工具才算把能力交到它手里。"""

    def config(self, **kwargs) -> Config:
        return Config(
            base_url="http://unused",
            model="m",
            workspace=Path("/work/zhinu"),
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
            enable_mcp=False,
            **kwargs,
        )

    def test_mounted_only_when_registered(self) -> None:
        from xiaoyu.cli import build_toolbox

        bare = build_toolbox(self.config(), None).names()
        self.assertNotIn("list_sessions", bare)
        self.assertNotIn("send_message", bare)

        with_peer = build_toolbox(self.config(), self.register()).names()
        self.assertIn("list_sessions", with_peer)
        self.assertIn("send_message", with_peer)

    def test_approval_split(self) -> None:
        """list 真只读免确认；send 有外部副作用必须确认——也正是这一条把
        「agent 互发消息跑成对话回环」摁住：跑不了几轮就得经过用户。"""
        from xiaoyu.cli import build_toolbox

        box = build_toolbox(self.config(), self.register())
        self.assertFalse(box.get("list_sessions").requires_approval)
        self.assertTrue(box.get("send_message").requires_approval)

    def test_plan_mode_lets_listing_through_but_not_sending(self) -> None:
        from xiaoyu.agent import PLAN_MODE_TOOLS

        self.assertIn("list_sessions", PLAN_MODE_TOOLS)
        self.assertNotIn("send_message", PLAN_MODE_TOOLS)

    def test_yolo_session_gets_no_peer_tools(self) -> None:
        """--yolo 不登记（默认不收信），于是连工具也不该有——两处不能各说各话。"""
        from xiaoyu.cli import build_toolbox, register_peer

        config = self.config(auto_approve=True, enable_peers=False)
        box = build_toolbox(config, register_peer(config, interactive=True))
        self.assertNotIn("send_message", box.names())

    def test_list_handler_names_peers_and_excludes_self(self) -> None:
        me = self.register("/work/zhinu")
        other = self.register("/work/noc")
        from xiaoyu.cli import build_toolbox

        box = build_toolbox(self.config(), me)
        out = box.get("list_sessions").handler()
        self.assertIn(other.address, out)
        self.assertNotIn(me.ref, out)

    def test_list_handler_when_alone(self) -> None:
        me = self.register()
        from xiaoyu.cli import build_toolbox

        box = build_toolbox(self.config(), me)
        self.assertIn("没有其它在跑", box.get("list_sessions").handler())

    def test_send_handler_delivers_and_stamps_sender(self) -> None:
        me = self.register("/work/zhinu")
        other = self.register("/work/noc")
        from xiaoyu.cli import build_toolbox

        box = build_toolbox(self.config(), me)
        out = box.get("send_message").handler(to=other.name, message="看一眼 CI")
        self.assertIn(other.address, out)
        sender, wrapped = other.drain()[0]
        #  收件方拿到的 from 就是可回信的地址（不是"某个会话"这种没法回的说法）
        self.assertEqual(sender, me.address)
        self.assertIn("看一眼 CI", wrapped)

    def test_send_handler_returns_error_text_not_exception(self) -> None:
        """寻址失败要变成模型看得懂的 tool result，不能把 PeerError 抛进主循环。"""
        me = self.register()
        from xiaoyu.cli import build_toolbox

        box = build_toolbox(self.config(), me)
        out = box.get("send_message").handler(to="查无此人", message="x")
        self.assertTrue(out.startswith("ERROR:"))


class TestYoloDefault(unittest.TestCase):
    def test_yolo_turns_the_inbox_off_by_default(self) -> None:
        """无人值守 + 本机任意进程可投喂指令，两者叠加才是真风险。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIAOYU_ENABLE_PEERS", None)
            cfg = Config.from_env(workspace=Path.cwd(), auto_approve=True)
            self.assertFalse(cfg.enable_peers)
            plain = Config.from_env(workspace=Path.cwd())
            self.assertTrue(plain.enable_peers)

    def test_explicit_env_beats_the_yolo_default(self) -> None:
        with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_PEERS": "1"}):
            cfg = Config.from_env(workspace=Path.cwd(), auto_approve=True)
            self.assertTrue(cfg.enable_peers)


if __name__ == "__main__":
    unittest.main()
