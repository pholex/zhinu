"""会话落盘的测试。不碰真实用户目录。"""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.session_log import (
    SESSION_FORMAT,
    LoadedMessages,
    SessionLockedError,
    SessionLog,
    check_session_id,
    has_orphan_compact,
    list_sessions,
    load_messages,
    lock_path,
    open_named,
    sessions_dir,
    usage_digest,
)

ROOT = Path(__file__).resolve().parents[1]


class SessionDirTestCase(unittest.TestCase):
    """公共基座：把会话目录隔离到临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from xiaoyu import session_log as session_log_module

        #  session_log 用 from-import 绑定了 user_config_dir，要 patch 它自己的引用；
        #  patch os.name / 只设 XDG 在 Windows 上都不生效
        patcher = mock.patch.object(
            session_log_module,
            "user_config_dir",
            lambda: Path(self.tmp.name) / "xiaoyu",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def read_lines(self, log: SessionLog) -> list[dict]:
        return [
            json.loads(line)
            for line in log.path.read_text(encoding="utf-8").splitlines()
        ]


class SessionLogTest(SessionDirTestCase):
    def test_create_writes_meta_first_line(self):
        log = SessionLog.create("test-model", "/ws")
        lines = self.read_lines(log)
        self.assertEqual(lines[0]["event"], "meta")
        self.assertEqual(lines[0]["model"], "test-model")
        self.assertEqual(lines[0]["workspace"], "/ws")
        self.assertIn("version", lines[0])
        self.assertTrue(str(log.path.parent).startswith(str(sessions_dir().parent)))

    def test_create_partitions_by_workspace(self):
        """默认目录按工作区分子目录。"""
        log = SessionLog.create("m", "/ws/alpha")
        self.assertEqual(log.path.parent, sessions_dir() / "-ws-alpha")
        other = SessionLog.create("m", "/ws/beta")
        self.assertEqual(other.path.parent, sessions_dir() / "-ws-beta")

    def test_create_explicit_directory(self):
        """嵌入宿主传 directory 即可把会话隔离到独立目录（「sessions 目录宿主可配」①）。"""
        target = Path(self.tmp.name) / "host-sessions"
        log = SessionLog.create("m", "/ws", directory=target)
        self.assertEqual(log.path.parent, target)
        self.assertTrue(log.path.exists())  # meta 行已写入，目录自动建

    def test_append_messages_roundtrip(self):
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "你好"})
        log.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}],
            }
        )
        log.event("compact", note="压了")
        lines = self.read_lines(log)
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[1]["role"], "user")
        self.assertEqual(lines[1]["content"], "你好")
        self.assertEqual(lines[2]["tool_calls"][0]["function"]["name"], "bash")
        self.assertEqual(lines[3]["event"], "compact")
        for line in lines:
            self.assertIn("ts", line)

    def test_write_failure_never_raises(self):
        """磁盘问题只停写，不影响会话。"""
        target = Path(self.tmp.name) / "a-directory"
        target.mkdir()
        log = SessionLog(target)  # 路径是目录 → open 必失败
        log.append({"role": "user", "content": "x"})  # 不抛即通过
        self.assertTrue(log._broken)
        SessionLog(target)  # 停写即放锁：写不进去的句柄没理由占着会话
        log.append({"role": "user", "content": "y"})  # 停写后继续调用也安全

    def test_close_writes_exit_event_once(self):
        """退出约定：exit 事件幂等——信号处理器和 atexit 可能先后都到。

        文件末尾没有 exit 事件 = 异常终止（断电 / kill -9 / 关终端窗口），
        事后拿用户日志据此区分"模型没答"和"进程死了"。
        """
        log = SessionLog.create("m", "/ws")
        log.close("signal:SIGTERM")
        log.close("normal")  # atexit 兜底：已关就不再写
        lines = self.read_lines(log)
        exits = [line for line in lines if line.get("event") == "exit"]
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["reason"], "signal:SIGTERM")

    def install_hooks_for_test(self, log: SessionLog):
        """走 install_exit_logging 拿到它注册的信号处理器，但不真改本进程的
        信号处置与 atexit；在册表与模块状态用例结束即还原。"""
        import signal

        from xiaoyu import session_log as session_log_module

        handlers: dict[int, object] = {}
        for patcher in (
            mock.patch.dict(session_log_module._exit_logs, clear=True),
            mock.patch.object(session_log_module, "_exit_hooks_installed", False),
            #  create=True：状态变量名是实现细节，这里只负责用例后复原
            mock.patch.object(session_log_module, "_signal_reason", None, create=True),
            mock.patch.object(session_log_module.atexit, "register"),
            mock.patch.object(
                session_log_module.signal, "signal",
                side_effect=lambda sig, handler: handlers.setdefault(sig, handler),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        session_log_module.install_exit_logging(log)
        return handlers[signal.SIGTERM]

    def test_signal_landing_in_locked_write_does_not_deadlock(self):
        #  信号处理器跑在主线程、打断的可能正是持着 _mutex 的那段写入。_mutex
        #  不可重入：处理器再阻塞地抢它就是自己等自己，SIGTERM 之后进程永远退不出
        import signal
        import threading

        from xiaoyu import session_log as session_log_module

        log = SessionLog.create("test-model", "/ws")
        on_signal = self.install_hooks_for_test(log)
        codes: list[object] = []

        def deliver() -> None:
            try:
                on_signal(signal.SIGTERM, None)
            except SystemExit as exc:
                codes.append(exc.code)

        #  模拟"被打断的写入正持着锁"；处理器放到别的线程跑，修坏了只卡住那个线程
        log._mutex.acquire()
        worker = threading.Thread(target=deliver, daemon=True)
        worker.start()
        worker.join(timeout=5)
        stuck = worker.is_alive()
        log._mutex.release()
        worker.join(timeout=5)
        self.assertFalse(stuck, "信号处理器卡在 _mutex 上")
        self.assertEqual(codes, [128 + signal.SIGTERM])
        #  处理器没写成的 exit 由解栈路径（收尾方 / atexit）补，reason 仍按信号记
        session_log_module._close_registered("normal")
        exits = [r for r in self.read_lines(log) if r.get("event") == "exit"]
        self.assertEqual([r["reason"] for r in exits], ["signal:SIGTERM"])

    def test_interrupt_anywhere_in_write_propagates(self):
        #  信号处理器抛的 SystemExit 可能落在写入里的任意一处，都必须原样传出去、
        #  日志照常可写。逐个函数入口注入（call 事件）：解释器正是在函数入口、
        #  循环回跳、调用返回处处理挂起的信号。不用 line 事件——它会落在 with
        #  退出序列里 __exit__ 之前这种真实信号到不了的位置，测出假的锁泄漏。
        #  老写法 fdopen 建 TextIOWrapper 时被打断，io.open 已关掉 fd，调用方再关
        #  一次报 EBADF：这个 OSError 顶掉 SystemExit、又被停写兜底吞掉——进程
        #  收了 SIGTERM 却接着跑，日志还被判成坏了
        log = SessionLog.create("test-model", "/ws")
        landed = 0
        for point in range(1, 100_000):
            seen = 0

            def tracer(frame, event, arg):
                nonlocal seen
                if event == "call":
                    seen += 1
                    if seen == point:
                        raise SystemExit(143)
                return None

            raised = False
            sys.settrace(tracer)
            try:
                log.event("mode", value=str(point))
            except SystemExit:
                raised = True
            finally:
                sys.settrace(None)
            self.assertFalse(log._broken, f"第 {point} 个执行点被打断后日志停写了（SystemExit 被吞）")
            self.assertFalse(log._mutex.locked(), f"第 {point} 个执行点被打断后 _mutex 没放")
            if not raised:
                break  # 注入点已越过一次写入的全部执行点
            landed += 1
        self.assertGreater(landed, 10)
        #  中途打断的写入不留半行：每行都能解析
        self.read_lines(log)

    def test_exit_event_is_ignored_on_replay(self):
        """exit / error 事件不参与 resume 重放。"""
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        log.event("error", error="RuntimeError: boom")
        log.close()
        messages = load_messages(log.path)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "任务")

    def test_agent_records_through_session_log(self):
        """Agent._record 是消息入历史的唯一入口，必须同步进日志。"""
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry

        config = Config(base_url="http://unused", model="m", workspace=Path.cwd())
        config.enable_explore = False
        config.enable_skills = False
        log = SessionLog.create("m", "/ws")
        self.addCleanup(log.release)  # Windows 上持有的锁文件挡住临时目录清理
        agent = Agent(config, registry=Registry.for_client(object()), session_log=log)  # client 不会被用到
        agent._record({"role": "user", "content": "任务"})
        agent.reset()
        lines = self.read_lines(log)
        self.assertEqual(lines[1]["content"], "任务")
        self.assertEqual(lines[2]["event"], "clear")


@unittest.skipIf(os.name == "nt", "Windows 上 POSIX 权限位无语义")
class SessionFilePermissionTest(SessionDirTestCase):
    """会话 JSONL 含工具输出、可能带密钥：文件 0600、自建目录 0700。"""

    @staticmethod
    def mode(path: Path) -> int:
        return path.stat().st_mode & 0o777

    def test_new_session_file_and_dirs_are_owner_only(self):
        log = SessionLog.create("m", "/ws/perm")
        self.addCleanup(log.release)
        self.assertEqual(self.mode(log.path), 0o600)
        self.assertEqual(self.mode(lock_path(log.path)), 0o600)
        #  xiaoyu 自己建出来的每一层目录都是 0700
        self.assertEqual(self.mode(log.path.parent), 0o700)
        self.assertEqual(self.mode(sessions_dir()), 0o700)

    def test_existing_wide_file_tightened_on_append(self):
        directory = sessions_dir() / "-ws-old"
        directory.mkdir(parents=True)
        path = directory / "19990101-000000-1.jsonl"
        path.write_text(json.dumps({"event": "meta", "workspace": "/ws/old"}) + "\n", encoding="utf-8")
        os.chmod(path, 0o644)
        log = SessionLog(path)
        self.addCleanup(log.release)
        log.append({"role": "user", "content": "hi"})
        self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.read_lines(log)[-1]["content"], "hi")

    def test_host_directory_permissions_untouched(self):
        """宿主显式传的已存在目录不替它改权限，只管自己建的文件。"""
        target = Path(self.tmp.name) / "host"
        target.mkdir()
        os.chmod(target, 0o755)
        log = SessionLog.create("m", "/ws", directory=target)
        self.addCleanup(log.release)
        self.assertEqual(self.mode(target), 0o755)
        self.assertEqual(self.mode(log.path), 0o600)


class DeferredSessionTest(SessionDirTestCase):
    """defer=True：什么都没发生的会话不在盘上留任何东西。"""

    def test_empty_session_leaves_nothing_on_disk(self):
        log = SessionLog.create("m", "/ws/probe", session_id="sess-probe", defer=True)
        self.assertFalse(log.materialized)
        self.assertFalse(log.path.exists())
        self.assertFalse(lock_path(log.path).exists())
        log.close()
        #  exit 事件也不落：整个会话目录都不该被建出来
        self.assertFalse(sessions_dir().exists())
        self.assertEqual(list_sessions(workspace="/ws/probe"), [])

    def test_first_record_materializes_with_meta_first(self):
        log = SessionLog.create("m", "/ws/probe", session_id="sess-real", defer=True)
        self.addCleanup(log.release)
        log.event("mode", value="plan")
        self.assertTrue(log.materialized)
        lines = self.read_lines(log)
        self.assertEqual([line.get("event") for line in lines], ["meta", "mode"])
        self.assertEqual(lines[0]["session_id"], "sess-real")
        #  落盘即持锁：另一个写句柄抢不到
        with self.assertRaises(SessionLockedError):
            SessionLog(log.path)
        log.append({"role": "user", "content": "问题"})
        (info,) = list_sessions(workspace="/ws/probe")
        self.assertEqual(info.preview, "问题")

    def test_explicit_materialize_then_close_records_exit(self):
        log = SessionLog.create("m", "/ws/probe", defer=True)
        log.materialize()
        self.assertTrue(log.path.exists())
        log.close()
        self.assertEqual([line.get("event") for line in self.read_lines(log)], ["meta", "exit"])

    def test_released_pending_session_can_be_reacquired(self):
        log = SessionLog.create("m", "/ws/probe", defer=True)
        log.release()
        log.append({"role": "user", "content": "丢弃"})  # 放锁后的写入一律丢弃
        self.assertFalse(log.path.exists())
        log.acquire()
        log.append({"role": "user", "content": "留下"})
        self.addCleanup(log.release)
        self.assertEqual(self.read_lines(log)[-1]["content"], "留下")


class UsageDigestTest(SessionDirTestCase):
    """`xiaoyu sessions digest` 的地基：跨会话聚合轮末 usage 快照。"""

    def make_log(self, name: str, workspace: str) -> SessionLog:
        """手动指定文件名建会话文件（create 的时间戳到秒，同秒同 pid 会撞名）。"""
        path = sessions_dir() / name
        log = SessionLog(path)
        log.event(
            "meta", format=SESSION_FORMAT, model="m", workspace=workspace,
            started_at="2026-08-24T00:00:00",
        )
        return log

    @staticmethod
    def usage_event(log: SessionLog, **models: tuple[int, int, int]) -> None:
        log.event(
            "usage",
            turns=sum(calls for calls, _, _ in models.values()),
            prompt_tokens=sum(p for _, p, _ in models.values()),
            completion_tokens=sum(c for _, _, c in models.values()),
            by_model={
                model: {"calls": calls, "prompt_tokens": p, "completion_tokens": c}
                for model, (calls, p, c) in models.items()
            },
        )

    def test_last_snapshot_wins_and_workspaces_aggregate(self):
        """快照是累计值：每文件只算最后一条；同工作区跨文件求和。"""
        a1 = self.make_log("a1.jsonl", "/ws/a")
        self.usage_event(a1, **{"p/m1": (1, 100, 10)})
        self.usage_event(a1, **{"p/m1": (3, 300, 30)})  # 累计快照，覆盖前一条
        a2 = self.make_log("a2.jsonl", "/ws/a")
        self.usage_event(a2, **{"p/m1": (1, 50, 5), "p/m2": (2, 200, 20)})
        b = self.make_log("b.jsonl", "/ws/b")
        self.usage_event(b, **{"p/m1": (1, 7, 3)})
        digest = usage_digest()
        self.assertEqual(set(digest.by_workspace), {"/ws/a", "/ws/b"})
        ws_a = digest.by_workspace["/ws/a"]
        self.assertEqual(ws_a.sessions, 2)
        self.assertEqual(ws_a.by_model["p/m1"], [4, 350, 35])
        self.assertEqual(ws_a.by_model["p/m2"], [2, 200, 20])
        self.assertEqual(ws_a.prompt_tokens, 550)
        self.assertEqual(ws_a.completion_tokens, 55)
        self.assertEqual(digest.no_usage, 0)
        self.assertEqual(digest.corrupt, 0)

    def test_files_without_usage_are_counted_not_silenced(self):
        self.make_log("old.jsonl", "/ws/a")  # 只有 meta：旧版本记录/零调用
        digest = usage_digest()
        self.assertEqual(digest.no_usage, 1)
        self.assertEqual(digest.by_workspace, {})

    def test_truncated_usage_line_is_counted(self):
        """断电截断的 usage 半行：跳过且计数，绝不静默。"""
        log = self.make_log("t.jsonl", "/ws/a")
        self.usage_event(log, **{"p/m1": (1, 100, 10)})
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write('{"ts": "x", "event": "usage", "turns": 2, "prompt_to')
        digest = usage_digest()
        self.assertEqual(digest.corrupt, 1)
        #  完整的那条照常入账
        self.assertEqual(digest.by_workspace["/ws/a"].by_model["p/m1"], [1, 100, 10])

    def test_workspace_filter_uses_meta(self):
        a = self.make_log("a.jsonl", "/ws/a")
        self.usage_event(a, **{"p/m1": (1, 100, 10)})
        b = self.make_log("b.jsonl", "/ws/b")
        self.usage_event(b, **{"p/m1": (1, 7, 3)})
        digest = usage_digest(workspace="/ws/a")
        self.assertEqual(set(digest.by_workspace), {"/ws/a"})

    def test_usage_event_is_ignored_on_replay(self):
        """resume 重放不认识 usage 事件——照常跳过，不进历史。"""
        log = self.make_log("r.jsonl", "/ws/a")
        log.append({"role": "user", "content": "hi"})
        self.usage_event(log, **{"p/m1": (1, 100, 10)})
        messages = load_messages(log.path)
        self.assertEqual([m["role"] for m in messages], ["user"])

    def test_agent_logs_cumulative_snapshot_only_on_change(self):
        """轮末落累计快照；用量没变的轮不重复写。"""
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry

        config = Config(base_url="http://unused", model="m", workspace=Path.cwd())
        config.enable_explore = False
        config.enable_skills = False
        log = SessionLog.create("m", "/ws")
        self.addCleanup(log.release)  # Windows 上持有的锁文件挡住临时目录清理
        agent = Agent(config, registry=Registry.for_client(object()), session_log=log)
        agent._log_usage()  # 零调用：不写
        agent.usage.add("p/m1", 100, 10)
        agent._log_usage()
        agent._log_usage()  # 没变：不重复写
        agent.usage.add("p/m1", 50, 5)
        agent._log_usage()
        events = [line for line in self.read_lines(log) if line.get("event") == "usage"]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["by_model"]["p/m1"]["prompt_tokens"], 100)
        self.assertEqual(events[1]["by_model"]["p/m1"]["prompt_tokens"], 150)  # 累计

    def test_send_snapshots_usage_even_when_turn_dies(self):
        """接线在 send() 的 finally：异常轮已烧掉的 token 也入账。"""
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.providers import Registry

        config = Config(base_url="http://unused", model="m", workspace=Path.cwd())
        config.enable_explore = False
        config.enable_skills = False
        log = SessionLog.create("m", "/ws")
        self.addCleanup(log.release)  # Windows 上持有的锁文件挡住临时目录清理
        agent = Agent(config, registry=Registry.for_client(object()), session_log=log)

        def dying_turn(user_input):
            agent.usage.add("p/m1", 100, 10)
            raise RuntimeError("boom")

        agent._turn = dying_turn
        with self.assertRaises(RuntimeError):
            agent.send("hi")
        events = [line for line in self.read_lines(log) if line.get("event") == "usage"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["prompt_tokens"], 100)


class ResumeTest(SessionDirTestCase):
    """`xiaoyu resume` 的地基：列出历史会话 + 重放消息。"""

    def test_load_messages_plain_replay(self):
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        log.append({"role": "assistant", "content": "好的"})
        messages = load_messages(log.path)
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "任务"},
                {"role": "assistant", "content": "好的"},
            ],
        )

    def test_compact_replacement_resets_history(self):
        """compact 事件带 replacement 时整体替换——resume 不必理解压缩语义。"""
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        log.append({"role": "assistant", "content": "很长的中间过程"})
        log.event(
            "compact",
            note="已压缩",
            replacement=[{"role": "user", "content": "任务\n\n[摘要]"}],
        )
        log.append({"role": "assistant", "content": "压缩后的新回复"})
        messages = load_messages(log.path)
        self.assertEqual(len(messages), 2)
        self.assertIn("[摘要]", messages[0]["content"])
        self.assertEqual(messages[1]["content"], "压缩后的新回复")

    def test_old_format_compact_event_is_ignored(self):
        #  旧版本的 compact 事件没有 replacement：跳过，消息照常累积
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        log.event("compact", note="已压缩")
        messages = load_messages(log.path)
        self.assertEqual(len(messages), 1)

    def test_clear_event_empties_history(self):
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "旧任务"})
        log.event("clear")
        log.append({"role": "user", "content": "新任务"})
        messages = load_messages(log.path)
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["content"], "新任务")

    def test_newer_format_is_rejected(self):
        """遇到比当前实现新的格式版本要明确拒绝，不能静默错乱。"""
        log = SessionLog.create("m", "/ws")
        raw = log.path.read_text(encoding="utf-8").splitlines()
        meta = json.loads(raw[0])
        meta["format"] = SESSION_FORMAT + 1
        log.path.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_messages(log.path)

    def test_half_written_tail_is_skipped(self):
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write('{"role": "assistant", "cont')  # 写到一半断了
        messages = load_messages(log.path)
        self.assertEqual(len(messages), 1)
        #  尾部半行是崩溃的正常形态：静默，不计入损坏
        self.assertEqual(messages.corrupt_lines, [])

    def test_list_sessions_reads_head_only(self):
        log_a = SessionLog.create("model-a", "/ws/a")
        log_a.append({"role": "user", "content": "修复登录页面的重定向问题"})
        #  文件名靠时间戳排序，同一秒建两个会撞名；直接改名模拟更早的会话
        earlier = log_a.path.with_name("19990101-000000-1.jsonl")
        log_a.path.rename(earlier)
        log_b = SessionLog.create("model-b", "/ws/b")
        log_b.append({"role": "user", "content": "写一个爬虫"})

        infos = list_sessions()
        self.assertEqual(len(infos), 2)
        #  倒序：新的在前
        self.assertEqual(infos[0].model, "model-b")
        self.assertIn("写一个爬虫", infos[0].preview)
        self.assertEqual(infos[1].model, "model-a")

    def test_list_sessions_workspace_filter(self):
        log = SessionLog.create("m", "/ws/target")
        log.append({"role": "user", "content": "x"})
        self.assertEqual(len(list_sessions(workspace="/ws/target")), 1)
        self.assertEqual(list_sessions(workspace="/ws/other"), [])

    def test_list_sessions_includes_legacy_flat_files(self):
        """分区之前的存量文件平铺在根目录：列举兼容，不搬家。"""
        #  存量平铺文件：直接写在 sessions 根目录，模拟旧版本产物
        legacy = SessionLog(sessions_dir() / "19990101-000000-1.jsonl")
        legacy.event(
            "meta",
            format=SESSION_FORMAT,
            version="0.22",
            model="model-old",
            workspace="/ws/a",
            started_at="1999-01-01T00:00:00",
        )
        legacy.append({"role": "user", "content": "旧任务"})
        new = SessionLog.create("model-new", "/ws/a")
        new.append({"role": "user", "content": "新任务"})

        infos = list_sessions()
        self.assertEqual([info.model for info in infos], ["model-new", "model-old"])
        #  workspace 过滤也要能带出存量平铺文件（过滤以 meta 为准，不是目录名）
        infos = list_sessions(workspace="/ws/a")
        self.assertEqual(len(infos), 2)
        self.assertEqual(list_sessions(workspace="/ws/other"), [])


class NamedSessionTest(SessionDirTestCase):
    """`--session-id`：有则续、无则建。"""

    def test_create_new_when_absent(self):
        log, messages = open_named("nightly", "m", "/ws")
        self.assertEqual(messages, [])
        self.assertTrue(log.path.name.endswith("-id-nightly.jsonl"))
        meta = self.read_lines(log)[0]
        self.assertEqual(meta["session_id"], "nightly")

    def test_reopen_appends_to_same_file_without_copying(self):
        """第二次调用续写同一个文件，历史只回放不重抄——否则每次调用翻倍。"""
        first, _ = open_named("nightly", "m", "/ws")
        first.append({"role": "user", "content": "第一步"})
        first.append({"role": "assistant", "content": "好"})
        #  真实场景里第二次调用是另一个进程，前一个已退出（写锁随之释放）
        first.close()

        second, messages = open_named("nightly", "m", "/ws")
        self.assertEqual(second.path, first.path)
        self.assertEqual([m["content"] for m in messages], ["第一步", "好"])
        #  续写点留痕，且**没有**把历史再抄一遍
        kinds = [line.get("event") for line in self.read_lines(second)]
        self.assertEqual(kinds.count("reopened"), 1)
        self.assertEqual(len(self.read_lines(second)), 5)  # meta + 2 条消息 + exit + reopened

        second.append({"role": "user", "content": "第二步"})
        second.close()
        _, again = open_named("nightly", "m", "/ws")
        self.assertEqual([m["content"] for m in again], ["第一步", "好", "第二步"])

    def test_named_sessions_are_isolated_by_name_and_workspace(self):
        a, _ = open_named("alpha", "m", "/ws")
        b, _ = open_named("beta", "m", "/ws")
        self.assertNotEqual(a.path, b.path)
        #  同名但不同工作区也是两个会话（命名会话仍按工作区分区）
        other, messages = open_named("alpha", "m", "/ws/other")
        self.assertNotEqual(other.path, a.path)
        self.assertEqual(messages, [])

    def test_lookup_is_decided_by_meta_not_filename(self):
        """会话名自己可以含 `-id-`，文件名 glob 会撞——认定以 meta 为准。"""
        decoy, _ = open_named("beta-id-alpha", "m", "/ws")
        decoy.append({"role": "user", "content": "诱饵"})
        log, messages = open_named("alpha", "m", "/ws")
        self.assertNotEqual(log.path, decoy.path)
        self.assertEqual(messages, [])

    def test_named_session_listed_and_sorted_by_time(self):
        """命名会话照常出现在 resume 列表里，且不因文件名前缀乱了时间序。"""
        old, _ = open_named("nightly", "m", "/ws")
        old.append({"role": "user", "content": "旧任务"})
        old.path.rename(old.path.with_name("19990101-000000-1-id-nightly.jsonl"))
        new = SessionLog.create("m", "/ws")
        new.append({"role": "user", "content": "新任务"})

        infos = list_sessions(workspace="/ws")
        self.assertEqual([i.preview for i in infos], ["新任务", "旧任务"])
        self.assertEqual([i.session_id for i in infos], ["", "nightly"])

    def test_bad_session_id_is_rejected_not_sanitized(self):
        """洗名字等于让两个脚本以为各写各的、实际共用一个会话——一律报错。"""
        for bad in ("", "  ", "a/b", "../etc", "a b", "会话", "x" * 65, "..", "-"):
            with self.assertRaises(ValueError, msg=bad):
                check_session_id(bad)

    def test_good_session_id_passes_through(self):
        for good in ("nightly", "build-42", "ci_run.1", "A-Z_0.9"):
            self.assertEqual(check_session_id(good), good)
        self.assertEqual(check_session_id("  padded  "), "padded")


#  子进程持锁脚本：拿到锁报一声，然后等 stdin（主进程决定它什么时候死）
_HOLDER_SCRIPT = (
    "import sys\n"
    "from pathlib import Path\n"
    "from xiaoyu.session_log import SessionLog\n"
    "log = SessionLog(Path(sys.argv[1]))\n"
    "print('locked', flush=True)\n"
    "sys.stdin.readline()\n"
)


class SessionLockTest(SessionDirTestCase):
    """跨进程写锁：同一会话文件同一时刻只允许一个写句柄。

    锁在旁车锁文件上（内核锁，进程死亡自动释放），日志本身不上锁——读的一方不受影响。
    """

    def test_second_writer_in_same_process_is_refused(self):
        """flock 按 open file description 算，同进程再开一次也抢不到——
        ACP 同进程 session/load 必须先让出旧句柄的锁就是这个原因。"""
        first = SessionLog.create("m", "/ws", session_id="nightly")
        with self.assertRaises(SessionLockedError) as ctx:
            SessionLog(first.path)
        self.assertEqual(ctx.exception.pid, os.getpid())
        self.assertIn("nightly", str(ctx.exception))
        self.assertIn(str(os.getpid()), str(ctx.exception))
        #  读不受影响
        self.assertEqual(load_messages(first.path), [])

    def test_close_releases_lock_and_keeps_lock_file(self):
        first = SessionLog.create("m", "/ws")
        first.append({"role": "user", "content": "任务"})
        first.close()
        #  锁文件留着（删了再建有 inode 竞态），且不会被当成会话列出来
        self.assertTrue(lock_path(first.path).is_file())
        self.assertEqual(len(list_sessions()), 1)
        second = SessionLog(first.path)
        second.append({"role": "assistant", "content": "好"})
        self.assertEqual([m["content"] for m in load_messages(first.path)], ["任务", "好"])

    def test_writes_after_close_are_dropped(self):
        """close 放掉锁之后别的进程随时可能接手，迟到的写入不能再落进去——
        也顺带守住"exit 事件是最后一条"的判据。"""
        log = SessionLog.create("m", "/ws")
        log.close()
        log.append({"role": "user", "content": "迟到"})
        kinds = [line.get("event") or line.get("role") for line in self.read_lines(log)]
        self.assertEqual(kinds, ["meta", "exit"])

    def test_release_then_acquire_again(self):
        """release 不写 exit，只让出锁（ACP 同进程重载用）；acquire 重新拿回。"""
        log = SessionLog.create("m", "/ws")
        log.release()
        other = SessionLog(log.path)
        with self.assertRaises(SessionLockedError):
            log.acquire()
        other.release()
        log.acquire()
        log.append({"role": "user", "content": "接着写"})
        kinds = [line.get("event") or line.get("role") for line in self.read_lines(log)]
        self.assertEqual(kinds, ["meta", "user"])

    def test_dropped_handle_releases_lock(self):
        """没人 close 的句柄（装配中途失败被丢掉的）被回收时也要放锁，不能把会话锁死到进程退出。"""
        path = SessionLog.create("m", "/ws").path
        gc.collect()
        SessionLog(path)  # 不抛即通过

    def test_other_process_blocks_until_it_dies(self):
        path = SessionLog.create("m", "/ws").path
        gc.collect()
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER_SCRIPT, str(path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(ROOT),
        )
        try:
            self.assertEqual(proc.stdout.readline().strip(), "locked")
            with self.assertRaises(SessionLockedError) as ctx:
                SessionLog(path)
            self.assertEqual(ctx.exception.pid, proc.pid)
            self.assertIn("另一个进程", str(ctx.exception))
        finally:
            #  kill 而不是让它正常退出：锁的释放不能依赖进程收尾代码
            proc.kill()
            proc.wait(timeout=15)
            proc.stdin.close()
            proc.stdout.close()
        SessionLog(path)  # 进程一死内核就放锁

    def test_open_named_refuses_locked_session_without_leaking(self):
        first, _ = open_named("nightly", "m", "/ws")
        first.append({"role": "user", "content": "第一步"})
        with self.assertRaises(SessionLockedError):
            open_named("nightly", "m", "/ws")
        first.close()
        second, messages = open_named("nightly", "m", "/ws")
        self.assertEqual([m["content"] for m in messages], ["第一步"])
        #  失败那次没留下半截痕迹：只有第二次成功的续写点
        kinds = [line.get("event") for line in self.read_lines(second)]
        self.assertEqual(kinds.count("reopened"), 1)

    def test_torn_tail_is_sealed_on_reopen(self):
        """崩溃留下的半行没有换行符：续写前不补一个换行，下一条记录会粘在半行后面一起坏掉。"""
        first, _ = open_named("nightly", "m", "/ws")
        first.append({"role": "user", "content": "第一步"})
        first.release()  # 模拟进程死掉：不写 exit
        with first.path.open("a", encoding="utf-8") as handle:
            handle.write('{"role": "assistant", "cont')
        second, messages = open_named("nightly", "m", "/ws")
        self.assertEqual([m["content"] for m in messages], ["第一步"])
        self.assertEqual(messages.corrupt_lines, [])
        second.append({"role": "assistant", "content": "续上了"})
        again = load_messages(second.path)
        self.assertEqual([m["content"] for m in again], ["第一步", "续上了"])
        #  崩溃半行是已知的尾巴，续写之后也不算中段损坏
        self.assertEqual(again.corrupt_lines, [])


class _FakeMsvcrt:
    """msvcrt.locking 的替身：按 (dev, inode) 记谁锁了第 0 字节，语义照 Windows（被占即 OSError）。"""

    LK_UNLCK = 0
    LK_NBLCK = 2

    def __init__(self) -> None:
        self.held: dict[tuple[int, int], int] = {}
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        stat = os.fstat(fd)
        key = (stat.st_dev, stat.st_ino)
        if mode == self.LK_NBLCK:
            if key in self.held:
                raise OSError(13, "Permission denied")
            self.held[key] = fd
        elif mode == self.LK_UNLCK:
            if self.held.get(key) != fd:
                raise OSError(13, "Permission denied")
            del self.held[key]


class WindowsLockBranchTest(SessionDirTestCase):
    """Windows 分支（msvcrt.locking 锁锁文件第 0 字节）在本机用替身跑一遍逻辑。"""

    def setUp(self):
        super().setUp()
        from xiaoyu import session_log as session_log_module

        self.fake = _FakeMsvcrt()
        for patcher in (
            mock.patch.object(session_log_module, "_WINDOWS", True),
            mock.patch.dict(sys.modules, {"msvcrt": self.fake}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        if os.name != "nt":
            import fcntl

            patcher = mock.patch.object(fcntl, "flock", side_effect=AssertionError("Windows 分支不该碰 flock"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_mutual_exclusion_release_and_reopen(self):
        first = SessionLog.create("m", "/ws", session_id="nightly")
        with self.assertRaises(SessionLockedError) as ctx:
            SessionLog(first.path)
        #  pid 写在第 1 字节之后：第 0 字节被锁着，别的进程在 Windows 上读不了它
        self.assertEqual(ctx.exception.pid, os.getpid())
        first.close()
        second = SessionLog(first.path)
        second.close()
        self.assertTrue(self.fake.calls)
        for mode, nbytes, offset in self.fake.calls:
            self.assertEqual((nbytes, offset), (1, 0))
        modes = [mode for mode, _, _ in self.fake.calls]
        #  抢(成) 抢(败) 放 抢(成) 放
        self.assertEqual(
            modes,
            [_FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_UNLCK,
             _FakeMsvcrt.LK_NBLCK, _FakeMsvcrt.LK_UNLCK],
        )
        self.assertEqual(self.fake.held, {})


class CorruptLineTest(SessionDirTestCase):
    """读回坏行：只有最后一行（崩溃留下的半行）静默跳过，中段坏行要计数、让用户看见。"""

    def raw(self, log: SessionLog, text: str) -> None:
        with log.path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def test_mid_file_bad_lines_are_counted(self):
        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        self.raw(log, '{"role": "assistant", "tool_ca\n')  # 交错写坏的一行
        self.raw(log, "42\n")  # 能解析但不是记录，同样算坏
        log.append({"role": "assistant", "content": "好"})
        messages = load_messages(log.path)
        self.assertIsInstance(messages, list)
        self.assertEqual([m["content"] for m in messages], ["任务", "好"])
        self.assertEqual(messages.corrupt_lines, [3, 4])

    def test_bad_line_before_replacement_is_not_reported(self):
        """compact/clear 之后历史整体重建，之前的坏行不影响接回的内容，不必惊动用户。"""
        log = SessionLog.create("m", "/ws")
        self.raw(log, "{broken\n")
        log.event("clear")
        log.append({"role": "user", "content": "新任务"})
        self.assertEqual(load_messages(log.path).corrupt_lines, [])

    def test_restore_surfaces_corruption_as_notice(self):
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config
        from xiaoyu.events import Notice
        from xiaoyu.providers import Registry

        log = SessionLog.create("m", "/ws")
        log.append({"role": "user", "content": "任务"})
        self.raw(log, "{broken\n")
        log.append({"role": "assistant", "content": "好"})
        log.close()

        class ListSink:
            def __init__(self) -> None:
                self.events: list = []

            def emit(self, event) -> None:
                self.events.append(event)

        config = Config(base_url="http://unused", model="m", workspace=Path.cwd())
        config.enable_explore = False
        config.enable_skills = False
        sink = ListSink()
        agent = Agent(config, registry=Registry.for_client(object()), sink=sink)
        agent.restore(load_messages(log.path), copy=False)
        notices = [e for e in sink.events if isinstance(e, Notice) and "损坏" in e.text]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].level, "warn")
        self.assertIn("第 3 行", notices[0].text)

        #  干净的历史不出这条提示
        clean = ListSink()
        agent = Agent(config, registry=Registry.for_client(object()), sink=clean)
        agent.restore(LoadedMessages([{"role": "user", "content": "任务"}]), copy=False)
        self.assertFalse([e for e in clean.events if isinstance(e, Notice) and "损坏" in e.text])


class OrphanCompactTest(unittest.TestCase):
    """压缩日志锁（start‖end 括号）：孤儿 start = 死在压缩中途。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "s.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *records: dict) -> None:
        self.path.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def test_orphan_start_detected(self):
        self.write({"event": "meta", "format": 2}, {"event": "compact_start"})
        self.assertTrue(has_orphan_compact(self.path))

    def test_paired_bracket_is_clean(self):
        #  失败的压缩也会写 compact_end（ok=False）：成败都不算孤儿，只有崩溃算
        self.write(
            {"event": "compact_start"},
            {"event": "compact", "note": "跳过"},
            {"event": "compact_end", "ok": False},
        )
        self.assertFalse(has_orphan_compact(self.path))

    def test_second_orphan_after_paired_bracket(self):
        self.write(
            {"event": "compact_start"},
            {"event": "compact_end", "ok": True},
            {"event": "compact_start"},
        )
        self.assertTrue(has_orphan_compact(self.path))

    def test_no_compact_events_is_clean(self):
        self.write({"event": "meta", "format": 2}, {"role": "user", "content": "hi"})
        self.assertFalse(has_orphan_compact(self.path))


if __name__ == "__main__":
    unittest.main()
