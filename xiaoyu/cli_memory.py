"""Explicit memory review from the CLI; no model or automatic publication."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

from .config import user_config_dir
from .memory import MemoryCandidate, MemoryScope, MemoryStore
from ._state_db import StateError


def memory_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu memory", description="按作用域管理记忆候选；接受后才参与检索")
    parser.add_argument("--db", type=Path, default=user_config_dir() / "memory.sqlite3")
    parser.add_argument("--owner", required=True, help="宿主指定的用户标识")
    parser.add_argument("--project", default="", help="精确项目标识；空值为个人作用域，不自动跨域检索")
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("propose", help="提交候选，不立即生效")
    add.add_argument("key")
    add.add_argument("text")
    add.add_argument("--kind", choices=("preference", "fact", "episode", "lesson", "skill"), default="fact")
    add.add_argument("--source", required=True)
    add.add_argument("--evidence", required=True)
    add.add_argument("--base-revision", type=int, default=0)
    add.add_argument("--expires-at", type=float)
    review = commands.add_parser("accept", help="确认候选生效")
    review.add_argument("id")
    review.add_argument("--reviewer", required=True)
    for name in ("reject", "show"):
        commands.add_parser(name).add_argument("id")
    for name in ("forget", "history"):
        commands.add_parser(name).add_argument("key")
    listing = commands.add_parser("list")
    listing.add_argument("--status", choices=("pending", "active", "superseded", "rejected", "forgotten"))
    recall = commands.add_parser("recall")
    recall.add_argument("query", nargs="?", default="")
    recall.add_argument("--budget", type=int, default=4000)
    export = commands.add_parser("export-skill", help="将已接受技能写入新的 SKILL.md，不覆盖现有文件")
    export.add_argument("id")
    export.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        scope, store = MemoryScope(args.owner, args.project), MemoryStore(args.db)
        action = args.command
        if action == "propose":
            result = asdict(store.propose(scope, MemoryCandidate(args.key, args.text, args.kind, args.source, args.evidence, args.base_revision, args.expires_at)))
        elif action == "accept":
            result = asdict(store.accept(scope, args.id, reviewer=args.reviewer))
        elif action == "show":
            result = asdict(store.get(scope, args.id))
        elif action == "reject":
            result = asdict(store.reject(scope, args.id))
        elif action == "forget":
            store.forget(scope, args.key)
            result = {"forgotten": args.key}
        elif action == "export-skill":
            result = {"path": str(store.export_skill(scope, args.id, args.destination))}
        elif action == "recall":
            print(store.context(scope, args.query, char_budget=args.budget))
            return 0
        else:
            records = store.list(scope, key=args.key) if action == "history" else store.list(scope, status=args.status)
            print(json.dumps([asdict(r) for r in records], ensure_ascii=False))
            return 0
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError, StateError) as exc:
        print(f"记忆操作失败：{exc}", file=sys.stderr)
        return 2
