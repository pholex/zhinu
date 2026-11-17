"""委托存档落盘：句柄在进程重启 / resume 之后仍然接得上。

句柄写在委托结论的尾部、随对话历史进了会话日志；存档若只在内存，重启后
历史里的每一个 `resume_from` 都是死链。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from xiaoyu.agents import (
    MAX_DISK_RUNS,
    AgentSpec,
    RunStore,
    SubagentRun,
    make_subagent_tool,
    runs_dir_for,
)

from .test_agent_paths import AgentTestCase, call_fragment, chunk
from .test_agents_spec import text_turn


def handle_in(conclusion: str) -> str:
    """委托结论尾部的句柄。"""
    found = re.search(r"resume_from: ([0-9a-f]{8})", conclusion)
    assert found is not None, conclusion
    return found.group(1)


def make_run(run_id: str, **extra) -> SubagentRun:
    return SubagentRun(
        id=run_id,
        spec_name="worker",
        model="m",
        messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "任务"}],
        **extra,
    )


class RunStoreDiskTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "s.jsonl.runs"

    def test_runs_dir_sits_beside_the_log_and_is_not_a_session_file(self) -> None:
        path = runs_dir_for(Path("/x/sessions/2026-a.jsonl"))
        self.assertEqual(path, Path("/x/sessions/2026-a.jsonl.runs"))
        self.assertNotEqual(path.suffix, ".jsonl")

    def test_round_trip_through_a_fresh_store(self) -> None:
        RunStore(locate=lambda: self.dir).persist(
            make_run("cafe0123", worktree=Path("/w/t"), isolated=True, workdir=Path("/w/t"))
        )
        #  新进程 = 新的空存档，只有目录是同一个
        run = RunStore(locate=lambda: self.dir).recall("cafe0123")
        self.assertIsNotNone(run)
        self.assertEqual((run.spec_name, run.model, run.isolated), ("worker", "m", True))
        self.assertEqual((run.worktree, run.workdir), (Path("/w/t"), Path("/w/t")))
        self.assertEqual(run.messages[1]["content"], "任务")

    @unittest.skipIf(os.name == "nt", "Windows 上 POSIX 权限位无语义")
    def test_archive_is_owner_only(self) -> None:
        RunStore(locate=lambda: self.dir).persist(make_run("cafe0123"))
        self.assertEqual((self.dir / "cafe0123.json").stat().st_mode & 0o777, 0o600)

    def test_handle_that_is_not_a_handle_never_reaches_the_disk(self) -> None:
        #  resume_from 是模型填的：拿去拼文件名之前必须先认形状
        outside = self.dir.parent / "secret.json"
        outside.write_text(json.dumps({"id": "../secret", "messages": [{}, {}]}), encoding="utf-8")
        store = RunStore(locate=lambda: self.dir)
        for bad in ("../secret", "..", "cafe0123/../x", "CAFE0123", "cafe012", "cafe01234", ""):
            with self.subTest(handle=bad):
                self.assertIsNone(store.recall(bad))

    def test_damaged_or_mismatched_archive_counts_as_missing(self) -> None:
        self.dir.mkdir(parents=True)
        (self.dir / "aaaa0000.json").write_text("{半截", encoding="utf-8")
        (self.dir / "bbbb0000.json").write_text(json.dumps([1, 2]), encoding="utf-8")
        #  文件名与内容里的句柄对不上：不认
        (self.dir / "cccc0000.json").write_text(
            json.dumps({"id": "dddd0000", "messages": [{}, {}]}), encoding="utf-8"
        )
        (self.dir / "eeee0000.json").write_text(
            json.dumps({"id": "eeee0000", "messages": "不是列表"}), encoding="utf-8"
        )
        store = RunStore(locate=lambda: self.dir)
        for handle in ("aaaa0000", "bbbb0000", "cccc0000", "eeee0000", "ffff0000"):
            with self.subTest(handle=handle):
                self.assertIsNone(store.recall(handle))

    def test_disk_keeps_only_the_newest(self) -> None:
        store = RunStore(locate=lambda: self.dir)
        total = MAX_DISK_RUNS + 5
        for index in range(total):
            store.persist(make_run(f"{index:08x}"))
            #  按 mtime 排序：同一秒内写的文件要分得出先后
            os.utime(self.dir / f"{index:08x}.json", (index, index))
        kept = sorted(path.stem for path in self.dir.glob("*.json"))
        self.assertEqual(len(kept), MAX_DISK_RUNS)
        self.assertEqual(kept[0], f"{total - MAX_DISK_RUNS:08x}")

    def test_no_location_means_memory_only(self) -> None:
        for store in (RunStore(), RunStore(locate=lambda: None)):
            store.persist(make_run("cafe0123"))
            self.assertIsNone(store.recall("cafe0123"))

    def test_unwritable_location_does_not_raise(self) -> None:
        blocker = self.dir.parent / "blocker"
        blocker.write_text("x", encoding="utf-8")
        #  目录位置上是个文件：建不了目录、写不进去，委托照常收工
        RunStore(locate=lambda: blocker / "runs").persist(make_run("cafe0123"))

        def boom() -> Path:
            raise RuntimeError("定位失败")

        RunStore(locate=boom).persist(make_run("cafe0123"))

    def test_sources_are_searched_after_own_directory(self) -> None:
        origin = self.dir.parent / "old.jsonl.runs"
        RunStore(locate=lambda: origin).persist(make_run("cafe0123"))
        store = RunStore(locate=lambda: self.dir)
        self.assertIsNone(store.recall("cafe0123"))
        store.sources.append(origin)
        self.assertEqual(store.recall("cafe0123").id, "cafe0123")


class ResumeAcrossProcessTest(AgentTestCase):
    SPEC = AgentSpec(
        name="worker", description="干活", system_prompt="在 {workspace} 干活",
        tools=("read_file", "grep", "list_files"),
    )

    def delegate(self, store: RunStore, script: list, arguments: dict) -> str:
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "worker", json.dumps(arguments))])],
            *script,
            text_turn("主收尾"),
        ])
        agent.toolbox.register(
            make_subagent_tool(
                self.SPEC, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, runs=store,
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("委托一下")
        (result,) = [m for m in agent.messages if m["role"] == "tool"]
        return str(result["content"])

    def test_handle_survives_a_restart(self) -> None:
        directory = self.root / "s.jsonl.runs"
        first = self.delegate(
            RunStore(locate=lambda: directory), [text_turn("第一阶段做完")], {"task": "第一阶段"}
        )
        handle = handle_in(first)
        self.assertTrue((directory / f"{handle}.json").is_file())

        #  重启：内存存档是空的，句柄来自历史
        fresh = RunStore(locate=lambda: directory)
        second = self.delegate(
            fresh, [text_turn("第二阶段做完")], {"task": "接着做", "resume_from": handle}
        )
        self.assertNotIn("找不到", second)
        self.assertIn("第二阶段做完", second)
        #  续跑是在第一阶段的上下文上长出来的
        (resumed,) = fresh.values()
        contents = [str(m.get("content")) for m in resumed.messages]
        self.assertTrue(any("第一阶段做完" in text for text in contents))
        self.assertTrue(any("接着做" in text for text in contents))

    def test_without_disk_the_handle_is_dead_after_restart(self) -> None:
        first = self.delegate(RunStore(), [text_turn("做完")], {"task": "第一阶段"})
        handle = handle_in(first)
        second = self.delegate(RunStore(), [], {"task": "接着做", "resume_from": handle})
        self.assertIn("找不到", second)


class AgentWiringTest(AgentTestCase):
    def test_store_follows_the_session_log_and_restore_adds_the_source(self) -> None:
        from unittest import mock

        import xiaoyu.agents as agents_mod
        from xiaoyu.session_log import SessionLog

        from .test_agents_spec import GOOD_SPEC, write_spec

        write_spec(self.root / ".xiaoyu" / "agents", "doc", GOOD_SPEC)
        self.config.enable_agents = True
        log = SessionLog(self.root / "now.jsonl")
        self.addCleanup(log.release)
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            agent = self.build([])
        self.assertIsNone(agent.subagent_runs.directory())
        agent.session_log = log
        self.assertEqual(agent.subagent_runs.directory(), self.root / "now.jsonl.runs")

        source = self.root / "old.jsonl"
        history = [{"role": "user", "content": "旧的一轮"}, {"role": "assistant", "content": "答复"}]
        agent.restore(history, source=str(source))
        agent.restore([], source=str(source))  # 同一来源不重复登记
        self.assertEqual(agent.subagent_runs.sources, [self.root / "old.jsonl.runs"])


if __name__ == "__main__":
    unittest.main()
