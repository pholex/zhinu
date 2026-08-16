"""rewind 快照的测试：首写获胜、恢复/删除、冲突检测、对话截断、resume 重放。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu import rewind as rw
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = rw.RewindStore()

    def path(self, name: str, content: str | None = None) -> Path:
        target = self.root / name
        if content is not None:
            target.write_text(content, encoding="utf-8")
        return target

    def test_first_write_wins_within_turn(self):
        target = self.path("a.txt", "v0")
        self.store.begin("改 a")
        self.store.record(target, "v0")
        target.write_text("v1", encoding="utf-8")
        self.store.record(target, "v1")  # 同轮第二次：忽略
        target.write_text("v2", encoding="utf-8")
        self.store.finish()
        ok, _ = self.store.rewind_files(1)
        self.assertTrue(ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "v0")

    def test_restore_across_turns_and_delete_created(self):
        existing = self.path("keep.txt", "old")
        created = self.root / "new.txt"
        self.store.begin("第一轮")
        self.store.record(existing, "old")
        existing.write_text("turn1", encoding="utf-8")
        self.store.finish()
        self.store.begin("第二轮")
        self.store.record(created, None)
        created.write_text("hello", encoding="utf-8")
        self.store.record(existing, "turn1")
        existing.write_text("turn2", encoding="utf-8")
        self.store.finish()
        #  回到第 1 轮前：existing 取"最早"的 before（old），created 删除
        ok, summary = self.store.rewind_files(1)
        self.assertTrue(ok, summary)
        self.assertEqual(existing.read_text(encoding="utf-8"), "old")
        self.assertFalse(created.exists())
        self.assertEqual(self.store.points(), [])

    def test_partial_rewind_keeps_earlier_points(self):
        target = self.path("a.txt", "v0")
        for turn in ("一", "二"):
            self.store.begin(turn)
            before = target.read_text(encoding="utf-8")
            self.store.record(target, before)
            target.write_text(f"after-{turn}", encoding="utf-8")
            self.store.finish()
        ok, _ = self.store.rewind_files(2)
        self.assertTrue(ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "after-一")
        self.assertEqual([p.index for p in self.store.points()], [1])

    def test_conflict_detection_on_external_edit(self):
        target = self.path("a.txt", "v0")
        self.store.begin("改")
        self.store.record(target, "v0")
        target.write_text("v1", encoding="utf-8")
        self.store.finish()
        self.assertEqual(self.store.conflicts(1), [])
        target.write_text("外部改动", encoding="utf-8")
        self.assertEqual(self.store.conflicts(1), [str(target)])

    def test_oversized_file_skipped(self):
        target = self.path("big.txt", "x")
        self.store.begin("大文件")
        self.store.record(target, "y" * (rw.MAX_FILE_BYTES + 1))
        self.store.finish()
        self.assertEqual(self.store.points()[0].files, {})
        self.assertEqual(self.store.skipped_from(1), [str(target)])

    def test_record_outside_turn_is_noop(self):
        self.store.record(self.path("a.txt", "v0"), "v0")
        self.assertEqual(self.store.points(), [])

    def test_cap_evicts_oldest(self):
        for turn in range(rw.MAX_POINTS + 5):
            self.store.begin(str(turn))
            self.store.finish()
        points = self.store.points()
        self.assertEqual(len(points), rw.MAX_POINTS)
        self.assertEqual(points[0].index, 6)


class ToolboxCaptureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        config = Config(
            base_url="x", model="x", workspace=self.root,
            enable_plugins=False, enable_mcp=False,
        )
        self.box = Toolbox(config)

    def test_write_and_replace_capture_before_content(self):
        self.box.rewind.begin("一轮")
        self.box.run("write_file", {"path": "a.txt", "content": "v1\n"})
        self.box.run("read_file", {"path": "a.txt"})
        self.box.run("str_replace", {"path": "a.txt", "old_str": "v1", "new_str": "v2"})
        self.box.rewind.finish()
        point = self.box.rewind.points()[0]
        #  新建文件的 before 是 None（首写获胜：str_replace 不覆盖它）
        self.assertEqual(point.files, {str(self.root / "a.txt"): None})
        ok, _ = self.box.rewind.rewind_files(1)
        self.assertTrue(ok)
        self.assertFalse((self.root / "a.txt").exists())

    def test_replace_existing_captures_original(self):
        target = self.root / "b.txt"
        target.write_text("原文\n", encoding="utf-8")
        self.box.rewind.begin("一轮")
        self.box.run("read_file", {"path": "b.txt"})
        self.box.run("str_replace", {"path": "b.txt", "old_str": "原文", "new_str": "新文"})
        self.box.rewind.finish()
        ok, _ = self.box.rewind.rewind_files(1)
        self.assertTrue(ok)
        self.assertEqual(target.read_text(encoding="utf-8"), "原文\n")


class AgentRewindTest(unittest.TestCase):
    """不跑真模型：手工搭历史 + 快照点，验证 rewind_to 的截断与匹配语义。"""

    def make_agent(self):
        import tempfile as tf

        from xiaoyu.agent import Agent
        from xiaoyu.providers import Provider, Registry

        self.tmp = tf.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Config(
            base_url="http://localhost:9", model="deepseek-v4-pro",
            workspace=Path(self.tmp.name).resolve(),
            enable_plugins=False, enable_mcp=False, enable_skills=False,
            enable_explore=False, enable_web_search=False,
            enable_agents=False, enable_hooks=False,
        )
        registry = Registry([Provider(name="g", base_url="http://localhost:9", api_key="k")])
        return Agent(config, registry=registry)

    def turn(self, agent, text: str) -> None:
        agent.toolbox.rewind.begin(text)
        agent.messages.append({"role": "user", "content": text})
        agent.messages.append({"role": "assistant", "content": f"回复 {text}"})
        agent.toolbox.rewind.finish()

    def test_conversation_truncated_to_point(self):
        agent = self.make_agent()
        self.turn(agent, "第一件事")
        self.turn(agent, "第二件事")
        self.turn(agent, "第三件事")
        result = agent.rewind_to(2, files=False)
        self.assertIn("回滚到第 2 轮", result)
        texts = [m.get("content") for m in agent.messages if m.get("role") == "user"]
        self.assertEqual(texts, ["第一件事"])

    def test_duplicate_prompts_match_by_occurrence(self):
        agent = self.make_agent()
        self.turn(agent, "再来一次")
        self.turn(agent, "再来一次")
        agent.rewind_to(2, files=False)
        texts = [m.get("content") for m in agent.messages if m.get("role") == "user"]
        self.assertEqual(texts, ["再来一次"])

    def test_compacted_turn_falls_back_to_files_only(self):
        agent = self.make_agent()
        self.turn(agent, "会被压缩掉的轮次")
        #  模拟压缩改写：用户原话从历史里消失
        agent.messages = [agent.messages[0], {"role": "user", "content": "[摘要]"}]
        result = agent.rewind_to(1, conversation=True, files=True)
        self.assertIn("已被压缩合并", result)

    def test_unknown_point(self):
        agent = self.make_agent()
        self.assertIn("没有编号为 9", agent.rewind_to(9))


class ReplayTest(unittest.TestCase):
    def test_load_messages_applies_rewind_replacement(self):
        import json

        from xiaoyu.session_log import load_messages

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            lines = [
                {"event": "meta", "format": 2},
                {"role": "user", "content": "一"},
                {"role": "assistant", "content": "答一"},
                {"role": "user", "content": "二"},
                {"event": "rewind", "target": 2,
                 "replacement": [{"role": "user", "content": "一"},
                                 {"role": "assistant", "content": "答一"}]},
                {"role": "user", "content": "新的二"},
            ]
            path.write_text(
                "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
                encoding="utf-8",
            )
            messages = load_messages(path)
            self.assertEqual(
                [m["content"] for m in messages], ["一", "答一", "新的二"]
            )


if __name__ == "__main__":
    unittest.main()
