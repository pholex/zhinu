"""验收脚本（题面里不出现）：只读题——服务端状态一个字没变，而且确实是翻完页才回答的。"""
import json
import os
import sys
from pathlib import Path

STATE = Path(os.environ["AB_STATE"])
SEED = Path(__file__).resolve().parents[1] / "ab_mcp" / "tickets.json"
same = json.loads(SEED.read_text(encoding="utf-8")) == json.loads((STATE / "tickets.json").read_text(encoding="utf-8"))
names, cursors = [], []
if (STATE / "calls.tsv").is_file():
    for line in (STATE / "calls.tsv").read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            names.append(parts[1])
            cursors.append(json.loads(parts[2]).get("cursor"))
checks = [
    ("工单一张没动", same),
    ("没调过写工具", not any(name in ("add_comment", "close_ticket") for name in names)),
    ("用了工单服务", "list_tickets" in names),
    ("翻完了所有页", any(cursors)),
]
for title, ok in checks:
    print(("ok   " if ok else "FAIL ") + title)
print(f"{sum(ok for _, ok in checks)}/{len(checks)}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
