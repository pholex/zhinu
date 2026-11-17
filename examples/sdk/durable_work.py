"""Persist a draft → approval wait → acceptance flow; --demo needs no model key.

Real use: --state-dir /path/to/work. Run again from a host timer; write 'approved'
to approval.txt when reviewed. Each invocation advances at most one callback.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import tempfile
import time

from xiaoyu_agent_sdk import (BudgetOptions, DurableTaskRunner, Session, TaskAcceptance,
                             WorkContext, WorkResult, WorkStep)
from common import options, text_chunk


def drive(root: Path, demo: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    now = [time.time()]
    draft, approval = root / "draft.txt", root / "approval.txt"

    def write(ctx: WorkContext) -> WorkResult:
        opts = replace(options(demo, [[text_chunk("A checked draft")]], root), budget=BudgetOptions(max_requests=2))
        with Session(opts) as session:
            result = session.run("Write a short project status template. Prior review: " + ctx.evidence)
            if result.interrupted or result.stopped != "done":
                return WorkResult("blocked", "Draft did not complete")
        draft.write_text(result.text, encoding="utf-8")
        return WorkResult("waiting", "Draft awaits owner review", checkpoint=str(draft), fingerprint="awaiting-owner")

    def probe(ctx: WorkContext) -> WorkResult:
        text = approval.read_text(encoding="utf-8", errors="replace").strip() if approval.exists() else ""
        if text == "approved":
            return WorkResult("succeeded", "Owner approved the draft")
        return WorkResult("waiting", "Owner has not approved", fingerprint="awaiting-owner")

    def accept(ctx: WorkContext) -> TaskAcceptance:
        valid = draft.is_file() and bool(draft.read_text(encoding="utf-8", errors="replace").strip())
        return TaskAcceptance("passed" if valid else "failed", "Approved draft exists" if valid else "Draft is missing")

    def open_runner() -> DurableTaskRunner:
        return DurableTaskRunner(root / "work.sqlite", handlers={"draft": write}, probes={"approval": probe},
                                 evaluators={"artifact": accept}, clock=lambda: now[0])

    with open_runner() as runner:
        existing = runner.list()
        run = existing[0] if existing else runner.create((WorkStep("draft", "draft", probe="approval", evaluator="artifact", retry_safe=True),), timeout=86400, max_calls=50)
        current = runner.tick(run.id)
        print(current.id, current.state, current.reason)
    if demo:
        approval.write_text("approved", encoding="utf-8")
        now[0] += 60
        # A new runner restores the checkpoint; it does not generate another draft.
        with open_runner() as restored:
            restored.tick(run.id)
            completed = restored.tick(run.id)
            assert completed.state == "completed" and completed.steps[0].attempt == 1
            print("Restored and accepted:", completed.state)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--state-dir", type=Path)
    args = parser.parse_args()
    if args.demo:
        with tempfile.TemporaryDirectory() as temporary:
            drive(Path(temporary), True)
    elif args.state_dir:
        drive(args.state_dir.resolve(), False)
    else:
        parser.error("Pass --state-dir for persistent work, or --demo")


if __name__ == "__main__":
    main()
