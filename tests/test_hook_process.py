"""真实 hook 的有限输出、取消与进程树回收；所有夹具都有产量/寿命上限。"""

import contextlib
import gc
import os
import signal
import threading
import time
import tracemalloc
import unittest
from unittest import mock

from xiaoyu import hooks
from xiaoyu.embedding import measured_send
from xiaoyu.errors import Interrupted
from xiaoyu.hooks import Hook, HookEngine
from .test_agent_paths import AgentTestCase
from .test_hooks import _script_cmd, text_turn, tool_turn
from .test_render import RecordingSink


class HookProcessTest(AgentTestCase):
    def tearDown(self):
        try:
            self.assertEqual(hooks._active, {}, "hook 退出后仍持有进程或 I/O 线程")
        finally:
            super().tearDown()

    def test_running_hooks_cancel_promptly_and_preserve_tool_results(self):
        for event in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "ToolFailed"):
            with self.subTest(event=event):
                ready = self.root / (event + ".ready")
                ran_next = self.root / (event + ".next")
                cmd = _script_cmd(self.root, event + ".py",
                    "import time\nfrom pathlib import Path\n"
                    f"Path({str(ready)!r}).touch()\ntime.sleep(4)\n")
                second = _script_cmd(self.root, event + "_next.py",
                    f"from pathlib import Path\nPath({str(ran_next)!r}).touch()\n")
                engine = HookEngine([Hook(event, cmd), Hook(event, second)], self.root)
                turns = [text_turn("下一轮正常")]
                if event != "UserPromptSubmit":
                    turns.insert(0, tool_turn("read_file", {"path": "missing.txt"}) if event == "ToolFailed"
                                 else tool_turn("write_file", {"path": event + ".txt", "content": "done"}))
                agent = self.build(turns, hook_engine=engine, sink=RecordingSink())
                results, failures = [], []

                def send():
                    try:
                        results.append(measured_send(agent, "有界取消测试"))
                    except BaseException as exc:
                        failures.append(exc)

                worker = threading.Thread(target=send)
                worker.start()
                deadline = time.monotonic() + 3
                while not ready.exists() and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                try:
                    self.assertTrue(ready.exists(), failures)
                    started = time.monotonic()
                    agent.interrupt()
                finally:
                    worker.join(8)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertTrue(results[0].interrupted)
                self.assertFalse(ran_next.exists())
                self.assertEqual(len(self.client.completions.calls), int(event != "UserPromptSubmit"))
                if event in ("PostToolUse", "ToolFailed"):
                    result = [m for m in agent.messages if m["role"] == "tool"]
                    self.assertEqual(len(result), 1)
                    self.assertIn("文件不存在" if event == "ToolFailed" else "已创建", result[0]["content"])
                if event == "PreToolUse":
                    self.assertFalse((self.root / (event + ".txt")).exists())
                agent.hook_engine = None
                self.assertEqual(measured_send(agent, "继续").text, "下一轮正常")

    def test_large_both_streams_have_bounded_parent_memory(self):
        cmd = _script_cmd(self.root, "large.py",
            "import sys\n"
            "sys.stdout.buffer.write(b'FIRST\\n')\n"
            "for _ in range(128):\n"
            "    sys.stdout.buffer.write(b'o' * 65536)\n"
            "    sys.stderr.buffer.write(b'e' * 65536)\n"
            "sys.stderr.buffer.write(b'\\nFINAL-ERROR')\nsys.exit(2)\n")
        engine = HookEngine([Hook("PreToolUse", cmd, timeout=8)], self.root)
        gc.collect()
        tracemalloc.start()
        try:
            decision = engine.fire("PreToolUse", {})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(decision.blocked)
        self.assertIn("FINAL-ERROR", decision.reason)
        self.assertLess(peak, 6 * 1024 * 1024, f"父进程收集峰值 {peak} 不应随 16 MiB 输出等比例增长")

    def test_subagent_lifecycle_cancel_preserves_archive(self):
        from xiaoyu import agents
        from .test_agents_spec import write_spec, DelegationTest

        write_spec(self.root / ".xiaoyu" / "agents", "writer", DelegationTest.WRITER_SPEC)
        self.config.enable_agents = True
        for event in ("SubagentStart", "SubagentEnd"):
            with self.subTest(event=event):
                ready = self.root / (event + ".ready")
                cmd = _script_cmd(self.root, event + ".py",
                    "import time\nfrom pathlib import Path\n"
                    f"Path({str(ready)!r}).touch()\ntime.sleep(4)\n")
                engine = HookEngine([Hook(event, cmd)], self.root)
                with mock.patch.object(agents, "user_config_dir", lambda: self.root / "cfg"):
                    agent = self.build([
                        tool_turn("writer", {"task": "写文件"}),
                        tool_turn("write_file", {"path": "child.txt", "content": "done"}),
                        text_turn("子任务完成"),
                    ], hook_engine=engine, sink=RecordingSink())
                agent.mode = "auto"
                results, failures = [], []

                def send():
                    try:
                        results.append(measured_send(agent, "委托"))
                    except BaseException as exc:
                        failures.append(exc)

                worker = threading.Thread(target=send)
                worker.start()
                deadline = time.monotonic() + 3
                while not ready.exists() and worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.01)
                try:
                    self.assertTrue(ready.exists(), failures)
                    started = time.monotonic()
                    agent.interrupt()
                finally:
                    worker.join(8)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertLess(time.monotonic() - started, 2.5)
                self.assertTrue(results[0].interrupted)
                self.assertEqual(len(self.client.completions.calls), 1 if event == "SubagentStart" else 3)
                self.assertTrue(agent.subagent_runs, "取消生命周期 hook 后子任务仍须归档")
                if event == "SubagentStart":
                    self.assertFalse((self.root / "child.txt").exists())
                else:
                    self.assertEqual((self.root / "child.txt").read_text(encoding="utf-8"), "done")
                    record = list(agent.subagent_runs.values())[-1]
                    self.assertTrue(any(m.get("content") == "子任务完成" for m in record.messages))

    def test_truncated_json_is_not_injected_as_plain_text(self):
        notices = []
        cmd = _script_cmd(self.root, "json_output.py",
            "import json\nprint(json.dumps({'padding': 'x' * 1048576, 'systemMessage': '末尾提示'}))\n")
        decision = HookEngine([Hook("UserPromptSubmit", cmd)], self.root, notices.append).fire(
            "UserPromptSubmit", {})
        self.assertFalse(decision.blocked)
        self.assertEqual((decision.output, decision.notice), ("", ""))
        self.assertTrue(any("截断" in note for note in notices))

    def test_large_text_keeps_first_line_and_warns(self):
        notices = []
        cmd = _script_cmd(self.root, "text.py", "print('首行上下文')\nprint('x' * 1048576)\n")
        decision = HookEngine([Hook("UserPromptSubmit", cmd)], self.root, notices.append).fire(
            "UserPromptSubmit", {})
        self.assertEqual(decision.output, "首行上下文")
        self.assertTrue(any("截断" in note for note in notices))

    def test_large_stdin_to_nonreading_hook_still_times_out(self):
        notices = []
        cmd = _script_cmd(self.root, "no_read.py", "import time\ntime.sleep(4)\n")
        engine = HookEngine([Hook("PreToolUse", cmd, timeout=0.2, on_failure="block")],
                            self.root, notices.append)
        started = time.monotonic()
        result = engine.fire("PreToolUse", {"args": {"content": "x" * 1048576}})
        self.assertTrue(result.blocked)
        self.assertIn("超时", result.reason)
        self.assertLess(time.monotonic() - started, 2.5)

    @unittest.skipIf(os.name == "nt", "进程组与 /bin/ps 用例仅 POSIX")
    def test_timeout_reaps_descendant_after_leader_exits(self):
        pid_file = self.root / "child.pid"
        cmd = _script_cmd(self.root, "spawn.py",
            "import subprocess, sys\nfrom pathlib import Path\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(p.pid))\n")
        engine = HookEngine([Hook("PreToolUse", cmd, timeout=0.3, on_failure="block")], self.root)
        try:
            result = engine.fire("PreToolUse", {})
            self.assertTrue(result.blocked)
            pid = int(pid_file.read_text(encoding="utf-8"))
            self.assert_process_stopped(pid)
        finally:
            if pid_file.exists():
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)

    def test_cancel_before_spawn_and_scope_restores(self):
        target = self.root / "started.txt"
        cmd = _script_cmd(self.root, "scope.py", f"from pathlib import Path\nPath({str(target)!r}).touch()\n")
        engine = HookEngine([Hook("Stop", cmd)], self.root)
        with hooks.stop_scope(lambda: True):
            with self.assertRaises(Interrupted):
                engine.fire("Stop", {})
        self.assertFalse(target.exists())
        self.assertFalse(engine.fire("Stop", {}).blocked)
        self.assertTrue(target.exists())

    def test_keyboard_interrupt_reaps_child(self):
        ready = self.root / "interrupt.ready"
        cmd = _script_cmd(self.root, "interrupt.py",
            "import time\nfrom pathlib import Path\n"
            f"Path({str(ready)!r}).touch()\ntime.sleep(4)\n")
        procs = []
        original = hooks.subprocess.Popen

        def spawn(*args, **kwargs):
            proc = original(*args, **kwargs)
            procs.append(proc)
            return proc

        def stop():
            if ready.exists():
                raise KeyboardInterrupt
            return False

        with mock.patch.object(hooks.subprocess, "Popen", side_effect=spawn), hooks.stop_scope(stop):
            with self.assertRaises(KeyboardInterrupt):
                HookEngine([Hook("Stop", cmd)], self.root).fire("Stop", {})
        self.assertIsNotNone(procs[0].returncode)

    @unittest.skipIf(os.name == "nt", "SIGTERM 忽略用例仅 POSIX")
    def test_timeout_escalates_for_term_ignoring_child(self):
        pid_file = self.root / "stubborn.pid"
        cmd = _script_cmd(self.root, "stubborn.py",
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\ntime.sleep(5)\n")
        result = HookEngine([Hook("PreToolUse", cmd, timeout=0.3, on_failure="block")], self.root).fire(
            "PreToolUse", {})
        self.assertTrue(result.blocked)
        self.assert_process_stopped(int(pid_file.read_text(encoding="utf-8")))

    def test_large_stdin_is_delivered_completely(self):
        cmd = _script_cmd(self.root, "read_input.py", "import sys, json\nprint(len(json.load(sys.stdin)['value']))\n")
        decision = HookEngine([Hook("UserPromptSubmit", cmd)], self.root).fire(
            "UserPromptSubmit", {"value": "中文" * 524288})
        self.assertEqual(decision.output, "1048576")

    def assert_process_stopped(self, pid):
        import subprocess
        result = subprocess.run(["/bin/ps", "-o", "stat=", "-p", str(pid)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2)
        self.assertTrue(not result.stdout.strip() or result.stdout.strip().startswith("Z"), result.stdout)

    def test_invalid_output_bytes_remain_safe(self):
        cmd = _script_cmd(self.root, "bytes.py", "import sys\nsys.stderr.buffer.write(b'bad\\xff')\nsys.exit(2)\n")
        result = HookEngine([Hook("PreToolUse", cmd)], self.root).fire("PreToolUse", {})
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason, "bad�")

    def test_truncated_blank_stderr_still_falls_back_to_stdout(self):
        cmd = _script_cmd(self.root, "blank.py",
            "import sys\nsys.stderr.write('　' * 400000)\n"
            "sys.stdout.write('真正的拦截理由')\nsys.exit(2)\n")
        result = HookEngine([Hook("PreToolUse", cmd)], self.root).fire("PreToolUse", {})
        self.assertTrue(result.blocked)
        self.assertEqual(result.reason, "真正的拦截理由")
