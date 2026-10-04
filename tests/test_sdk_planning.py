"""Optional planning respects host configuration and survives session operations."""
from __future__ import annotations

import importlib.util
import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    AsyncSession, ConfigurationError, PlanStep, PlanUpdated, Plugin, Session,
    SQLiteSessionStore, Tool,
)
from tests.test_sdk_controls import options
from tests.test_agent_paths import call_fragment, chunk
from xiaoyu.tools import Tool as KernelTool


def update(step="Inspect", status="in_progress", ident="plan-1"):
    return chunk(tool_calls=[call_fragment(0, ident, "update_plan", json.dumps({
        "plan": [{"step": step, "status": status}], "explanation": "Next step",
    }))])


def names(config):
    return {t["function"]["name"] for t in config.model.client.completions.calls[-1].get("tools", [])}


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class PlanningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    def test_default_off_and_explicit_opt_in_is_independent_of_builtin_selection(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                config = options(self.workspace, [[chunk("done")]], enable_plan=enabled)
                with Session(config) as session:
                    session.run("start")
                    self.assertEqual("update_plan" in names(config), enabled)
                    self.assertNotIn("read_file", names(config))
                    self.assertEqual(session.snapshot().plan, ())

    def test_invalid_flag_fails_before_client_use(self):
        for flag in (1, "false", None):
            config = options(self.workspace, [], enable_plan=flag)
            with self.assertRaises(ConfigurationError):
                Session(config)
            self.assertEqual(config.model.client.completions.calls, [])

    def test_plan_events_are_detached_and_snapshot_steps_are_immutable(self):
        config = options(self.workspace, [[update()], [chunk("done")]], enable_plan=True)
        with Session(config) as session:
            events = list(session.stream("start"))
            event = next(e for e in events if isinstance(e, PlanUpdated))
            self.assertEqual(event.session_id, session.session_id)
            self.assertTrue(event.run_id)
            self.assertEqual(event.explanation, "Next step")
            event.plan[0]["step"] = "Host changed event"
            before = session.snapshot()
            self.assertEqual(before.plan, (PlanStep("Inspect", "in_progress"),))
            with self.assertRaises(FrozenInstanceError):
                before.plan[0].step = "mutated"
            session.reset()
            self.assertEqual(session.snapshot().plan, ())
            self.assertEqual(before.plan[0].step, "Inspect")

    def test_plan_mode_allows_updates_without_granting_exit_permission(self):
        exit_call = chunk(tool_calls=[call_fragment(0, "exit", "exit_plan_mode", '{"plan":"Ready"}')])
        config = options(self.workspace, [[update()], [exit_call], [chunk("waiting")]],
                         enable_plan=True, mode="plan")
        with Session(config) as session:
            events = list(session.stream("plan"))
            self.assertTrue(any(isinstance(e, PlanUpdated) for e in events))
            self.assertTrue(any(e.kind == "tool.denied" and e.name == "exit_plan_mode" for e in events))
            self.assertEqual(session.snapshot().mode, "plan")

    def test_deny_and_invalid_updates_do_not_replace_previous_plan(self):
        config = options(self.workspace, [[update()], [chunk("done")],
                         [update("Bad", "unknown", "invalid")], [chunk("rejected")]], enable_plan=True)
        with Session(config) as session:
            session.run("first")
            events = list(session.stream("invalid"))
            self.assertFalse(any(isinstance(e, PlanUpdated) for e in events))
            self.assertEqual(session.snapshot().plan, (PlanStep("Inspect", "in_progress"),))
        config = options(self.workspace, [[update()], [chunk("denied")]], enable_plan=True,
                         deny_rules=("deny update_plan",))
        with Session(config) as session:
            events = list(session.stream("denied"))
            self.assertTrue(any(e.kind == "tool.denied" for e in events))
            self.assertEqual(session.snapshot().plan, ())

    def test_enabled_plan_rejects_host_and_plugin_name_collisions(self):
        tool = Tool("update_plan", "Custom", {"type": "object"}, lambda: "custom")
        with self.assertRaises(ConfigurationError):
            Session(options(self.workspace, [], enable_plan=True, tools=(tool,)))
        plugin_tool = KernelTool("update_plan", "Custom", {"type": "object"}, lambda: "custom")
        with patch("xiaoyu.tools.load_plugin_tools", return_value=[plugin_tool]):
            with self.assertRaises(ConfigurationError):
                Session(options(self.workspace, [], enable_plan=True, plugins=(Plugin("p", "dist"),)))
        # Existing hosts may still own this name when the native tool is off.
        with Session(options(self.workspace, [], tools=(tool,))):
            pass

    def test_resume_restores_plan_but_host_option_controls_tool_availability(self):
        for backend in ("jsonl", "sqlite"):
            with self.subTest(backend=backend):
                storage = ({"session_dir": self.workspace / "logs"} if backend == "jsonl" else
                           {"session_store": SQLiteSessionStore(self.workspace / "plans.sqlite")})
                config = options(self.workspace, [[update()], [chunk("done")]], enable_plan=True, **storage)
                with Session(config) as session:
                    session.run("plan")
                    resume = ({"resume_from": session.session_path} if backend == "jsonl" else
                              {"resume_id": session.session_id})
                with Session(replace(config, enable_plan=False), **resume) as restored:
                    self.assertEqual(restored.snapshot().plan, (PlanStep("Inspect", "in_progress"),))
                    self.assertIsNone(restored._toolbox.get("update_plan"))
                    restored.reset()
                with Session(config, **resume) as restored:
                    self.assertEqual(restored.snapshot().plan, ())

    def test_fork_and_rewind_preserve_the_plan_at_the_selected_history(self):
        config = options(self.workspace, [[update()], [chunk("first")],
                         [update("Deliver", "pending", "plan-2")], [chunk("second")]],
                         enable_plan=True, session_dir=self.workspace / "logs")
        with Session(config) as parent:
            parent.run("inspect")
            with parent.fork() as child:
                self.assertEqual(child.snapshot().plan, parent.snapshot().plan)
                child.reset()
                self.assertTrue(parent.snapshot().plan)
            parent.run("deliver")
            self.assertEqual(parent.snapshot().plan[0].step, "Deliver")
            parent.rewind(parent.checkpoints()[-1], files=False)
            expected = (PlanStep("Inspect", "in_progress"),)
            self.assertEqual(parent.snapshot().plan, expected)
            path = parent.session_path
        with Session(config, resume_from=path) as restored:
            self.assertEqual(restored.snapshot().plan, expected)

    def test_optional_network_factories_are_not_loaded_by_sdk(self):
        with patch("xiaoyu.websearch.make_web_search_tool") as web, \
             patch("xiaoyu.xsearch.make_x_search_tool") as search, \
             patch("xiaoyu.deepresearch.make_deep_research_tools") as research:
            with Session(options(self.workspace, [])) as session:
                for name in ("web_search", "x_search", "deep_research", "deep_research_status"):
                    self.assertIsNone(session._toolbox.get(name))
            web.assert_not_called()
            search.assert_not_called()
            research.assert_not_called()


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncPlanningTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_and_snapshot_share_the_same_plan(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = options(Path(temporary).resolve(), [[update()], [chunk("done")]], enable_plan=True)
            async with AsyncSession(config) as session:
                events = [event async for event in session.stream("plan")]
                self.assertTrue(any(isinstance(e, PlanUpdated) for e in events))
                self.assertEqual((await session.snapshot()).plan, (PlanStep("Inspect", "in_progress"),))
