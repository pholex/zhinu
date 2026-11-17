"""图片、批量替换与事件等待的可见行为回归。"""

import base64
import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import media, patches
from xiaoyu.config import Config
from xiaoyu.permissions import Permissions, parse_rule
from xiaoyu.tools import Toolbox
from .test_agent_paths import AgentTestCase, chunk, call_fragment, usage_chunk

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWQAAAABJRU5ErkJggg==')


def edit(path, old, new):
    return {"path": path, "operation": "update", "edits": [{"old_str": old, "new_str": new}]}


class FileToolsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.box = Toolbox(Config(base_url="http://unused", model="m", workspace=self.root,
                                  enable_plugins=False, enable_mcp=False))
        self.addCleanup(self.box.tasks.shutdown)
        cache = mock.patch.object(media, "cache_dir", return_value=self.root / "cache")
        cache.start()
        self.addCleanup(cache.stop)

    def file(self, path, raw):
        target = self.root / path
        target.write_bytes(raw)
        self.box.run("read_file", {"path": path})
        return target

    # 与已有编码回归一致，显式测试 UTF-8 → GBK 的回退路径。
    @mock.patch("xiaoyu.tools.locale.getpreferredencoding", return_value="UTF-8")
    def test_batch_uses_original_and_preserves_encoding_rewind_artifacts(self, _preferred):
        target = self.file("a", "甲\r\n乙\r\n".encode("gbk"))
        other = self.file("b", b"old\n")
        self.box.rewind.begin("批量替换")
        self.box.artifacts.begin()
        changes = [edit("a", "甲", "乙"), edit("b", "old", "new")]
        changes[0]["edits"].append({"old_str": "乙", "new_str": "丙"})
        output = self.box.run("apply_patch", {"changes": changes})
        self.assertNotIn("ERROR", output)
        self.assertEqual(target.read_bytes(), "乙\r\n丙\r\n".encode("gbk"))
        self.assertEqual(other.read_bytes(), b"new\n")
        self.assertEqual(len(self.box.artifacts.finish(complete=True)), 2)
        self.box.rewind.finish()
        self.assertEqual(self.box.rewind.points()[0].files[str(target)], "甲\r\n乙\r\n".encode("gbk"))
        self.assertIn("原文：", patches.preview({"changes": changes}))

    def test_late_invalid_edit_does_not_modify_any_file(self):
        a = self.file("a", b"old\n")
        self.file("b", b"old\nold\n")
        output = self.box.run("apply_patch", {"changes": [edit("a", "old", "new"), edit("b", "old", "new")]})
        self.assertIn("ERROR", output)
        self.assertEqual(a.read_bytes(), b"old\n")

    def test_overlapping_and_ambiguous_edits_rejected(self):
        for text, edits in [
            ("aaa", [{"old_str": "aa", "new_str": "b"}]),
            ("abcd", [{"old_str": "abc", "new_str": "a"}, {"old_str": "cd", "new_str": "d"}]),
            ("  x", [{"old_str": "x", "new_str": "x\ny"}]),
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                patches.apply_edits(text, edits)

    def test_alternative_forms_are_exclusive_and_strict(self):
        target = self.file("a", b"old\n")
        for item in [dict(edit("a", "old", "new"), diff="@@ -1 +1 @@\n-old\n+new\n"),
                     dict(edit("a", "old", "new"), operation="create"),
                     dict(edit("a", "old", "new"), edits=[]),
                     dict(edit("a", "old", "new"), edits='[]')]:
            self.assertIn("ERROR", self.box.run("apply_patch", {"changes": [item]}))
        self.assertEqual(target.read_bytes(), b"old\n")

    def test_image_returns_cached_media_and_leaves_source(self):
        target = self.root / "image.png"
        target.write_bytes(PNG)
        with mock.patch.dict('sys.modules', {'PIL': None}):
            output = self.box.run("view_image", {"path": "image.png"})
        self.assertNotIn("ERROR", output)
        parts = self.box.take_media()
        self.assertEqual(len(parts), 1)
        self.assertEqual(media.path_of(parts[0]['image_url']['url']).read_bytes(), PNG)
        self.assertEqual(target.read_bytes(), PNG)
        self.assertTrue(self.box.needs_approval("view_image", {"path": str(self.root.parent / 'outside.png')}))

    def test_bad_image_and_missing_optional_dependency(self):
        (self.root / "bad.png").write_text("not an image")
        self.assertIn("ERROR", self.box.run("view_image", {"path": "bad.png"}))
        self.assertEqual(self.box.take_media(), [])
        with mock.patch.dict('sys.modules', {'PIL': None}):
            self.assertIn("xiaoyu-agent[image]", self.box.run("view_image", {"path": "bad.png", "max_size": 10}))
        for args in ({"crop": [0, 0, 0, 1]}, {"crop": [0, 1]}, {"max_size": 0}):
            self.assertIn("ERROR", self.box.run("view_image", {"path": "bad.png", **args}))

    def test_real_crop_and_scale(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("需可选图片依赖")
        target = self.root / 'image.png'
        with Image.new('RGB', (100, 80), 'red') as picture:
            picture.save(target)
        original = target.read_bytes()
        output = self.box.run('view_image', {'path': 'image.png', 'crop': [10, 20, 90, 60], 'max_size': 20})
        self.assertNotIn('ERROR', output)
        part, = self.box.take_media()
        self.assertEqual(media.dimensions(media.path_of(part['image_url']['url']).read_bytes()), (20, 10))
        self.assertEqual(target.read_bytes(), original)
        self.assertIn('ERROR', self.box.run('view_image', {'path': 'image.png', 'crop': [0, 0, 101, 80]}))


class WaitTest(AgentTestCase):
    def agent(self):
        self.config.enable_mcp = False
        agent = self.build([])
        self.addCleanup(agent.toolbox.tasks.shutdown)
        return agent

    def test_timeout_and_passive_notifications(self):
        agent = self.agent()
        agent.notify('公告', key='passive', wake=False)
        self.assertIn('超时', agent.toolbox.run('wait_for_event', {'timeout': 0}))
        self.assertEqual(len(agent.pending_notifications()), 1)
        for timeout in (-1, 601):
            self.assertIn('ERROR', agent.toolbox.run('wait_for_event', {'timeout': timeout}))

    def test_notification_wakes_and_delivers_once(self):
        agent = self.agent()
        started = threading.Event()
        result = []
        def wait():
            started.set()
            result.append(agent.toolbox.run('wait_for_event', {'timeout': 5}))
        worker = threading.Thread(target=wait)
        worker.start()
        self.addCleanup(worker.join, 6)
        self.assertTrue(started.wait(1))
        agent.notify('完成', key='task-1')
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertIn('事件', result[0])
        self.assertEqual(agent._drain_notifications(), ['完成'])
        agent.notify('完成', key='task-1')
        self.assertIn('超时', agent._wait_for_event(0))

    def test_interrupt_steer_and_upstream_cancel(self):
        for action, expected in [('interrupt', '中断'), ('steer', '插话'), ('upstream', '中断')]:
            with self.subTest(action=action):
                agent = self.agent()
                timer = threading.Timer(0.02, lambda: agent.steer('改一下') if action == 'steer'
                                        else setattr(agent, '_upstream_stop', lambda: True) if action == 'upstream'
                                        else agent.interrupt())
                timer.start()
                self.addCleanup(timer.join)
                start = time.monotonic()
                self.assertIn(expected, agent._wait_for_event(5))
                self.assertLess(time.monotonic() - start, 1)
                if action == 'steer':
                    self.assertEqual(agent.drain_steers(), ['改一下'])

    def test_peer_message_keeps_original_delivery_path(self):
        agent = self.agent()
        agent.peer = mock.Mock()
        agent.peer.drain.side_effect = [[('peer', '消息正文')], []]
        self.assertIn('协作', agent._wait_for_event(0))
        self.assertTrue(agent._consume_inbox(midturn=True))
        self.assertEqual(sum('消息正文' in str(m['content']) for m in agent.messages), 1)
        self.assertEqual(agent._wait_peer_messages, [])

    def test_wait_tool_in_full_turn_keeps_call_result_pairing(self):
        self.config.enable_mcp = False
        script = [[chunk(tool_calls=[call_fragment(0, 'wait1', 'wait_for_event', '{"timeout":0}')]), usage_chunk(10, 2)],
                  [chunk(content='完成'), usage_chunk(10, 2)]]
        agent = self.build(script)
        self.addCleanup(agent.toolbox.tasks.shutdown)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send('检查事件')
        results = [m for m in agent.messages if m['role'] == 'tool']
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['tool_call_id'], 'wait1')
        self.assertIn('超时', results[0]['content'])


class EditPermissionTest(AgentTestCase):
    def test_batch_deny_prevents_all_writes(self):
        self.config.enable_mcp = False
        (self.root / 'a').write_text('old')
        (self.root / 'b').write_text('old')
        args = {'changes': [edit('a', 'old', 'new'), edit('b', 'old', 'new')]}
        script = [[chunk(tool_calls=[call_fragment(0, 'edit1', 'apply_patch', json.dumps(args))]), usage_chunk(10, 2)],
                  [chunk(content='已拒绝'), usage_chunk(10, 2)]]
        agent = self.build(script, permissions=Permissions(self.root, [parse_rule('deny apply_patch(b)')]))
        self.addCleanup(agent.toolbox.tasks.shutdown)
        for path in ('a', 'b'):
            agent.toolbox.run('read_file', {'path': path})
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send('修改文件')
        self.assertEqual((self.root / 'a').read_text(), 'old')
        self.assertEqual((self.root / 'b').read_text(), 'old')
        self.assertTrue(any('deny apply_patch(b)' in str(m['content']) for m in agent.messages if m['role'] == 'tool'))
