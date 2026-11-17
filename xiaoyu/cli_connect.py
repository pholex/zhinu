"""CLI attachment to an existing local desktop service; no agent or log ownership."""
from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
from typing import Any
from urllib.parse import urlencode, urlsplit
import uuid

PAIR_LIMIT = 16384
HTTP_LIMIT = 8 * 1024 * 1024


class ConnectError(ValueError):
    pass


class UnknownSubmission(ConnectError):
    """The request may have taken effect. Its in-memory pending key is retained."""


@dataclass(frozen=True)
class Pairing:
    base_url: str
    token: str = field(repr=False)
    instance_id: str
    workspace: str


@dataclass
class Pending:
    path: str
    body: dict[str, Any] = field(repr=False)
    key: str
    retryable: bool
    state: str = "sending"


def canonical_workspace(workspace: Path) -> Path:
    path = workspace.expanduser().resolve()
    if not path.is_dir():
        raise ConnectError("工作区不存在或不是目录。")
    return path


def pairing_socket_path(workspace: Path) -> Path:
    if not hasattr(os, "getuid"):
        raise ConnectError("当前平台不支持本机桌面配对。")
    digest = hashlib.sha256(str(canonical_workspace(workspace)).encode("utf-8")).hexdigest()[:24]
    return Path("/tmp") / f"xiaoyu-desktop-{os.getuid()}" / f"{digest}.sock"


socket_path = pairing_socket_path


def validate_pairing_socket(path: Path) -> os.stat_result:
    try:
        parent, leaf = path.parent.lstat(), path.lstat()
    except OSError:
        raise ConnectError("未发现该工作区的桌面配对服务；请先在羽案中启用 CLI 连接。") from None
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
            or not stat.S_ISSOCK(leaf.st_mode) or leaf.st_uid != os.getuid()
            or stat.S_IMODE(leaf.st_mode) != 0o600):
        raise ConnectError("配对通道属主或权限不正确，未读取连接凭据。")
    return leaf


def _origin(value: Any) -> str:
    if not isinstance(value, str):
        raise ConnectError("配对响应缺少有效本机地址。")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        raise ConnectError("配对服务地址格式无效。") from None
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"}
            or not port or url.username is not None or url.password is not None
            or url.path not in {"", "/"} or url.query or url.fragment
            or any(ord(char) < 33 for char in value)):
        raise ConnectError("配对服务地址必须是没有凭据、路径和查询参数的本机 HTTP 地址。")
    return value.rstrip("/")


def pair(workspace: Path) -> Pairing:
    root = canonical_workspace(workspace)
    path = pairing_socket_path(root)
    before = validate_pairing_socket(path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(5)
            channel.connect(str(path))
            after = validate_pairing_socket(path)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise ConnectError("配对通道已变化，请重新连接。")
            request = {"protocol": 1, "client": "xiaoyu-cli", "workspace": str(root)}
            channel.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            data = bytearray()
            while b"\n" not in data:
                chunk = channel.recv(min(4096, PAIR_LIMIT + 1 - len(data)))
                if not chunk:
                    raise ConnectError("配对服务没有返回完整响应。")
                data.extend(chunk)
                if len(data) > PAIR_LIMIT:
                    raise ConnectError("配对响应过大。")
    except (OSError, TimeoutError):
        raise ConnectError("配对连接失败；请确认羽案仍在运行且已允许 CLI 连接。") from None
    try:
        response = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8", errors="replace"))
    except ValueError:
        raise ConnectError("配对响应不是有效 JSON。") from None
    if not isinstance(response, dict) or response.get("protocol") != 1 or "error" in response:
        raise ConnectError("配对被拒绝或协议不兼容。")
    if response.get("workspace") != str(root):
        raise ConnectError("配对工作区不匹配。")
    token, instance = response.get("token"), response.get("instance_id")
    if (not isinstance(token, str) or not 1 <= len(token) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in token)
            or not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", instance)):
        raise ConnectError("配对凭据或服务实例无效。")
    return Pairing(_origin(response.get("base_url")), token, instance, str(root))


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,200}", value):
        raise ConnectError("会话、审批或问题标识格式无效。")
    return value


