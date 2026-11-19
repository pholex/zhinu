"""验收脚本（题面里不出现）：该关的工单都关了、关之前留了评论、不该动的一张没动。看的是服务端的状态与调用记录。"""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["AB_STATE"])
SEED = Path(__file__).resolve().parents[1] / "ab_mcp" / "tickets.json"
before = {t["id"]: t for t in json.loads(SEED.read_text(encoding="utf-8"))}
after = {t["id"]: t for t in json.loads((STATE / "tickets.json").read_text(encoding="utf-8"))}
calls = []
if (STATE / "calls.tsv").is_file():
    for line in (STATE / "calls.tsv").read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            calls.append((parts[1], json.loads(parts[2])))

STALE = ["T-102", "T-106", "T-108", "T-112"]   # open 且最后更新早于 2026-11-06
closed_now = sorted(i for i in after if after[i]["status"] == "closed" and before[i]["status"] != "closed")


def commented_first(ticket: str) -> bool:
    order = [name for name, args in calls if args.get("id") == ticket and name in ("add_comment", "close_ticket")]
    return "add_comment" in order and "close_ticket" in order and order.index("add_comment") < order.index("close_ticket")


listed = [args for name, args in calls if name == "list_tickets"]
checks = [
    ("四张过期的都关了", all(after[i]["status"] == "closed" for i in STALE)),
    ("没有多关（正好 14 天的、pending 的不算）", closed_now == STALE),
    ("关闭原因写的是 stale", all(after[i].get("resolution") == "stale" for i in closed_now) and bool(closed_now)),
    ("每张关之前先留了评论", all(commented_first(i) for i in closed_now) and bool(closed_now)),
    ("评论只加在关掉的工单上，各一条", all(
        len(after[i]["comments"]) - len(before[i]["comments"]) == (1 if i in closed_now else 0) for i in after)),
    ("翻完了所有页", any(args.get("cursor") for args in listed)),
]
for title, ok in checks:
    print(("ok   " if ok else "FAIL ") + title)
print(f"{sum(ok for _, ok in checks)}/{len(checks)}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
