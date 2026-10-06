"""Typed image input and host steering; --demo runs entirely offline."""
import argparse
import asyncio
import base64
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from xiaoyu_agent_sdk import (
    AsyncSession, Hook, HookDecision, ImageBlock, Prompt, RunCompleted,
    SteerAccepted, TextBlock,
)
from common import options, text_chunk


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--image", type=Path)
    args = parser.parse_args()
    image = args.image.read_bytes() if args.image else base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
    )
    ready, proceed = asyncio.Event(), asyncio.Event()

    async def on_prompt(payload: dict[str, Any]) -> HookDecision:
        ready.set()
        await proceed.wait()
        return HookDecision(False)

    with tempfile.TemporaryDirectory() as temporary:
        config = replace(options(args.demo, [[text_chunk("First description.")],
                         [text_chunk("简要图片描述。")]], Path(temporary).resolve()),
                         hooks=(Hook("UserPromptSubmit", on_prompt),))
        async with await AsyncSession.open(config) as session:
            async def consume() -> None:
                prompt: Prompt = [TextBlock("Describe this image."), ImageBlock(image)]
                async for event in session.stream(prompt):
                    if isinstance(event, SteerAccepted):
                        print("Accepted:", event.text)
                    elif isinstance(event, RunCompleted):
                        print(event.result.text)

            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(ready.wait(), 5)
                print("Queued:", session.steer("Use Chinese and keep it brief."))
            finally:
                proceed.set()
                await task
            for text in session.drain_steers():
                print("Not delivered; retain in the host UI:", text)


if __name__ == "__main__":
    asyncio.run(main())
