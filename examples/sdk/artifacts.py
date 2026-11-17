"""File deliveries from a host tool; --demo runs without network or credentials."""
import argparse
from dataclasses import replace
from pathlib import Path
import tempfile

from xiaoyu_agent_sdk import Session, Tool, ToolResult
from common import options, text_chunk, tool_chunk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()

        def report() -> ToolResult:
            (workspace / "report.csv").write_text("name,count\nexample,3\n", encoding="utf-8")
            return ToolResult("Report generated.", artifacts=("report.csv",))

        config = replace(options(args.demo, [[tool_chunk("report", "{}")],
            [text_chunk("Report ready.")]], workspace),
            tools=(Tool("report", "Create the sample CSV report", {"type": "object"}, report),),
            approver=lambda *_: True)
        with Session(config) as session:
            result = session.run("Create the sample report.")
            for artifact in result.artifacts:
                print(artifact.path, artifact.size, artifact.sha256, artifact.status)


if __name__ == "__main__":
    main()
