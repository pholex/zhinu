"""Persistent storage and resume using only public SDK APIs. --demo is offline."""
from __future__ import annotations

import argparse
from pathlib import Path
import tempfile

from xiaoyu_agent_sdk import Session, SQLiteSessionStore
from dataclasses import replace
from common import options, text_chunk


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="sdk-storage-example-") as directory:
        workspace = Path(directory).resolve()
        store = SQLiteSessionStore(workspace / "sessions.sqlite")
        config = replace(options(args.demo, [[text_chunk("Remembered Xiaoyu.")]], workspace), session_store=store)
        with Session(config) as session:
            print(session.run("Remember the project name: Xiaoyu.").text)
            key = session.session_id
        config = replace(options(args.demo, [[text_chunk("Xiaoyu.")]], workspace),
                         session_store=SQLiteSessionStore(store.path))
        with Session(config, resume_id=key) as restored:
            print(restored.run("What is the project name?").text)
        print("Stored sessions:", len(store.list_sessions()))


if __name__ == "__main__":
    main()
