"""运单系统的 MCP server（stdio，纯标准库）——宿主应用"自有工具"的最小样本。

一个真实的宿主会把它换成对自家 API 的封装；形状不变：
- 只读工具（list_shipments / get_shipment）：查询；
- 改动工具（reroute_shipment）：有后果的动作。小羽对**所有** MCP 工具 fail-closed
  （requires_approval），所以它一定会经审批回路挂起，等宿主 UI 放行。

数据放内存，进程即状态：serve 为每个会话单独拉起一份（会话私有 manager），
关会话即退出。
"""

from __future__ import annotations

import json
import sys

SHIPMENTS = {
    "SHP-1001": {"id": "SHP-1001", "dest": "上海", "carrier": "顺丰", "status": "in_transit", "eta": "2026-08-25"},
    "SHP-1002": {"id": "SHP-1002", "dest": "深圳", "carrier": "京东", "status": "delayed", "eta": "2026-08-28"},
    "SHP-1003": {"id": "SHP-1003", "dest": "常州", "carrier": "顺丰", "status": "delivered", "eta": "2026-08-22"},
}

TOOLS = [
    {
        "name": "list_shipments",
        "description": "列出运单，可按 status 过滤（in_transit / delayed / delivered）",
        "inputSchema": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "可选，按状态过滤"}},
        },
    },
    {
        "name": "get_shipment",
        "description": "按运单号取详情",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "reroute_shipment",
        "description": "把运单改派给另一家承运商（有后果的动作，会触发人工审批）",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}, "carrier": {"type": "string"}},
            "required": ["id", "carrier"],
        },
    },
]


def send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def text(mid, payload, is_error: bool = False) -> None:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    result = {"content": [{"type": "text", "text": body}]}
    if is_error:
        result["isError"] = True
    send({"jsonrpc": "2.0", "id": mid, "result": result})


def call(mid, name: str, args: dict) -> None:
    if name == "list_shipments":
        status = args.get("status")
        rows = [s for s in SHIPMENTS.values() if not status or s["status"] == status]
        return text(mid, rows)
    if name == "get_shipment":
        row = SHIPMENTS.get(str(args.get("id", "")))
        return text(mid, row) if row else text(mid, f"运单不存在：{args.get('id')}", True)
    if name == "reroute_shipment":
        row = SHIPMENTS.get(str(args.get("id", "")))
        if not row:
            return text(mid, f"运单不存在：{args.get('id')}", True)
        row["carrier"] = str(args.get("carrier", ""))
        row["status"] = "in_transit"
        return text(mid, {"ok": True, "shipment": row})
    return text(mid, f"未知工具：{name}", True)


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:  # 通知（notifications/initialized 之类）不需要回
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "shipments", "version": "0.1"},
            }})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            call(mid, params.get("name", ""), params.get("arguments") or {})
        else:
            send({"jsonrpc": "2.0", "id": mid,
                  "error": {"code": -32601, "message": f"method not found: {method}"}})


if __name__ == "__main__":
    main()
