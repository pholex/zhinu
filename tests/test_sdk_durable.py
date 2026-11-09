from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import DurableTaskRunner, WorkStep, WorkResult, TaskAcceptance, SessionLockedError, StateError


class DurableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "work.db"
        self.now = [100.0]
        self.calls = []

    def runner(self, **kwargs):
        return DurableTaskRunner(self.path, clock=lambda: self.now[0], **kwargs)

    def test_restart_wait_probe_then_acceptance_does_not_repeat_execution(self):
        def execute(ctx):
            self.calls.append(ctx.idempotency_key)
            return WorkResult("waiting", "CI submitted", checkpoint="ci:42", fingerprint="running")
        def probe(ctx):
            self.assertEqual(ctx.checkpoint, "ci:42")
            return WorkResult("succeeded", "CI passed")
        with self.runner(handlers={"build": execute}) as runner:
            run = runner.create((WorkStep("build", "build", probe="ci", evaluator="tests"),))
            self.assertEqual(runner.tick(run.id).steps[0].state, "waiting")
        with self.runner(handlers={"build": execute}, probes={"ci": probe}, evaluators={"tests": lambda ctx: TaskAcceptance("passed", "suite passed")}) as runner:
            self.assertEqual(runner.tick(run.id).calls, 1)
            self.now[0] += 60
            self.assertEqual(runner.tick(run.id).steps[0].state, "verifying")
            final = runner.tick(run.id)
            self.assertEqual(final.state, "completed")
            self.assertEqual(final.steps[0].acceptance, "passed")
        self.assertEqual(len(self.calls), 1)

    def test_crash_safe_retry_keeps_key_unsafe_requires_host_approval(self):
        def crash(ctx):
            self.calls.append(ctx.idempotency_key)
            raise KeyboardInterrupt()
        with self.runner(handlers={"send": crash}) as runner:
            unsafe = runner.create((WorkStep("send", "send"),))
            safe = runner.create((WorkStep("send", "send", retry_safe=True),))
            for run in (unsafe, safe):
                with self.assertRaises(KeyboardInterrupt): runner.tick(run.id)
        def finish(ctx):
            self.calls.append(ctx.idempotency_key)
            return WorkResult("succeeded", "business receipt reconciled")
        with self.runner(handlers={"send": finish}) as runner:
            self.assertEqual(runner.get(unsafe.id).state, "uncertain")
            with self.assertRaises(ValueError): runner.resume(unsafe.id)
            self.assertEqual(runner.tick(safe.id).state, "completed")
            self.assertEqual(self.calls[1], self.calls[2])
            runner.resume(unsafe.id, allow_uncertain=True)
            runner.tick(unsafe.id)
            self.assertEqual(self.calls[0], self.calls[3])

    def test_probe_exception_backoff_does_not_spend_repair_budget(self):
        def failed_probe(ctx): raise OSError("secret")
        with self.runner(handlers={"build": lambda ctx: WorkResult("waiting", "submitted")}, probes={"ci": failed_probe}) as runner:
            run = runner.create((WorkStep("build", "build", probe="ci", max_retries=1),))
            runner.tick(run.id)
            self.now[0] += 60
            failed = runner.tick(run.id)
            self.assertEqual((failed.steps[0].retries, failed.steps[0].repairs), (1, 0))
            self.assertNotIn("secret", str(failed))
            self.assertEqual(runner.tick(run.id).calls, 2)
            self.now[0] += 60
            self.assertEqual(runner.tick(run.id).state, "failed")

    def test_failed_acceptance_repairs_only_current_step(self):
        order = []
        def execute(ctx):
            order.append(ctx.step.name)
            return WorkResult("succeeded", "written", checkpoint="child:123")
        checks = iter([TaskAcceptance("failed", "test failed"), TaskAcceptance("passed", "fixed")])
        with self.runner(handlers={"run": execute}, evaluators={"check": lambda ctx: next(checks)}) as runner:
            run = runner.create((WorkStep("first", "run"), WorkStep("second", "run", evaluator="check", retry_safe=True)))
            for _ in range(3): runner.tick(run.id)
            self.now[0] += 60
            runner.tick(run.id)
            final = runner.tick(run.id)
            self.assertEqual(final.state, "completed")
            self.assertEqual(final.steps[1].repairs, 1)
        self.assertEqual(order, ["first", "second", "second"])

    def test_wait_deadline_stall_budget_and_cancel_survive_restart(self):
        waiting = lambda ctx: WorkResult("waiting", "still waiting", fingerprint="same")
        with self.runner(handlers={"wait": waiting}, probes={"probe": waiting}) as runner:
            stalled = runner.create((WorkStep("wait", "wait", probe="probe", max_unchanged=1),))
            deadline = runner.create((WorkStep("wait", "wait", probe="probe"),), timeout=10)
            cancelled = runner.create((WorkStep("wait", "wait"),))
            runner.cancel(cancelled.id)
            runner.tick(stalled.id)
            self.now[0] += 60
            self.assertEqual(runner.tick(stalled.id).state, "paused")
            self.assertEqual(runner.tick(deadline.id).state, "failed")
        with self.runner(handlers={"wait": waiting}) as runner:
            self.assertEqual(runner.tick(cancelled.id).state, "cancelled")
            self.assertEqual(runner.tick(stalled.id).state, "paused")

    def test_lock_and_commit_failure_prevent_duplicate_side_effects(self):
        with self.runner(handlers={"run": lambda ctx: self.fail("must not execute")}) as runner:
            with self.assertRaises(SessionLockedError): self.runner(handlers={})
            run = runner.create((WorkStep("run", "run"),))
            with patch.object(runner, "_save", side_effect=StateError("disk full")):
                with self.assertRaises(StateError): runner.tick(run.id)
            self.assertEqual(runner.get(run.id).calls, 0)

    def test_unknown_callback_or_acceptance_pauses_without_execution(self):
        with self.runner(handlers={}) as runner:
            run = runner.create((WorkStep("run", "run"),))
            self.assertEqual(runner.tick(run.id).state, "paused")
        with self.runner(handlers={"run": lambda ctx: WorkResult("succeeded", "done")}, evaluators={"check": lambda ctx: TaskAcceptance("unknown", "needs owner")}) as runner:
            runner.resume(run.id)
            self.assertEqual(runner.tick(run.id).state, "completed")
            other = runner.create((WorkStep("run", "run", evaluator="check"),))
            runner.tick(other.id)
            self.assertEqual(runner.tick(other.id).state, "paused")

    def test_handler_exceptions_are_uncertain_and_retry_budgets_are_finite(self):
        def error(ctx): raise RuntimeError("private business data")
        with self.runner(handlers={"run": error}) as runner:
            run = runner.create((WorkStep("run", "run", retry_safe=True),))
            result = runner.tick(run.id)
            self.assertEqual(result.state, "uncertain")
            self.assertNotIn("private business", str(result))
            for invalid in (0, float("inf"), float("nan"), True):
                with self.assertRaises(ValueError): runner.create((WorkStep("run", "run"),), timeout=invalid)
