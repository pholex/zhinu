"""Idle session controls with stable history and usage; --demo is offline."""
import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile

from xiaoyu_agent_sdk import AsyncSession
from common import options, text_chunk


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--model", help="Another model name on the same configured endpoint")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(options(args.demo, [[text_chunk("First answer.")],
                         [text_chunk("Fresh conversation.")]], Path(temporary).resolve()),
                         session_dir=Path(temporary) / "sessions")
        async with AsyncSession(config) as session:
            await session.run("Say hello.")
            print(await session.set_mode("plan"))
            print(await session.set_mode("default"))
            await session.switch_model(args.model or ("demo-next" if args.demo else config.model.model))
            await session.set_budget_tokens(100000)
            before = await session.snapshot()
            await session.reset()
            after = await session.snapshot()
            print("Same identity:", before.session_id == after.session_id)
            print("History cleared:", not after.history)
            print("Usage retained:", before.usage == after.usage)
            print((await session.run("Start a new conversation.")).text)


if __name__ == "__main__":
    asyncio.run(main())
