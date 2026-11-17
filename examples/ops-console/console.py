"""运营控制台（宿主应用）——把小羽嵌进自己的产品的最小样本，纯标准库。

四步，对应平台化的四个抓手（见 docs/platform.md）：
1. **agent 对象**：一次声明，带上宿主自有的 MCP server（shipments_mcp.py）与人格；
   版本钉定，后续改配置不影响在跑的会话；
2. **会话 + 异步提交**：prompt_async 立刻返回，控制台自己轮询；
3. **审批走宿主 UI**：有后果的工具调用挂起在 waiting_for_approval，这里在终端
   问操作员——真实宿主就是它自己的审批弹窗 / 工单流；
4. **结构化收尾**：output_schema 让模型按 schema 交回对象，控制台直接渲染，
   不解析自然语言。

先起服务（stdio server 要 --agent-mcp all；只用远端 server 时 http 档即可）：

    xiaoyu serve --workspace /tmp/ops-ws --agent-mcp all --mode auto

然后：

    python examples/ops-console/console.py "把所有延误的运单改派给顺丰"
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8420"
HERE = Path(__file__).resolve().parent

REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "description": "实际执行了的动作",
            "items": {
                "type": "object",
                "properties": {
                    "shipment_id": {"type": "string"},
                    "action": {"type": "string"},
                    "result": {"type": "string", "enum": ["done", "denied", "failed"]},
                },
                "required": ["shipment_id", "action", "result"],
            },
        },
        "summary": {"type": "string", "description": "一句话给操作员看的总结"},
    },
    "required": ["actions", "summary"],
}


def api(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def ensure_agent() -> str:
    """找到或创建本控制台的 agent 对象。名字不唯一，按名字找最新一个。"""
    for item in api("GET", "/agent")["agents"]:
        if item["name"] == "ops-console" and not item.get("archived"):
            return item["agent_id"]
    created = api("POST", "/agent", {
        "name": "ops-console",
        "config": {
            "append_system_prompt": (
                "你是运营控制台里的运单助手。只通过 shipments 工具查询和改派运单，"
                "不碰文件系统。改派前先查清状态；每个动作的结果如实记录。"
            ),
            "budget": {"tokens": 200_000},
            "mcp_servers": {
                "shipments": {"command": sys.executable, "args": [str(HERE / "shipments_mcp.py")]},
            },
        },
    })
    print(f"[控制台] 创建 agent {created['agent_id']} v{created['version']}")
    return created["agent_id"]


def handle_approvals(sid: str, status: dict) -> None:
    """宿主 UI 的审批：这里是终端问操作员，真实宿主换成自己的弹窗。"""
    for item in status.get("pending_approvals", []):
        print(f"\n[审批] 模型想调用 {item['tool']}：{json.dumps(item.get('args'), ensure_ascii=False)}")
        answer = input("       放行？[y/N] ").strip().lower()
        decision = {"request_id": item["request_id"], "decision": "allow" if answer == "y" else "deny"}
        if answer != "y":
            decision["reason"] = "操作员拒绝了这次改派"
        api("POST", f"/session/{sid}/permissions", decision)


def main() -> int:
    task = " ".join(sys.argv[1:]) or "列出所有延误的运单，给出处理建议"
    agent_id = ensure_agent()
    sid = api("POST", "/session", {"agent": agent_id})["session_id"]
    print(f"[控制台] 会话 {sid}，任务：{task}")
    api("POST", f"/session/{sid}/prompt_async", {"text": task, "output_schema": REPORT_SCHEMA})

    cursor = 1
    while True:
        status = api("GET", f"/session/{sid}/status")
        #  顺手把进度事件打出来（真实宿主喂给自己的活动面板）
        events = api("GET", f"/session/{sid}/events?from={cursor}&limit=200")
        for ev in events["events"]:
            if ev["kind"] == "tool.pending":
                print(f"  · 调用 {ev.get('name')}")
            elif ev["kind"] == "text.delta":
                sys.stdout.write(ev.get("text", "")); sys.stdout.flush()
        cursor = events["next"]
        if status["detail"] == "waiting_for_approval":
            handle_approvals(sid, status)
            continue
        if status["status"] != "running":
            break
        time.sleep(0.5)

    print()
    if status["status"] == "error":
        print(f"[控制台] 失败：{status['error']}")
        return 1
    report = (status.get("last_result") or {}).get("output")
    if report is None:
        print("[控制台] 模型没有交回结构化报告；正文：", (status.get("last_result") or {}).get("text"))
        return 1
    print(f"[报告] {report['summary']}")
    for action in report["actions"]:
        print(f"  {action['shipment_id']:>9}  {action['action']:<20} {action['result']}")
    api("DELETE", f"/session/{sid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
