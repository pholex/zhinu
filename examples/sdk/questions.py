"""Host questions through the SDK; --demo uses scripted answers and no network."""
import argparse
import json
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from xiaoyu_agent_sdk import Asker, Session
from common import options, text_chunk, tool_chunk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()

    def ask(questions: list[dict[str, Any]]) -> dict[str, str]:
        answers = {}
        for question in questions:
            print(question["question"])
            print(" / ".join(option["label"] for option in question["options"]))
            answer = "Chinese" if args.demo else input("Answer (empty to skip): ").strip()
            if answer:
                answers[question["question"]] = answer
        return answers

    asker: Asker = ask
    script = [[tool_chunk("ask_user", json.dumps({"questions": [
        {"question": "Output language?", "options": ["English", "Chinese"]},
    ]}))], [text_chunk("已按你的选择，使用中文撰写报告。")]]
    with tempfile.TemporaryDirectory() as temporary:
        config = replace(options(args.demo, script, Path(temporary).resolve()),
                         asker=asker, question_timeout=120.0)
        with Session(config) as session:
            print(session.run("Ask which language to use, then confirm the choice.").text)


if __name__ == "__main__":
    main()
