"""python examples/sdk/orchestration.py --demo"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import tempfile

from xiaoyu_agent_sdk import BudgetOptions, Session, SQLiteSessionStore, Subagent, TaskSpec, TaskAcceptance, TelemetryOptions, TraceRecord
from common import options, text_chunk


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        traces: list[TraceRecord] = []
        opts = replace(options(args.demo, [[text_chunk("first source")], [text_chunk("second source")],
                                          [text_chunk("combined answer")]], root),
            session_store=SQLiteSessionStore(root / "sessions.sqlite"), max_parallel_tasks=2,
            subagents=(Subagent("analyst", "Analyze assigned information", "Complete the assigned task.", ()),),
            budget=BudgetOptions(max_requests=10), telemetry=TelemetryOptions(traces.append))
        with Session(opts) as session:
            handles = session.tasks.submit((TaskSpec("first", "analyst", "Find the first fact."),
                TaskSpec("second", "analyst", "Find the second fact."),
                TaskSpec("combine", "analyst", "Combine the dependency results.", ("first", "second"))))
            for handle in handles:
                result = handle.wait(120)
                print(result.name, result.state, result.answer)
            # Demo evidence is a deterministic fixture check; real hosts supply business checks.
            checked = session.tasks.evaluate(result.task_id, lambda task: TaskAcceptance(
                "passed" if args.demo and task.answer == "combined answer" else "unknown",
                "Matched the offline fixture" if args.demo else "Needs a host business check",
            ))
            print("Acceptance:", checked.acceptance, checked.acceptance_evidence)
            session_id = session.session_id
        with Session(opts, resume_id=session_id) as restored:
            print("Restored without rerunning:", [(t.name, t.state) for t in restored.tasks.list()])
        print("Exported traces:", len(traces))


if __name__ == "__main__":
    main()
