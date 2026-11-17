"""session fork（按轮截断分叉）的测试。不打网络。

turn_starts 是纯函数直接测；CLI 的 --turns / --fork 用真子进程 + 手造会话
文件 + scripted 桩黑盒验证（复用 test_e2e_scripted 的隔离环境）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

from xiaoyu.agent import EMPTY_REPLY_NUDGE, SYNTHETIC_USER_TEXTS
from xiaoyu.session_log import _workspace_slug, turn_starts

from .test_e2e_scripted import E2ECase


class TurnStartsTest(unittest.TestCase):
    def test_user_messages_are_turn_starts(self):
        messages = [
            {"role": "user", "content": "轮一"},
            {"role": "assistant", "content": "答一"},
            {"role": "user", "content": "轮二"},
            {"role": "assistant", "content": None, "tool_calls": [{}]},
            {"role": "tool", "content": "结果"},
            {"role": "assistant", "content": "答二"},
        ]
        self.assertEqual(turn_starts(messages), [0, 2])

    def test_synthetic_and_blank_user_messages_excluded(self):
        messages = [
            {"role": "user", "content": "真轮次"},
            {"role": "user", "content": EMPTY_REPLY_NUDGE},
            {"role": "user", "content": "   "},
            {"role": "user", "content": "又一轮"},
        ]
        self.assertEqual(turn_starts(messages, SYNTHETIC_USER_TEXTS), [0, 3])


class ForkE2ETest(E2ECase):
    """手造两轮会话文件 → resume --turns 列轮次 → --fork 1 只带前一轮分叉。"""

    def _plant_session(self) -> Path:
        sessions = (
            Path(self.tmp) / "config" / "xiaoyu" / "sessions"
            / _workspace_slug(str(self.workspace))
        )
        sessions.mkdir(parents=True)
        path = sessions / "20260809-000000-1.jsonl"
        records = [
            {"event": "meta", "format": 2, "model": "m", "workspace": str(self.workspace),
             "started_at": "2026-08-09T00:00:00"},
            {"role": "user", "content": "第一轮：查清结构"},
            {"role": "assistant", "content": "结构查清了"},
            {"role": "user", "content": "第二轮：动手改"},
            {"role": "assistant", "content": "改完了"},
        ]
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
        return path

    def _run_resume(self, script: str, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "xiaoyu", "resume", *argv],
            #  不继承跑测试那一方的 stdin：resume 在 stdin 不是终端时会把它整个
            #  读完当作追加输入，继承到一个永不关闭的管道就一直等到超时
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=self.scripted_env(script),
            cwd=self.workspace,
        )

    def test_turns_lists_fork_points(self):
        self._plant_session()
        proc = self._run_resume("text: 不会用到\n", ["--last", "--turns"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("第一轮：查清结构", proc.stdout)
        self.assertIn("第二轮：动手改", proc.stdout)
        self.assertIn("--fork", proc.stdout)

    def test_fork_keeps_only_first_turn(self):
        self._plant_session()
        proc = self._run_resume(
            "text: 分叉后继续\n",
            ["--last", "--fork", "1", "--output-format", "stream-json", "接着第一轮做"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(result["kind"], "result")
        self.assertEqual(result["result"], "分叉后继续")
        #  新会话文件只带第一轮 + 新指令；第二轮没有进来；原文件不动
        forked = Path(result["session_log"]).read_text(encoding="utf-8")
        self.assertIn("第一轮：查清结构", forked)
        self.assertNotIn("第二轮：动手改", forked)
        self.assertIn("接着第一轮做", forked)
        self.assertIn("resumed_from", forked)

    def test_fork_out_of_range_is_error(self):
        self._plant_session()
        proc = self._run_resume("text: x\n", ["--last", "--fork", "5", "指令", "--output-format", "text"])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("超出范围", proc.stderr)


class ResumeFollowsModelE2ETest(ForkE2ETest):
    """`xiaoyu resume` 跟随旧会话最后生效的模型（与 ACP session/load、term 同口径）；
    `--model` 显式给了才覆盖。"""

    def _plant_switched_session(self) -> Path:
        path = self._plant_session()
        with path.open("a", encoding="utf-8") as handle:
            #  /model 切换的留痕：最后写的说了算
            handle.write(json.dumps({"event": "model", "model": "backup-model"}) + "\n")
        return path

    def test_resume_follows_last_model_of_session(self):
        self._plant_switched_session()
        proc = self._run_resume("text: 接上了\n", ["--last", "继续", "--output-format", "stream-json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(result["model"], "backup-model")
        #  新会话文件的 meta 记的也是跟随后的模型，再次 resume 仍跟得上
        resumed = Path(result["session_log"]).read_text(encoding="utf-8")
        meta = json.loads(resumed.splitlines()[0])
        self.assertEqual(meta["model"], "backup-model")

    def test_explicit_model_flag_overrides_recorded_model(self):
        self._plant_switched_session()
        proc = self._run_resume(
            "text: 换了\n",
            ["--last", "--model", "other-model", "继续", "--output-format", "stream-json"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        self.assertEqual(result["model"], "other-model")


class ResumeByIdE2ETest(ForkE2ETest):
    """开场横幅给的会话 id（文件名）能直接点名接回，不被当成指令的第一个词。"""

    def test_resume_by_id_picks_that_session_not_latest(self):
        older = self._plant_session()
        newer = older.with_name("20260810-000000-2.jsonl")
        newer.write_text(older.read_text(encoding="utf-8").replace("第一轮", "新会话"), encoding="utf-8")
        proc = self._run_resume(
            "text: 接上了\n", [older.stem, "继续", "--output-format", "stream-json"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        resumed = Path(result["session_log"]).read_text(encoding="utf-8")
        #  resumed_from 指向点名的那份，不是最新的。按事件解析再比路径：JSON 里的
        #  Windows 路径反斜杠是转义过的，拿裸字符串去正文里找在那边必不中
        sources = [
            Path(record["source"])
            for record in map(json.loads, filter(str.strip, resumed.splitlines()))
            if record.get("event") == "resumed_from"
        ]
        self.assertEqual(sources, [older])
        self.assertIn("第一轮：查清结构", resumed)
        self.assertIn('"继续"', resumed)  # id 之后的词才是指令

    def test_unknown_id_is_error_not_prompt(self):
        self._plant_session()
        proc = self._run_resume("text: x\n", ["20990101-000000-9", "继续"])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("找不到会话", proc.stderr)


class ResumeByNameE2ETest(ForkE2ETest):
    """具名会话的名字（`--session-id`、终端集成的 `term-…`）也能点名接回。"""

    def _plant_term_session(self) -> Path:
        from xiaoyu.session_log import TERM_SUBDIR

        sessions = Path(self.tmp) / "config" / "xiaoyu" / "sessions" / TERM_SUBDIR
        sessions.mkdir(parents=True)
        path = sessions / "20260808-000000-7-id-term-ab12cd34.jsonl"
        records = [
            {"event": "meta", "format": 2, "model": "m", "workspace": str(self.workspace),
             "started_at": "2026-08-08T00:00:00", "session_id": "term-ab12cd34"},
            {"role": "user", "content": "终端里问的：为什么 make 挂了"},
            {"role": "assistant", "content": "因为少了依赖"},
        ]
        path.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
            encoding="utf-8",
        )
        return path

    def test_resume_by_term_name_picks_that_session_not_latest(self):
        term_log = self._plant_term_session()
        self._plant_session()  # 文件名更新的匿名会话：默认「最近一个」会是它
        proc = self._run_resume(
            "text: 接上了\n", ["term-ab12cd34", "继续", "--output-format", "stream-json"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        resumed = Path(result["session_log"]).read_text(encoding="utf-8")
        sources = [
            Path(record["source"])
            for record in map(json.loads, filter(str.strip, resumed.splitlines()))
            if record.get("event") == "resumed_from"
        ]
        self.assertEqual(sources, [term_log])
        self.assertIn("为什么 make 挂了", resumed)
        self.assertIn('"继续"', resumed)  # 名字之后的词才是指令
        #  接回写的是新文件，原 term 会话一个字节不动（终端里的 @x 仍续它）
        self.assertNotIn("继续", term_log.read_text(encoding="utf-8"))

    def test_unknown_term_name_is_error_not_prompt(self):
        self._plant_session()
        proc = self._run_resume("text: x\n", ["term-00000000", "继续"])
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("找不到会话", proc.stderr)

    def test_unknown_plain_word_stays_an_instruction(self):
        self._plant_session()
        proc = self._run_resume("text: 收到\n", ["fix", "the", "bug", "--output-format", "stream-json"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout.splitlines()[-1])
        self.assertIn("fix the bug", Path(result["session_log"]).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