class ConnectClient:
    def __init__(self, pairing: Pairing):
        self.pairing = pairing
        self.pending: Pending | None = None

    def request(self, method: str, path: str, body: dict | None = None, key: str = "") -> Any:
        # No proxy environment, redirects, arbitrary URLs, or credential-bearing errors.
        if not path.startswith("/") or path.startswith("//") or any(c in path for c in "\r\n#\\"):
            raise ConnectError("无效的服务请求路径。")
        url = urlsplit(_origin(self.pairing.base_url))
        connection = http.client.HTTPConnection(url.hostname, url.port, timeout=30)
        headers = {"Authorization": f"Bearer {self.pairing.token}", "Accept": "application/json",
                   "X-Xiaoyu-Instance": self.pairing.instance_id}
        payload = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if key:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", key):
                raise ConnectError("幂等键格式无效。")
            headers["Idempotency-Key"] = key
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read(HTTP_LIMIT + 1)
            if len(data) > HTTP_LIMIT:
                if method != "GET":
                    raise UnknownSubmission("写入响应过大，结果未知；不会自动重发。")
                raise ConnectError("服务响应过大；请缩小读取范围。")
            if not 200 <= response.status < 300:
                if (response.status >= 500 or 300 <= response.status < 400) and method != "GET":
                    raise UnknownSubmission("服务未返回确定写入结果；不会自动重发，请核对状态与原幂等键。")
                raise ConnectError(f"服务返回 HTTP {response.status}；请刷新会话状态后核对操作。")
            try:
                return json.loads(data.decode("utf-8", errors="replace"))
            except ValueError:
                if method != "GET":
                    raise UnknownSubmission("写入响应无法解析，结果未知；不会自动重发。") from None
                raise ConnectError("服务响应不是有效 JSON。") from None
        except (OSError, http.client.HTTPException):
            if method != "GET":
                raise UnknownSubmission("连接中断，写入结果未知；不会自动重发，请保留原幂等键核对。") from None
            raise ConnectError("读取服务失败；请检查桌面连接。") from None
        finally:
            connection.close()

    def check_instance(self, expected: str = "") -> dict:
        health = self.request("GET", "/health")
        if (not isinstance(health, dict) or health.get("instance_id") != self.pairing.instance_id
                or health.get("root") != self.pairing.workspace
                or (expected and expected != self.pairing.instance_id)):
            raise ConnectError("服务实例或工作区已变化；没有提交操作。")
        return health

    def write(self, path: str, body: dict, *, key: str = "", retryable: bool = False) -> Any:
        if self.pending is not None and self.pending.state == "unknown":
            raise ConnectError("已有结果未知的提交；先核对原请求，不能覆盖它。")
        self.pending = Pending(path, copy.deepcopy(body), key, retryable)
        return self._submit_pending()

    def _submit_pending(self) -> Any:
        pending = self.pending
        if pending is None:
            raise ConnectError("没有待核对的提交。")
        try:
            result = self.request("POST", pending.path, pending.body, pending.key)
        except UnknownSubmission:
            pending.state = "unknown"
            raise
        except ConnectError:
            pending.state = "rejected"
            raise
        pending.state = "received"
        return result

    def retry_pending(self) -> Any:
        """Explicit caller action only; never used automatically by connect_command."""
        if not self.pending or self.pending.state != "unknown" or not self.pending.retryable or not self.pending.key:
            raise ConnectError("该提交不能安全重试，请读取状态核对。")
        self.check_instance()
        return self._submit_pending()

    def history(self, sid: str):
        cursor = 0
        while True:
            page = self.request("GET", f"/session/{_identifier(sid)}/history?cursor={cursor}&limit=100")
            if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
                raise ConnectError("历史响应结构无效，已停止读取。")
            yield page
            next_cursor = page.get("next_cursor")
            if next_cursor is None:
                return
            if type(next_cursor) is not int or next_cursor <= cursor:
                raise ConnectError("历史分页游标无效，已停止读取。")
            cursor = next_cursor

    def watch(self, sid: str, cursor: int = 1):
        while True:
            self.check_instance()
            page = self.request("GET", f"/session/{_identifier(sid)}/events?" + urlencode({"from": cursor, "wait": 20}))
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise ConnectError("事件响应结构无效，已停止监听。")
            next_cursor = page.get("next")
            if type(next_cursor) is not int or next_cursor < cursor:
                raise ConnectError("事件游标无效，已停止监听。")
            first_seq = page.get("first_seq", cursor)
            if type(first_seq) is not int or first_seq < 1:
                raise ConnectError("事件起始游标无效，已停止监听。")
            if first_seq > cursor:
                yield {"warning": "部分实时事件已超出保留窗口；完整正文请重新读取 history。"}
            yield page
            cursor = next_cursor


