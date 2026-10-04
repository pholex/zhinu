"""Persist a question, reopen the session, submit and accept an answer; --demo is offline."""
import argparse
import asyncio
from contextlib import aclosing, suppress
from dataclasses import replace
from pathlib import Path
import tempfile

from xiaoyu_agent_sdk import AsyncSession, QuestionAnswer, QuestionOptions, SQLiteSessionStore
from common import options, text_chunk, tool_chunk


async def foreground_demo(root: Path) -> None:
    config = replace(options(True, [[tool_chunk("ask_user",
        '{"questions":[{"question":"Which color?","options":["Blue","Green"]}]}')],
        [text_chunk("Using your submitted color.")]], root),
        questions=QuestionOptions(foreground_timeout_seconds=60),
        session_store=SQLiteSessionStore(root / "foreground.sqlite"))
    async with AsyncSession(config) as session:
        async def respond() -> None:
            async with aclosing(session.questions.watch()) as events:
                async for event in events:
                    if event.question.state not in {"open", "pending"}:
                        continue
                    # Synthetic submission only in --demo; a real UI collects an explicit answer.
                    replies = tuple(QuestionAnswer(i.item_id, (i.options[0].label,)) for i in event.question.items)
                    receipt = await session.questions.answer(event.question.question_id, replies, "demo-click")
                    print("Foreground submission:", receipt.state)
                    return
        responder = asyncio.create_task(respond())
        try:
            result = await session.run("Ask which color I prefer.")
            await responder
            print("Foreground result:", result.text)
            print("Foreground pending:", len(await session.questions.list_pending()))
        finally:
            responder.cancel()
            with suppress(asyncio.CancelledError):
                await responder


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        config = replace(options(args.demo, [[tool_chunk("ask_user",
            '{"questions":[{"question":"Which output format?","options":["JSON","Markdown"]}]}')],
            [text_chunk("Waiting for your format preference.")], [text_chunk("Using JSON.")]], root),
            questions=QuestionOptions(), session_store=SQLiteSessionStore(root / "questions.sqlite"))
        async with AsyncSession(config) as session:
            await session.run("Use ask_user to ask which output format I prefer.")
            ident = session.session_id
        async with AsyncSession(config, resume_id=ident) as session:
            for question in await session.questions.list_pending():
                reply = []
                for item in question.items:
                    if args.demo:
                        reply.append(QuestionAnswer(item.item_id, (item.options[0].label,)))
                    else:
                        text = await asyncio.to_thread(input, f"{item.question} (blank to skip): ")
                        reply.append(QuestionAnswer(item.item_id, custom=text, skipped=not text.strip()))
                receipt = await session.questions.answer(question.question_id, tuple(reply), "example-submission")
                print("Stored:", receipt.state)
                async with aclosing(session.questions.watch()) as events:
                    event = await anext(events)
                    print("Observed:", event.kind, "version", event.question.version)
            await session.run("Continue with my submitted answer.")
            print("Still pending:", len(await session.questions.list_pending()))
        if args.demo:
            await foreground_demo(root)


if __name__ == "__main__":
    asyncio.run(main())
