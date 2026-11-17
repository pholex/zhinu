"""Application-owned drafts and submission receipts, separate from model history."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
import secrets
import tempfile
import threading

from .app_ownership import OwnershipUnavailable, require_owner

MAX_BYTES = 2 * 1024 * 1024
VIEW_ID_PATTERN = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
LEGACY_OWNER = "view:legacy-owner"


def validate(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {"revision", "drafts", "outbox"}:
        raise ValueError("工作台状态格式不正确")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ValueError("工作台状态版本不正确")
    drafts, outbox = value["drafts"], value["outbox"]
    if not isinstance(drafts, dict) or len(drafts) - (LEGACY_OWNER in drafts) > 500:
        raise ValueError("草稿数量超出限制")
    if LEGACY_OWNER in drafts and (not isinstance(drafts[LEGACY_OWNER], str) or not re.fullmatch(VIEW_ID_PATTERN, drafts[LEGACY_OWNER])):
        raise ValueError("旧版草稿迁移归属不正确")
    if any(not isinstance(k, str) or not k or len(k) > (242 if re.match(r"view:" + VIEW_ID_PATTERN + ":", k) else 200)
           or not isinstance(v, str) or len(v) > 100000 for k, v in drafts.items()):
        raise ValueError("草稿格式或长度不正确")
    if not isinstance(outbox, dict) or len(outbox) > 500:
        raise ValueError("待提交回执数量超出限制")
    fields = {"kind", "sid", "path", "body", "instance", "seq", "key", "state", "result", "message", "qid", "draftKey", "draftText", "contestId", "view_id", "workflow"}
    for key, item in outbox.items():
        if (not isinstance(key, str) or len(key) > 500 or not isinstance(item, dict)
                or set(item) - fields or item.get("kind") not in {"create", "prompt", "steer", "abort", "approval", "answer", "weave", "contest", "selection", "fleet_message", "fleet_stop", "fleet_command", "weave_message", "weave_stop"}
                or not isinstance(item.get("key"), str) or not isinstance(item.get("body"), dict)):
            raise ValueError("待提交回执格式不正确")
        if ("draftKey" in item and (not isinstance(item["draftKey"], str) or len(item["draftKey"]) > 200)
                or "draftText" in item and (not isinstance(item["draftText"], str) or len(item["draftText"]) > 100000)):
            raise ValueError("待提交草稿格式或长度不正确")
        if "workflow" in item and (item.get("kind") != "create" or not isinstance(item["workflow"], str)
                                   or item["workflow"] not in {"weave", "contest", "fleet"}):
            raise ValueError("协作会话工作方式不正确")
        if "view_id" in item and (not isinstance(item["view_id"], str) or not re.fullmatch(VIEW_ID_PATTERN, item["view_id"])):
            raise ValueError("待提交窗口身份不正确")
        if "contestId" in item and (not isinstance(item["contestId"], str) or not item["contestId"] or len(item["contestId"]) > 256):
            raise ValueError("待提交采纳来源不正确")
    return value


class StateConflict(Exception):
    pass


class DesktopState:
    def __init__(self, path: Path, *, write_guard=None):
        self.path = path
        self.write_guard = write_guard or (lambda: None)
        self.lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as stream:
                text = stream.read(MAX_BYTES + 1)
            if len(text.encode("utf-8")) > MAX_BYTES:
                raise ValueError("工作台状态文件过大")
            return validate(json.loads(text))
        except FileNotFoundError:
            return {"revision": 0, "drafts": {}, "outbox": {}}

    def read(self) -> dict:
        with self.lock:
            return self._read()

    def save(self, value: dict) -> dict:
        validate(value)
        with self.lock:
            current = self._read()
            if current["revision"] != value["revision"]:
                raise StateConflict()
            saved = {**copy.deepcopy(value), "revision": current["revision"] + 1}
            encoded = json.dumps(saved, ensure_ascii=False).encode("utf-8")
            if len(encoded) > MAX_BYTES:
                raise ValueError("工作台状态超出容量限制")
            self.write_guard()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".desktop-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "wb") as stream:
                    self.write_guard()
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.write_guard()
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return saved


def install_state_routes(app, path: Path, token: str) -> None:
    # Imported here so the application state store remains usable without web extras.
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse

    store = DesktopState(path, write_guard=lambda: require_owner(app))

    def authorize(request):
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not secrets.compare_digest(supplied, ("Bearer " + token).encode("utf-8")):
            raise HTTPException(401, "需要本机应用令牌")

    async def read_state(request):
        authorize(request)
        try:
            return JSONResponse(store.read(), headers={"Cache-Control": "no-store"})
        except (OSError, ValueError):
            raise HTTPException(503, "无法读取工作台状态；原文件已保留") from None

    async def save_state(request):
        authorize(request)
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise HTTPException(413, "工作台状态超出容量限制")
        try:
            value = validate(json.loads(data.decode("utf-8", errors="replace")))
        except ValueError:
            raise HTTPException(422, "工作台状态格式不正确") from None
        try:
            saved = store.save(value)
        except StateConflict:
            raise HTTPException(409, "工作台状态已由其他页面更新；请重新打开界面核对") from None
        except OwnershipUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except (OSError, ValueError):
            raise HTTPException(503, "无法保存工作台状态；请保留输入并核对存储") from None
        return JSONResponse({"revision": saved["revision"]}, headers={"Cache-Control": "no-store"})

    # Resolve Request at registration without a module-level dependency on FastAPI.
    read_state.__annotations__["request"] = Request
    save_state.__annotations__["request"] = Request
    app.add_api_route("/desktop/state", read_state, methods=["GET"], include_in_schema=False)
    app.add_api_route("/desktop/state", save_state, methods=["POST"], include_in_schema=False)
