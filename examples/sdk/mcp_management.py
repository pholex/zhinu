"""Offline dynamic MCP lifecycle: python examples/sdk/mcp_management.py --demo"""
from __future__ import annotations

import argparse
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import time

from xiaoyu_agent_sdk import McpServer, Session
from common import options, text_chunk, tool_chunk

SERVER = '''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    method = message['method']
    if method == 'initialize':
        result = {'protocolVersion': message['params']['protocolVersion'], 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'demo', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'ping', 'description': 'Return pong', 'inputSchema': {'type': 'object', 'properties': {}}}]}
    else:
        result = {'content': [{'type': 'text', 'text': 'pong'}]}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        server = root / "server.py"
        server.write_text(SERVER, encoding="utf-8")
        opts = replace(options(args.demo, [[tool_chunk("use_tool", '{"tool_name":"mcp__demo__ping","tool_input":{}}')],
                                          [text_chunk("pong")]], root), approver=lambda *_: True)
        with Session(opts) as session:
            session.mcp_add(McpServer("demo", command=sys.executable, args=(str(server),)))
            deadline = time.monotonic() + 10
            while session.mcp_status()[0].state == "loading" and time.monotonic() < deadline:
                time.sleep(.01)
            if session.mcp_status()[0].state != "ready":
                raise RuntimeError("Demo MCP did not become ready")
            print(session.run("Call the MCP ping tool.").text)
            print("Stopped:", session.mcp_manage("demo", "stop"))
            session.mcp_manage("demo", "start")
            print("Removed:", session.mcp_manage("demo", "remove"))


if __name__ == "__main__":
    main()
