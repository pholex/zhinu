"""Observe notifications and session state; --demo runs without network access."""
import argparse
import asyncio
from contextlib import aclosing
import tempfile
from pathlib import Path

from xiaoyu_agent_sdk import AsyncSession
from common import options, text_chunk


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        config = options(args.demo, [[text_chunk("Initial response.")],
                         [text_chunk("Background result acknowledged.")]], Path(temporary).resolve())
        async with await AsyncSession.open(config) as session:
            async with aclosing(session.watch_notifications()) as changes:
                print("Initial pending:", await anext(changes))
                await asyncio.to_thread(session.notify, "The background report is ready.", "report-ready")
                print("Notification pending:", await anext(changes))
                print("Before host starts a turn:", session.status())
                result = await session.run("Review the available background result.")
                print(result.text)
                print("Pending after turn:", await anext(changes))
                snapshot = await session.snapshot()
                print("History messages:", len(snapshot.history))
                print("Last answer:", snapshot.last_assistant_text)
        print("After close:", session.status())


if __name__ == "__main__":
    asyncio.run(main())