def connect_command(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu connect", description="连接已运行的羽案会话；不会新建 agent 或接管日志。")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--session")
    parser.add_argument("--instance", default="", help="仅允许此前观察到的服务实例")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--history", action="store_true")
    actions.add_argument("--status", action="store_true")
    actions.add_argument("--watch", action="store_true")
    actions.add_argument("--prompt")
    actions.add_argument("--steer")
    actions.add_argument("--abort", action="store_true")
    actions.add_argument("--approve", metavar="REQUEST_ID")
    actions.add_argument("--answer", metavar="QUESTION_ID")
    parser.add_argument("--turn", type=int)
    parser.add_argument("--decision", choices=("allow", "deny"))
    parser.add_argument("--reason", default="")
    parser.add_argument("--answers-json", help="明确的 answers 数组；- 表示从标准输入读取")
    parser.add_argument("--idempotency-key", default="")
    args = parser.parse_args(argv)
    client = None
    try:
        action = any((args.history, args.status, args.watch, args.prompt is not None, args.steer is not None,
                      args.abort, args.approve, args.answer))
        if action and not args.session:
            raise ConnectError("该操作必须提供 --session。")
        if (args.steer is not None or args.abort) and (args.turn is None or args.turn < 1):
            raise ConnectError("插话和停止必须提供此前观察到的 --turn。")
        if args.approve and not args.decision:
            raise ConnectError("审批必须明确 --decision allow 或 deny，仅本次有效。")
        if args.answer and args.answers_json is None:
            raise ConnectError("回答必须提供 --answers-json；不会猜测问题答案。")
        if args.decision and not args.approve or args.answers_json is not None and not args.answer:
            raise ConnectError("审批决定或答案必须与对应操作一起提供。")
        if args.idempotency_key and args.prompt is None and not args.answer:
            raise ConnectError("幂等键仅用于提交任务或回答问题。")
        if args.turn is not None and args.steer is None and not args.abort:
            raise ConnectError("--turn 仅用于精确插话或停止。")
        if args.reason and not args.approve:
            raise ConnectError("--reason 仅用于审批决定。")
        client = ConnectClient(pair(args.workspace))
        client.check_instance(args.instance)
        def output(value):
            # Neither normal output nor accidental service echoes reveal the pairing token.
            encoded_token = json.dumps(client.pairing.token, ensure_ascii=False)[1:-1]
            print(json.dumps(value, ensure_ascii=False).replace(encoded_token, "[REDACTED]"))
        if not args.session:
            output(client.request("GET", "/session"))
            return 0
        sid = _identifier(args.session)
        prefix = f"/session/{sid}"
        if args.prompt is not None:
            if not args.prompt.strip():
                raise ConnectError("任务不能为空。")
            key = args.idempotency_key or uuid.uuid4().hex
            output({"submission_key": key})
            output(client.write(prefix + "/prompt_async", {"text": args.prompt}, key=key, retryable=True))
        elif args.steer is not None or args.abort:
            body = {"turn": args.turn}
            if args.steer is not None:
                if not args.steer.strip():
                    raise ConnectError("插话不能为空。")
                body["text"] = args.steer
            output(client.write(prefix + ("/steer" if args.steer is not None else "/abort"), body))
        elif args.approve:
            output(client.write(prefix + "/permissions", {"request_id": _identifier(args.approve),
                                "decision": args.decision, "reason": args.reason}))
        elif args.answer:
            raw = sys.stdin.read(131073) if args.answers_json == "-" else args.answers_json
            if len(raw) > 131072:
                raise ConnectError("答案输入过大。")
            try:
                answers = json.loads(raw)
            except ValueError:
                raise ConnectError("答案必须是有效 JSON 数组。") from None
            if not isinstance(answers, list) or not 1 <= len(answers) <= 4 or not all(isinstance(row, dict) for row in answers):
                raise ConnectError("答案必须包含 1–4 项明确回答。")
            key = args.idempotency_key or uuid.uuid4().hex
            output({"submission_key": key})
            output(client.write(prefix + f"/questions/{_identifier(args.answer)}/answers",
                                {"answers": answers, "idempotency_key": key}, key=key, retryable=True))
        elif args.status:
            output(client.request("GET", prefix + "/status"))
        else:
            cursor = None
            for page in client.history(sid):
                if cursor is None:
                    cursor = page.get("live_from_seq", 1)
                    if type(cursor) is not int or cursor < 1:
                        raise ConnectError("历史事件边界无效，已停止读取。")
                output(page)
            if args.watch:
                for page in client.watch(sid, cursor or 1):
                    output(page)
        return 0
    except KeyboardInterrupt:
        # Detaching the observer never aborts the model or closes the service session.
        return 130
    except ConnectError as exc:
        message = str(exc)
        if client is not None:
            message = message.replace(client.pairing.token, "[REDACTED]")
        print(message, file=sys.stderr)
        return 3 if isinstance(exc, UnknownSubmission) else 2
