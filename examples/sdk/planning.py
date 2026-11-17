"""Opt-in task plans through events and immutable snapshots; --demo is offline."""
import argparse
from dataclasses import replace
from pathlib import Path
import tempfile

from xiaoyu_agent_sdk import PlanUpdated, Session
from common import options, text_chunk, tool_chunk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(options(args.demo, [[tool_chunk("update_plan",
            '{"plan":[{"step":"Inspect inputs","status":"in_progress"}]}')],
            [text_chunk("Plan ready.")]], Path(temporary).resolve()), enable_plan=True)
        with Session(config) as session:
            for event in session.stream("Use update_plan to list the steps for reviewing inputs."):
                if isinstance(event, PlanUpdated):
                    print("Plan update:", event.plan)
            print("Current plan:", session.snapshot().plan)
            session.reset()
            print("After reset:", session.snapshot().plan)


if __name__ == "__main__":
    main()
