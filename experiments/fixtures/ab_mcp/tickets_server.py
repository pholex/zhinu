"""对照题用的假工单服务：stdio 上的 MCP server，零依赖。状态存一个 JSON 文件，每次调用记一行。

用法：python tickets_server.py <状态目录>。目录里要有 tickets.json；调用记录写进 calls.tsv。
列表一页 5 条，带 nextCursor——只看第一页就下结论会答错，这是题目的一部分。
"""
import json
import sys
import time
from pathlib import Path

for _stream in (sys.stdin, sys.stdout):  # 协议走 UTF-8，不跟着系统的控制台编码走
    _stream.reconfigure(encoding="utf-8", errors="replace")
STATE = Path(sys.argv[1]) / "tickets.json"
CALLS = Path(sys.argv[1]) / "calls.tsv"
PAGE = 5
TEXT = {"type": "string"}
TOOLS = [
    {"name": "list_tickets",
     "description": "列工单（不含评论）。可按 status 过滤：open / pending / closed。一页 5 条，"
                    "还有下一页时返回 next_cursor，把它传回 cursor 取下一页。",
     "inputSchema": {"type": "object", "properties": {"status": TEXT, "cursor": TEXT}}},
    {"name": "get_ticket", "description": "取一张工单的全部内容，含评论。",
     "inputSchema": {"type": "object", "properties": {"id": TEXT}, "required": ["id"]}},
    {"name": "add_comment", "description": "给工单加一条评论。",
     "inputSchema": {"type": "object", "properties": {"id": TEXT, "text": TEXT}, "required": ["id", "text"]}},
    {"name": "close_ticket", "description": "关闭一张工单。resolution 写关闭原因（done / stale / duplicate）。",
     "inputSchema": {"type": "object", "properties": {"id": TEXT, "resolution": TEXT},
                     "required": ["id", "resolution"]}},
]


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def call(name: str, args: dict) -> tuple[str, bool]:
    with CALLS.open("a", encoding="utf-8") as log:
        log.write(f"{int(time.time())}\t{name}\t{json.dumps(args, ensure_ascii=False)}\n")
    tickets = json.loads(STATE.read_text(encoding="utf-8"))
    by_id = {t["id"]: t for t in tickets}
    if name == "list_tickets":
        status = args.get("status")
        if status and status not in ("open", "pending", "closed"):
            return f"status 只能是 open / pending / closed，收到 {status!r}", True
        rows = [t for t in tickets if not status or t["status"] == status]
        try:
            start = int(args.get("cursor") or 0)
        except ValueError:
            return "cursor 不认识：只能传上一页返回的 next_cursor", True
        page = [{k: v for k, v in t.items() if k != "comments"} for t in rows[start:start + PAGE]]
        out = {"tickets": page, "total": len(rows)}
        if start + PAGE < len(rows):
            out["next_cursor"] = str(start + PAGE)
        return json.dumps(out, ensure_ascii=False, indent=1), False
    ticket = by_id.get(str(args.get("id") or ""))
    if ticket is None:
        return f"没有这张工单：{args.get('id')!r}", True
    if name == "get_ticket":
        return json.dumps(ticket, ensure_ascii=False, indent=1), False
    if name == "add_comment":
        if not str(args.get("text") or "").strip():
            return "评论不能为空", True
        ticket["comments"].append(str(args["text"]))
    elif name == "close_ticket":
        if ticket["status"] == "closed":
            return f"{ticket['id']} 已经是关闭状态", True
        if not str(args.get("resolution") or "").strip():
            return "关闭要写 resolution", True
        ticket["status"], ticket["resolution"] = "closed", str(args["resolution"])
    else:
        return f"没有这个工具：{name}", True
    STATE.write_text(json.dumps(tickets, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return f"ok：{ticket['id']}", False


for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method, mid = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}}, "serverInfo": {"name": "tickets", "version": "1.0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        text, failed = call(str(params.get("name")), params.get("arguments") or {})
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text", "text": text}], "isError": failed}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"不支持：{method}"}})
