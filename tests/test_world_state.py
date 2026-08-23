"""环境差量播报：变了才说、一步一条、恢复会话先全量说一次。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from xiaoyu import modes, session_log, world_state
from xiaoyu.agents import distill_history

from tests.test_agent_paths import AgentTestCase, chunk, usage_chunk
from tests.test_new_context import RecordingLog


def text_turn(text: str = "ok") -> list:
    return [chunk(content=text), usage_chunk(100, 10)]


def notes(agent) -> list[str]:
    return [
        m["content"]
        for m in agent.messages
        if m.get("role") == "user" and world_state.is_world_state_note(m.get("content"))
    ]


class WorldStateTest(AgentTestCase):
    def test_unchanged_environment_is_silent(self) -> None:
        agent = self.build([text_turn(), text_turn()])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.send("二")
        self.assertEqual(notes(agent), [])

    def test_model_switch_reports_only_model(self) -> None:
        agent = self.build([text_turn(), text_turn()])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.switch_model("other-model")
            agent.send("二")
        found = notes(agent)
        self.assertEqual(len(found), 1)
        self.assertIn("当前模型：other-model", found[0])
        self.assertNotIn("档位", found[0])
        self.assertNotIn("工作目录", found[0])
        #  播报排在本轮用户输入之后、模型回复之前
        roles = [m["role"] for m in agent.messages]
        idx = [i for i, m in enumerate(agent.messages) if m["content"] == found[0]][0]
        self.assertEqual(roles[idx - 1 : idx + 2], ["user", "user", "assistant"])

    def test_two_changes_one_note(self) -> None:
        agent = self.build([text_turn(), text_turn()])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.switch_model("other-model")
            agent.set_mode(modes.AUTO)
            agent.send("二")
        found = notes(agent)
        self.assertEqual(len(found), 1)
        self.assertIn("当前模型：other-model", found[0])
        self.assertIn("档位", found[0])

    def test_unknown_baseline_reports_full_block_once(self) -> None:
        agent = self.build([text_turn(), text_turn()])
        agent.world_state.baseline = None
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.send("二")
        found = notes(agent)
        self.assertEqual(len(found), 1)
        self.assertIn("恢复会话后", found[0])
        self.assertIn("当前模型：main-model", found[0])
        self.assertIn("当前工作目录", found[0])
        self.assertIn("工具：", found[0])
        self.assertNotIn("上下文窗口", found[0])

    def test_note_is_skipped_by_compaction_and_distill(self) -> None:
        agent = self.build([text_turn(), text_turn()])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.switch_model("other-model")
            agent.send("二")
        note = notes(agent)[0]
        self.assertIn(note, agent.compactor.synthetic_user_texts)
        starts = session_log.turn_starts(agent.messages, agent.compactor.synthetic_user_texts)
        self.assertEqual([agent.messages[i]["content"] for i in starts], ["一", "二"])
        distilled = distill_history(agent.messages, max_tokens=10_000, synthetic_texts=frozenset())
        self.assertFalse(any(world_state.is_world_state_note(m["content"]) for m in distilled))

    def test_baseline_logged_and_restored(self) -> None:
        log = RecordingLog()
        agent = self.build([text_turn(), text_turn()], session_log=log)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.switch_model("other-model")
            agent.send("二")
        baselines = [fields["baseline"] for kind, fields in log.events if kind == "world_state"]
        self.assertEqual(baselines[-1]["model"], {"value": "other-model"})
        #  临时字段不落盘
        self.assertNotIn("_text", baselines[-1]["project_instructions"])

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            lines = [json.dumps({"event": "meta", "format": 2})]
            lines += [
                json.dumps({"event": "world_state", "baseline": b}, ensure_ascii=False)
                for b in baselines
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.assertEqual(session_log.last_world_state(path), baselines[-1])
            self.assertIsNone(session_log.last_world_state(Path(tmp) / "missing.jsonl"))

            #  restore 接上基线：模型与日志一致则下一步不播报
            fresh = self.build([text_turn()])
            fresh.switch_model("other-model")
            fresh.restore([{"role": "user", "content": "旧"}, {"role": "assistant", "content": "旧答"}], source=str(path))
            self.assertEqual(fresh.world_state.baseline, baselines[-1])
            with contextlib.redirect_stdout(io.StringIO()):
                fresh.send("三")
            self.assertEqual(notes(fresh), [])

    def test_restore_without_log_marks_unknown(self) -> None:
        agent = self.build([text_turn()])
        agent.restore([{"role": "user", "content": "旧"}, {"role": "assistant", "content": "旧答"}])
        self.assertIsNone(agent.world_state.baseline)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("三")
        self.assertEqual(len(notes(agent)), 1)


class SectionUnitTest(unittest.TestCase):
    def test_names_section_diff(self) -> None:
        section = world_state.NamesSection("tools", lambda a: [], "工具")
        self.assertIsNone(section.render({"names": ["a", "b"]}, {"names": ["a", "b"]}))
        text = section.render({"names": ["a", "b"]}, {"names": ["a", "c"]})
        self.assertIn("新增 c", text)
        self.assertIn("移除 b", text)
        self.assertIn("当前可用工具：a、b", section.render(None, {"names": ["a", "b"]}))

    def test_project_instructions_section(self) -> None:
        section = world_state.ProjectInstructionsSection()
        self.assertIsNone(section.render(None, {"digest": "x", "_text": "t"}))
        self.assertIsNone(section.render({"digest": "x"}, {"digest": "x", "_text": "t"}))
        self.assertIn("新版", section.render({"digest": "x"}, {"digest": "y", "_text": "规则 A"}))
        self.assertIn("已移除", section.render({"digest": "x"}, {"digest": "", "_text": ""}))
        long = "很长" * world_state.PROJECT_TEXT_CAP
        self.assertIn("未随本条附带", section.render({"digest": "x"}, {"digest": "y", "_text": long}))

    def test_render_cap(self) -> None:
        ws = world_state.WorldState(sections=[])
        ws.sections = [
            world_state.ValueSection("big", lambda a: "x" * 5000, lambda v: v),
        ]
        ws.baseline = {"big": {"value": "y"}}
        text = ws.diff(None)  # type: ignore[arg-type]
        self.assertLessEqual(len(text), world_state.RENDER_CAP)
        self.assertTrue(text.endswith(world_state.TAG_CLOSE))


if __name__ == "__main__":
    unittest.main()
