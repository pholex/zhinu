"""Tool → approval → events → validated result → close → process restart."""
import argparse
import asyncio
from contextlib import aclosing
from dataclasses import replace
from pathlib import Path

from xiaoyu_agent_sdk import Allow, AsyncSession, OutputSpec, RunCompleted, Tool, ToolResult
from common import options, text_chunk, tool_chunk


async def main(demo: bool, workspace: Path, resume: Path | None) -> None:
    async def approve(name: str, arguments: dict) -> Allow:
        # Real hosts can await their approval UI here. This example grants only a lookup.
        print("Approved lookup:", name, arguments)
        return Allow()

    async def lookup(order_id: str) -> ToolResult:
        return ToolResult({"order_id": order_id, "ready": True})

    script = [[text_chunk("The previous order was A-42")]] if resume else [
        [tool_chunk("lookup_order", '{"order_id":"A-42"}')],
        [tool_chunk("structured_output", '{"order_id":"A-42","ready":"yes"}')],
        [tool_chunk("structured_output", '{"order_id":"A-42","ready":true}')],
    ]
    config = replace(options(demo, script, workspace), session_dir=workspace / ".sdk-sessions",
                     approver=approve, tools=(Tool(
                         "lookup_order", "Look up an order",
                         {"type": "object", "properties": {"order_id": {"type": "string"}},
                          "required": ["order_id"], "additionalProperties": False}, lookup,
                     ),))
    output = OutputSpec({"type": "object", "properties": {
        "order_id": {"type": "string"}, "ready": {"type": "boolean"}},
        "required": ["order_id", "ready"], "additionalProperties": False})
    async with AsyncSession(config, resume_from=resume) as session:
        if resume:
            print((await session.run("Which order did we just look up?")).text)
        else:
            async with aclosing(session.stream("Look up order A-42 and return its status", output=output)) as events:
                async for event in events:
                    print(event.kind)
                    if isinstance(event, RunCompleted):
                        print("Validated:", event.result.output_status, event.result.output)
                        print("Repair attempts:", event.result.output_retries)
        print("Resume in a new process with --resume", session.session_path)


parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true")
parser.add_argument("--workspace", type=Path, default=Path.cwd())
parser.add_argument("--resume", type=Path)
args = parser.parse_args()
asyncio.run(main(args.demo, args.workspace.resolve(), args.resume))
