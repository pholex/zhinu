"""读取结果须跨过模型请求边界，不能由同批执行顺序冒充已知内容。"""
import json

from .test_agent_paths import AgentTestCase, call_fragment, chunk
from .test_tools import ToolboxTestCase


class BatchReads(ToolboxTestCase):
    def test_new_read_cannot_authorize_same_batch_overwrite(self):
        with self.box.tool_batch():
            self.box.run("read_file", {"path": "calc.py"})
            result = self.box.run("write_file", {"path": "calc.py", "content": "guessed"})
            self.assertIn("尚未看到", result)
        self.assertNotEqual(self.read(), "guessed")
        with self.box.tool_batch():
            self.assertNotIn("ERROR", self.box.run("write_file", {"path": "calc.py", "content": "known"}))
        self.assertEqual(self.read(), "known")

    def test_previous_read_and_new_file_remain_editable(self):
        self.box.run("read_file", {"path": "calc.py"})
        with self.box.tool_batch():
            for name in ("calc.py", "new.py"):
                self.assertNotIn("ERROR", self.box.run("write_file", {"path": name, "content": "one"}))
                self.assertNotIn("ERROR", self.box.run("str_replace", {"path": name, "old_str": "one", "new_str": "two"}))

    def test_partial_read_cannot_be_promoted_within_batch(self):
        self.box.run("read_file", {"path": "calc.py", "offset": 1, "limit": 1})
        with self.box.tool_batch():
            self.box.run("read_file", {"path": "calc.py"})
            self.assertNotIn("ERROR", self.box.run("str_replace", {"path": "calc.py", "old_str": "def add", "new_str": "def sum_values"}))
            self.assertIn("只读过一部分", self.box.run("write_file", {"path": "calc.py", "content": "guessed"}))

    def test_opt_out_and_exception_cleanup(self):
        self.config.batch_read_guard = False
        with self.assertRaises(RuntimeError), self.box.tool_batch():
            self.box.run("read_file", {"path": "calc.py"})
            self.assertNotIn("ERROR", self.box.run("write_file", {"path": "calc.py", "content": "ok"}))
            raise RuntimeError("cancel")
        self.assertIsNone(self.box._batch_reads)


class BatchReadIntegration(AgentTestCase):
    def test_actual_model_batch_blocks_guess_then_accepts_next_response(self):
        def call(index, ident, name, args):
            return call_fragment(index, ident, name, json.dumps(args))
        agent = self.build([
            [chunk(tool_calls=[call(0, "r", "read_file", {"path": "calc.py"}),
                               call(1, "w", "write_file", {"path": "calc.py", "content": "guess"})])],
            [chunk(tool_calls=[call(0, "w2", "write_file", {"path": "calc.py", "content": "verified"})])],
            [chunk(content="完成")],
        ])
        agent.send("修改文件")
        outputs = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("尚未看到", outputs[1])
        self.assertEqual((self.root / "calc.py").read_text(), "verified")
