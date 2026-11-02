"""正式内置扩展：补丁、PDF、交互输入的真实行为及权限回归。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import background, documents, media, patches
from xiaoyu.config import Config
from xiaoyu.permissions import Permissions, parse_rule
from xiaoyu.tools import Toolbox
from .test_agent_paths import AgentTestCase


def change(path, diff=None, operation="update"):
    item = {"path": path, "operation": operation}
    if diff is not None:
        item["diff"] = diff
    return item


class PatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.box = Toolbox(Config(base_url="http://unused", model="m", workspace=self.root,
                                  enable_plugins=False, enable_mcp=False, sandbox=False))
        self.addCleanup(self.box.tasks.shutdown)

    def file(self, path, text="old\n"):
        target = self.root / path
        target.write_bytes(text.encode("utf-8"))
        self.box.run("read_file", {"path": path})
        return target

    def patch(self, *items):
        return self.box.run("apply_patch", {"changes": list(items)})

    def test_create_update_delete_and_rewind(self):
        first = self.file("a")
        removed = self.file("b")
        self.box.rewind.begin("修改三份文件")
        self.box.artifacts.begin()
        result = self.patch(change("a", "@@ -1 +1 @@\n-old\n+new\n"),
                            change("b", operation="delete"),
                            change("c", "@@ -0,0 +1 @@\n+created\n", "create"))
        self.assertNotIn("ERROR", result)
        self.assertEqual(first.read_text(), "new\n")
        self.assertFalse(removed.exists())
        self.assertEqual((self.root / "c").read_text(), "created\n")
        self.box.rewind.finish()
        point = self.box.rewind.points()[0]
        self.assertEqual(point.files[str(removed)], b"old\n")
        self.assertIsNone(point.after[str(removed)])

    def test_delete_replays_and_restores_from_journal(self):
        from xiaoyu.rewind import CheckpointReplay

        target = self.file("a")
        records = []
        self.box.rewind.journal = lambda event, **fields: records.append({"event": event, **fields})
        self.box.rewind.begin("删除")
        self.assertNotIn("ERROR", self.patch(change("a", operation="delete")))
        self.box.rewind.finish()
        replay = CheckpointReplay()
        replay.accept({"event": "meta", "workspace": str(self.root)})
        for record in records:
            replay.accept(record)
        store = replay.finish()
        self.assertFalse(store.problem, store.problem)
        self.assertEqual(store.conflicts(1), [])
        ok, message = store.rewind_files(1)
        self.assertTrue(ok, message)
        self.assertEqual(target.read_bytes(), b"old\n")

    def test_serialized_changes_cannot_bypass_path_checks(self):
        args = {"changes": json.dumps([change("a", "@@ -0,0 +1 @@\n+x\n", "create")])}
        self.assertIn("ERROR", self.box.run("apply_patch", args))
        self.assertFalse((self.root / "a").exists())
        args = {"changes": [json.dumps(change("a", "@@ -0,0 +1 @@\n+x\n", "create"))]}
        self.assertIn("ERROR", self.box.run("apply_patch", args))
        self.assertFalse((self.root / "a").exists())

    def test_late_bad_hunk_writes_nothing(self):
        first = self.file("a")
        self.file("b")
        result = self.patch(change("a", "@@ -1 +1 @@\n-old\n+new\n"),
                            change("b", "@@ -1 +1 @@\n-wrong\n+new\n"))
        self.assertIn("ERROR", result)
        self.assertEqual(first.read_text(), "old\n")

    def test_unread_stale_and_duplicate_targets(self):
        target = self.root / "a"
        target.write_text("old\n")
        item = change("a", "@@ -1 +1 @@\n-old\n+new\n")
        self.assertIn("还没读过", self.patch(item))
        self.box.run("read_file", {"path": "a"})
        target.write_text("external\n")
        self.assertIn("被改动", self.patch(item))
        self.box.run("read_file", {"path": "a"})
        self.assertIn("重复目标", self.patch(change("a", operation="delete"), change("./a", operation="delete")))

    def test_create_never_overwrites_and_delete_needs_whole_read(self):
        target = self.file("a", "one\ntwo\n")
        self.assertIn("已存在", self.patch(change("a", "@@ -0,0 +1 @@\n+new\n", "create")))
        self.box._reads.clear()
        self.box.run("read_file", {"path": "a", "offset": 1, "limit": 1})
        self.assertIn("只读过一部分", self.patch(change("a", operation="delete")))
        self.assertTrue(target.exists())

    # 固定解码候选，避免西文 Windows 的 cp1252 抢先解开 GBK 字节。
    @mock.patch("xiaoyu.tools.locale.getpreferredencoding", return_value="UTF-8")
    def test_encoding_and_crlf_preserved(self, _preferred):
        target = self.root / "a"
        target.write_bytes("旧\r\n".encode("gbk"))
        self.box.run("read_file", {"path": "a"})
        result = self.patch(change("a", "@@ -1 +1 @@\n-旧\n+新\n"))
        self.assertNotIn("ERROR", result)
        self.assertEqual(target.read_bytes(), "新\r\n".encode("gbk"))

    def test_partial_write_failure_reports_completed_and_pending(self):
        for name in "abc":
            self.file(name)
        original = __import__('xiaoyu.tools', fromlist=['_write_edit'])._write_edit
        def write(target, *args):
            if target.name == "b":
                raise OSError("disk full")
            return original(target, *args)
        with mock.patch("xiaoyu.tools._write_edit", side_effect=write):
            result = self.patch(*(change(n, "@@ -1 +1 @@\n-old\n+new\n") for n in "abc"))
        self.assertIn("已完成：update a", result)
        self.assertIn("未执行：c", result)
        self.assertEqual((self.root / "c").read_text(), "old\n")

    def test_multi_target_path_guard_and_rules(self):
        args = {"changes": [change("a", operation="delete"), change("../outside", operation="delete")]}
        self.assertTrue(self.box.outside_workspace(args))
        self.assertIsNotNone(self.box.guarded_config("apply_patch", {"changes": [change(".env", operation="delete")]}))
        permissions = Permissions(self.root)
        permissions.rules = [parse_rule("allow apply_patch(a)"), parse_rule(f"deny apply_patch({self.root.parent / 'outside'})")]
        self.assertEqual(permissions.explain("apply_patch", args)[0], "deny")
        permissions.rules = [parse_rule("allow apply_patch(a)")]
        self.assertEqual(permissions.explain("apply_patch", args)[0], "ask")
        permissions.rules.append(parse_rule(f"allow apply_patch({self.root.parent / 'outside'})"))
        self.assertEqual(permissions.explain("apply_patch", args)[0], "allow")

    def test_restricted_patch_tool_still_guards_configuration(self):
        box = Toolbox(self.box.config, only=["apply_patch"])
        self.assertIsNotNone(box.guarded_config("apply_patch", {"changes": [change(".env", operation="delete")]}))

    def test_unrelated_changes_argument_does_not_hide_path(self):
        args = {"path": "../outside", "changes": []}
        self.assertTrue(self.box.outside_workspace(args))
        self.assertEqual(self.box.resolved_target(args), self.root.parent / "outside")

    @unittest.skipIf(os.name == "nt", "符号链接")
    def test_second_target_symlink_movement(self):
        self.file("a")
        self.file("b")
        link = self.root / "link"
        link.symlink_to("a")
        args = {"changes": [change("b", operation="delete"), change("link", operation="delete")]}
        reviewed = self.box.resolved_target(args)
        link.unlink()
        link.symlink_to("b")
        self.assertIn("确认之后", self.box.target_moved(args, reviewed))

    def test_diff_multi_hunks_insert_and_no_final_newline(self):
        self.assertEqual(patches.apply_diff("a\nb\nc\n", "@@ -1 +1 @@\n-a\n+A\n@@ -3 +3,2 @@\n c\n+d\n"), "A\nb\nc\nd\n")
        self.assertEqual(patches.apply_diff("old", "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new\n\\ No newline at end of file\n"), "new")
        self.assertEqual(patches.apply_diff("a\n", "@@ -0,0 +1 @@\n+first\n"), "first\na\n")
        for diff in ("@@ -1,2 +1 @@\n-a\n+b\n", "@@ -1 +9 @@\n-a\n+b\n", "nonsense"):
            with self.assertRaises(ValueError):
                patches.apply_diff("a\n", diff)


class InputTest(unittest.TestCase):
    def setUp(self):
        self.manager = background.TaskManager()
        self.addCleanup(self.manager.shutdown)

    def start(self, code, **kwargs):
        task = self.manager.start([sys.executable, "-u", "-c", code], command="python",
                                  popen_extra={"start_new_session": True} if os.name != "nt" else {}, **kwargs)
        self.assertNotIsInstance(task, str, task)
        return task

    def test_pipe_roundtrip_eof_and_completed_rejection(self):
        task = self.start("import sys; print(sys.stdin.read().upper())", interactive=True)
        self.assertNotIn("ERROR", self.manager.send_input(task.task_id, "hello\n", eof=True))
        self.assertTrue(task.done.wait(5))
        self.assertIn("HELLO", self.manager.output_of(task))
        self.assertIn("已经结束", self.manager.send_input(task.task_id, "again"))

    def test_noninteractive_unknown_and_input_limit(self):
        task = self.start("pass")
        self.assertIn("未设置", self.manager.send_input(task.task_id, "x"))
        self.assertIn("未知任务", self.manager.send_input("task-999", "x"))
        interactive = self.start("import sys; sys.stdin.read()", interactive=True)
        self.assertIn("4096", self.manager.send_input(interactive.task_id, "中" * 2000))
        self.manager.send_input(interactive.task_id, eof=True)
        self.assertTrue(interactive.done.wait(5))

    @unittest.skipIf(os.name == "nt", "POSIX 伪终端")
    def test_pty_roundtrip_and_shutdown(self):
        task = self.start("import sys; print('TTY', sys.stdin.isatty()); print('READ', input())", interactive=True, tty=True)
        self.assertNotIn("ERROR", self.manager.send_input(task.task_id, "hello\n"))
        self.assertTrue(task.done.wait(5))
        output = self.manager.output_of(task)
        self.assertIn("TTY True", output)
        self.assertIn("READ hello", output)
        self.assertIsNone(task.input_fd)
        self.assertFalse(task.output_thread.is_alive())

    @unittest.skipIf(os.name == "nt", "POSIX 中断信号")
    def test_interrupt_reaches_process_and_reaps(self):
        task = self.start("import time; print('READY', flush=True); time.sleep(20)", interactive=True, tty=True)
        import time

        deadline = time.monotonic() + 5
        while "READY" not in self.manager.output_of(task) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("READY", self.manager.output_of(task))
        self.assertIn("中断信号", self.manager.send_input(task.task_id, "\x03"))
        self.assertTrue(task.done.wait(5))
        self.assertNotEqual(task.exit_code, 0)

    @unittest.skipIf(os.name == "nt", "POSIX shell 命令")
    def test_toolbox_interactive_bash_and_input_roundtrip(self):
        import shlex

        with tempfile.TemporaryDirectory() as tmp:
            box = Toolbox(Config(base_url="http://unused", model="m", workspace=Path(tmp).resolve(),
                                 enable_plugins=False, enable_mcp=False, sandbox=False))
            self.addCleanup(box.tasks.shutdown)
            command = shlex.quote(sys.executable) + " -u -c " + shlex.quote("print('ROUND', input())")
            result = box.run("bash", {"command": command, "interactive": True, "tty": True})
            self.assertIn("后台任务已启动", result)
            task = box.tasks.all()[0]
            self.assertTrue(box.get("task_input").available())
            result = box.run("task_input", {"task_id": task.task_id, "text": "trip\n"})
            self.assertNotIn("ERROR", result)
            self.assertTrue(task.done.wait(5))
            self.assertIn("ROUND trip", box.run("task_output", {"task_ids": [task.task_id]}))
            self.assertFalse(box.get("task_input").available())

    def test_input_backpressure_is_bounded(self):
        task = self.start("import time; time.sleep(20)", interactive=True)
        # 显式填满管道；一旦堵塞就终止，不让输入工具无限挂住。
        for _ in range(100):
            result = self.manager.send_input(task.task_id, "x" * 4096)
            if "ERROR" in result:
                break
        self.assertIn("ERROR", result)
        self.assertTrue(task.done.wait(5))


class ApprovalTest(AgentTestCase):
    def execute(self, agent, name, args):
        with contextlib.redirect_stdout(io.StringIO()):
            return agent._execute({"id": "c", "function": {"name": name, "arguments": json.dumps(args)}})["content"]

    def test_interactive_launch_asks_even_with_auto_and_allow(self):
        self.config.mode = "auto"
        asked = mock.Mock(return_value=False)
        agent = self.build([], approver=asked)
        agent.permissions.rules.append(parse_rule("allow bash(echo *)"))
        result = self.execute(agent, "bash", {"command": "echo test", "interactive": True})
        self.assertIn("拒绝", result)
        asked.assert_called_once()

    def test_input_asks_even_with_tool_allow_and_bash_deny_wins(self):
        self.config.sandbox = False
        asked = mock.Mock(return_value=False)
        agent = self.build([], approver=asked)
        self.addCleanup(agent.toolbox.tasks.shutdown)
        task = agent.toolbox.tasks.start([sys.executable, "-c", "import sys; sys.stdin.read()"], command="python", interactive=True)
        agent.permissions.rules.append(parse_rule("allow task_input"))
        self.assertIn("拒绝", self.execute(agent, "task_input", {"task_id": task.task_id, "text": "hi\n"}))
        asked.assert_called_once()
        agent.permissions.rules.append(parse_rule("deny bash(curl *)"))
        asked.reset_mock()
        self.assertIn("deny", self.execute(agent, "task_input", {"task_id": task.task_id, "text": "curl example.org\n"}))
        asked.assert_not_called()

    def test_patch_config_and_second_target_deny(self):
        asked = mock.Mock(return_value=False)
        agent = self.build([], approver=asked)
        args = {"changes": [change("a", "@@ -0,0 +1 @@\n+x\n", "create"),
                            change(".env", "@@ -0,0 +1 @@\n+x\n", "create")]}
        self.assertIn("拒绝", self.execute(agent, "apply_patch", args))
        asked.assert_called_once()
        agent.permissions.rules.append(parse_rule("deny apply_patch(.env)"))
        self.assertIn("deny", self.execute(agent, "apply_patch", args))
        self.assertFalse((self.root / "a").exists())


# 手写的两页最小 PDF；不需要生成库或网络，真实文本与渲染共用它。
def make_pdf(path):
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] /Resources << /Font << /F1 7 0 R >> >> /Contents 4 0 R >>',
               None,
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 6 0 R >>',
               b'<< /Length 0 >>\nstream\n\nendstream',
               b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    stream = b'BT /F1 18 Tf 10 50 Td (Hello PDF) Tj ET'
    objects[3] = b'<< /Length %d >>\nstream\n' % len(stream) + stream + b'\nendstream'
    data = bytearray(b'%PDF-1.4\n')
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f'{i} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(data)
    data.extend(f'xref\n0 {len(objects)+1}\n0000000000 65535 f \n'.encode())
    for offset in offsets:
        data.extend(f'{offset:010} 00000 n \n'.encode())
    data.extend(f'trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    path.write_bytes(data)


class DocumentTest(unittest.TestCase):
    def test_page_ranges_and_caps(self):
        self.assertEqual(documents.page_numbers("1-3,2,5", 5, 4), [1, 2, 3, 5])
        for spec in ("0", "6", "3-1", "1-99999999", "", "one", "1,2,3,4,5"):
            with self.assertRaises(ValueError):
                documents.page_numbers(spec, 5, 4)

    def test_missing_dependency_is_actionable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pdf"
            make_pdf(path)
            with mock.patch.dict(sys.modules, {"pypdfium2": None}):
                text, images = documents.read_pdf(path)
            self.assertIn("xiaoyu-agent[pdf]", text)
            self.assertFalse(images)

    @unittest.skipUnless(importlib.util.find_spec("pypdfium2"), "需 PDF extra")
    def test_document_tool_returns_cached_images_and_checks_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = root / "a.pdf"
            make_pdf(path)
            box = Toolbox(Config(base_url="http://unused", model="m", workspace=root,
                                 enable_plugins=False, enable_mcp=False))
            with mock.patch.object(media, "cache_dir", return_value=root / "media"):
                result = box.run("read_document", {"path": "a.pdf", "mode": "image"})
            self.assertNotIn("ERROR", result)
            parts = box.take_media()
            self.assertEqual(len(parts), 1)
            self.assertTrue(parts[0]["image_url"]["url"].startswith(media.SCHEME))
            self.assertFalse(box.needs_approval("read_document", {"path": "a.pdf"}))
            self.assertTrue(box.needs_approval("read_document", {"path": "../a.pdf"}))

    @unittest.skipUnless(importlib.util.find_spec("pypdfium2"), "需 PDF extra")
    def test_real_pdf_text_empty_page_and_rgb_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pdf"
            make_pdf(path)
            text, images = documents.read_pdf(path, "1-2")
            self.assertIn("Hello PDF", text)
            self.assertIn("第 2 页", text)
            self.assertIn("无可提取文字", text)
            self.assertFalse(images)
            text, images = documents.read_pdf(path, "1-2", "image")
            self.assertEqual(len(images), 2)
            self.assertEqual(media.dimensions(images[0]), (400, 200))
            self.assertEqual(media.dimensions(images[1]), (200, 200))
            with self.assertRaises(ValueError):
                documents.read_pdf(path, "3")
