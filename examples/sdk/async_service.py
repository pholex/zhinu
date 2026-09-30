"""Async turns and streaming with explicit stream cleanup."""
import argparse
import asyncio
from contextlib import aclosing
from pathlib import Path

from xiaoyu_agent_sdk import AsyncSession, RunCompleted, TextDelta
from common import options, text_chunk


async def main(demo: bool) -> None:
    config = options(demo, [[text_chunk("Hello "), text_chunk("from Xiaoyu")],
                            [text_chunk("Continued")]], Path.cwd())
    async with AsyncSession(config) as session:
        async with aclosing(session.stream("Say hello")) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    print(event.text, end="", flush=True)
                elif isinstance(event, RunCompleted):
                    print("\nStopped:", event.result.stopped)
        print((await session.run("Continue briefly")).text)


parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true")
args = parser.parse_args()
asyncio.run(main(args.demo))
