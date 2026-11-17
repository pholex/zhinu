"""宿主交付与文件快照在文件日志、数据库日志上的同一契约。"""
from __future__ import annotations

import json
import importlib.util
import unittest
from unittest.mock import patch

from tests import test_sdk_controls as support
from xiaoyu_agent_sdk import ArtifactsReady, RunCompleted, Session, SessionStorageError, SQLiteSessionStore, Tool, ToolResult


call, chunk, options = support.call, support.chunk, support.options


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class ArtifactIntegrationTests(unittest.TestCase):
    setUp = support.ControlTests.setUp
    def test_builtin_delivery_survives_result_and_event_serialization(self):
        config = options(self.workspace, [
            [call("write_file", {"path": "result.txt", "content": "first"})],
            [call("str_replace", {"path": "result.txt", "old_str": "first", "new_str": "final"})],
            [chunk("done")], [chunk("no files this time")],
        ], builtin_tools=("write_file", "str_replace"), approver=lambda *_: True)
        with Session(config) as session:
            events = list(session.stream("create a result"))
            result = next(e.result for e in events if isinstance(e, RunCompleted))
            item, = result.artifacts
            self.assertEqual((item.path, item.operation, item.size), ("result.txt", "created", 5))
            published = next(e for e in events if isinstance(e, ArtifactsReady))
            self.assertEqual(published.artifacts, result.artifacts)
            self.assertIn('"path": "result.txt"', json.dumps(published.to_dict()))
            self.assertEqual(session.run("next").artifacts, ())

    def test_host_tool_reports_files_without_putting_metadata_in_model_text(self):
        def generate():
            (self.workspace / "report.csv").write_bytes(b"a,b\n1,2\n")
            return ToolResult("ready", artifacts=("report.csv",))

        config = options(self.workspace, [[call("generate", {})], [chunk("done")]],
            tools=(Tool("generate", "Generate", {"type": "object"}, generate),), approver=lambda *_: True)
        with Session(config) as session:
            item, = session.run("report").artifacts
            self.assertEqual((item.path, item.operation, item.source), ("report.csv", "reported", "tool"))
            self.assertNotIn(item.sha256, str(config.model.client.completions.calls))

    def test_failed_or_invalid_host_reports_publish_no_partial_list(self):
        (self.workspace / "report.txt").write_text("ready")
        for result in (ToolResult("failed", True, ("report.txt",)),
                       ToolResult("ready", artifacts=("report.txt", "../invalid.txt"))):
            config = options(self.workspace, [[call("generate", {})], [chunk("done")]],
                tools=(Tool("generate", "Generate", {"type": "object"}, lambda: result),), approver=lambda *_: True)
            with Session(config) as session:
                self.assertEqual(session.run("report").artifacts, ())

    def test_persistent_rewind_and_conflict_protection_on_both_stores(self):
        for backend in ("files", "sqlite"):
            with self.subTest(backend=backend):
                target = self.workspace / f"{backend}.txt"
                original = "原始\r\n".encode("gbk")
                target.write_bytes(original)
                storage = ({"session_dir": self.workspace / "logs"} if backend == "files" else
                           {"session_store": SQLiteSessionStore(self.workspace / "sessions.sqlite")})
                config = options(self.workspace, [
                    [call("read_file", {"path": target.name})],
                    [call("write_file", {"path": target.name, "content": "改后\n"})],
                    [chunk("done")],
                ], builtin_tools=("read_file", "write_file"), approver=lambda *_: True, **storage)
                with Session(config) as session:
                    result = session.run("edit")
                    self.assertEqual(result.artifacts[0].operation, "updated")
                    resume = {"resume_from": session.session_path} if backend == "files" else {"resume_id": session.session_id}
                changed = target.read_bytes()
                with Session(config, **resume) as recovered:
                    self.assertEqual(recovered.checkpoints(), (1,))
                    target.write_bytes(b"external")
                    self.assertEqual(recovered.rewind(1).status, "conflict")
                    self.assertEqual(target.read_bytes(), b"external")
                    target.write_bytes(changed)
                    result = recovered.rewind(1)
                    self.assertEqual(result.status, "completed")
                    self.assertEqual(target.read_bytes(), original)
                with Session(config, **resume) as recovered:
                    self.assertEqual(recovered.checkpoints(), ())

    def test_reset_and_fork_do_not_resurrect_checkpoints(self):
        config = options(self.workspace, [[call("write_file", {"path": "a.txt", "content": "a"})], [chunk("done")]],
            builtin_tools=("write_file",), approver=lambda *_: True, session_dir=self.workspace / "logs")
        with Session(config) as session:
            session.run("create")
            with session.fork() as fork:
                self.assertEqual(fork.checkpoints(), ())
            path = session.session_path
            session.reset()
        with Session(config, resume_from=path) as recovered:
            self.assertEqual(recovered.checkpoints(), ())

    def test_failed_snapshot_commit_does_not_execute_the_edit_in_sdk(self):
        target = self.workspace / "a.txt"
        target.write_text("original")
        config = options(self.workspace, [
            [call("read_file", {"path": "a.txt"})],
            [call("write_file", {"path": "a.txt", "content": "changed"})], [chunk("failed")]],
            builtin_tools=("read_file", "write_file"), approver=lambda *_: True, session_dir=self.workspace / "logs")
        with Session(config) as session:
            original = session._log.commit_event

            def fail_prepare(kind, **fields):
                if fields.get("action") == "prepare":
                    session._log._broken = True
                    session._log.broken_reason = "disk full"
                    raise OSError("disk full")
                original(kind, **fields)

            with patch.object(session._log, "commit_event", side_effect=fail_prepare):
                with self.assertRaises(SessionStorageError):
                    session.run("edit")
            self.assertEqual(target.read_text(), "original")
            self.assertFalse(session._toolbox.artifacts.active)

    def test_failed_checkpoint_begin_closes_turn_collection(self):
        config = options(self.workspace, [[chunk("unused")]], session_dir=self.workspace / "logs")
        with Session(config) as session:
            original = session._log.commit_event

            def fail_begin(kind, **fields):
                if fields.get("action") == "begin":
                    session._log._broken = True
                    session._log.broken_reason = "disk full"
                    raise OSError("disk full")
                original(kind, **fields)

            with patch.object(session._log, "commit_event", side_effect=fail_begin):
                with self.assertRaises(SessionStorageError):
                    session.run("edit")
            self.assertFalse(session._toolbox.artifacts.active)
            self.assertEqual(session._agent.last_artifacts, ())
