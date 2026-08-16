"""会话落盘的测试。不碰真实用户目录。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.session_log import (
    SESSION_FORMAT,
    SessionLog,
    check_session_id,
    has_orphan_compact,
    list_sessions,
    load_messages,
    open_named,
    sessions_dir,
)


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
        log = SessionLog(Path(self.tmp.name))  # 路径是目录 → open 必失败
        log.append({"role": "user", "content": "x"})  # 不抛即通过
        self.assertTrue(log._broken)
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
        agent = Agent(config, registry=Registry.for_client(object()), session_log=log)  # client 不会被用到
        agent._record({"role": "user", "content": "任务"})
        agent.reset()
        lines = self.read_lines(log)
        self.assertEqual(lines[1]["content"], "任务")
        self.assertEqual(lines[2]["event"], "clear")


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

        second, messages = open_named("nightly", "m", "/ws")
        self.assertEqual(second.path, first.path)
        self.assertEqual([m["content"] for m in messages], ["第一步", "好"])
        #  续写点留痕，且**没有**把历史再抄一遍
        kinds = [line.get("event") for line in self.read_lines(second)]
        self.assertEqual(kinds.count("reopened"), 1)
        self.assertEqual(len(self.read_lines(second)), 4)  # meta + 2 条消息 + reopened

        second.append({"role": "user", "content": "第二步"})
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
