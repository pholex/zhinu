"""One-shot and multi-turn synchronous calls; use --demo without credentials."""
import argparse
from pathlib import Path

from xiaoyu_agent_sdk import Session, run
from common import options, text_chunk

parser = argparse.ArgumentParser()
parser.add_argument("--demo", action="store_true")
args = parser.parse_args()
config = options(args.demo, [[text_chunk("Hello")], [text_chunk("Remembered 42")],
                             [text_chunk("42")]], Path.cwd())
print(run("Say hello", config).text)
with Session(config) as session:
    print(session.run("Remember the number 42").text)
    print(session.run("What number did I give you?").text)
