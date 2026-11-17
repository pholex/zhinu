"""Exact qixiang execution controls; durable reservation precedes new input."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from . import serve_ownership
from .serve_control_ledger import DurableControlLedger, public
from .serve_weaves import EXECUTION_EVENT

JOURNAL = "serve.qixiang.control"
MAX_OPERATIONS = 1000


class ControlLedger(DurableControlLedger):
    def __init__(self):
        super().__init__(journal=JOURNAL, snapshot_event=EXECUTION_EVENT, payload_field="execution",
                         identity_fields=("batch_id", "item_id", "execution_id", "kind"),
                         warning="条目操作回执保存失败或不完整；新消息已停用，精确停止仍可请求。")

    def observe_snapshot(self, payload):
        if not hasattr(self, "_revisions"):
            self._revisions = {}
        if (not isinstance(payload, dict) or type(payload.get("revision")) is not int
                or not isinstance(payload.get("execution_id"), str)
                or not isinstance(payload.get("controls", []), (list, tuple))):
            self.fail()
            return
        eid, revision = payload["execution_id"], payload["revision"]
        if revision <= self._revisions.get(eid, 0):
            return
        self._revisions[eid] = revision
        self.observe([control for control in payload.get("controls", []) if isinstance(control, dict)
                      and all(control.get(key) == payload.get(key) for key in ("batch_id", "item_id", "execution_id"))])


def persistence_reason(session):
    if problem := serve_ownership.reason(session):
        return problem
    log = session.agent.session_log
    if not (session.cfg.persist and log and log.complete and log.locked
            and session.weaves.integrity and session.weave_controls.integrity
            and not session.history_warning):
        return "缺少可靠持久化记录；不能发送新消息，停止请求无法保证重启后可核对。"
    return ""


def current(session, batch_id, item_id, execution_id):
    batch = session.weaves.batches.get(batch_id)
    if not batch or not any(item.get("id") == item_id for item in batch["items"]):
        return None
    controller = getattr(session.agent, "_weave_controls", None)
    if controller is None:
        return None
    value = controller.snapshot(batch_id, item_id, execution_id)
    return value if value and value.get("execution_id") == execution_id and value.get("live") else None


def enrich(session, snapshots):
    fault = persistence_reason(session)
    for batch in snapshots:
        for item in batch["items"]:
            eid = item.get("execution_id")
            execution = current(session, batch["batch_id"], item["id"], eid) if eid else None
            item["live"] = bool(execution)
            reason = ("会话正在关闭。" if session.closing else
                      "该条目没有当前可控制的执行实例。" if not execution else
                      "已请求停止，等待当前执行实例结束。" if execution.get("stop_requested") else "")
            item["controls"] = {"can_message": not (reason or fault), "can_stop": not reason,
                                "reason": reason or fault, "message_reason": reason or fault,
                                "stop_reason": reason}
            item["operations"] = [public(op) for op in session.weave_controls.operations.values()
                                  if op["batch_id"] == batch["batch_id"] and op["item_id"] == item["id"]
                                  and op["execution_id"] == eid]


def register_routes(app, guard, pick):
    from fastapi import Body, Header, HTTPException
    from fastapi.responses import JSONResponse

    async def act(session_id, batch_id, item_id, body, key, kind):
        session = pick(session_id)
        ledger = session.weave_controls
        if not key or len(key) > 200:
            raise HTTPException(400, "需要有效 Idempotency-Key（最多 200 字符）。")
        expected = {"execution_id", "text"} if kind == "message" else {"execution_id"}
        if set(body) != expected or not isinstance(body.get("execution_id"), str) or not body["execution_id"]:
            raise HTTPException(400, "必须提供精确 execution_id，且不能含额外字段。")
        if kind == "message" and (not isinstance(body["text"], str) or not body["text"].strip() or len(body["text"]) > 8000):
            raise HTTPException(400, "消息须为 1–8000 字符的非空文本。")
        digest = hashlib.sha256(json.dumps([batch_id, item_id, kind, body], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        prior = ledger.operations.get(ledger.keys.get(key, ""))
        if prior:
            if prior["digest"] != digest:
                raise HTTPException(409, "该幂等键已用于不同条目操作。")
            return JSONResponse({"operation": public(prior), "duplicate": True})
        if session.closing:
            raise HTTPException(409, "会话正在关闭。")
        execution = current(session, batch_id, item_id, body["execution_id"])
        if not execution:
            raise HTTPException(409, "该条目执行实例已结束、已换代或不属于当前会话。")
        if execution.get("stop_requested"):
            raise HTTPException(409, "该实例已请求停止，等待实际结束。")
        warning = persistence_reason(session)
        if warning and kind == "message":
            raise HTTPException(409, warning)
        if kind == "message" and len(ledger.operations) >= MAX_OPERATIONS:
            raise HTTPException(409, "本会话已达到 1000 条条目操作上限。")
        operation = {"operation_id": uuid.uuid4().hex, "key": key, "digest": digest, "kind": kind,
                     "batch_id": batch_id, "item_id": item_id, "execution_id": body["execution_id"],
                     "text": body.get("text", ""), "status": "reserved", "created_at": time.time(),
                     "updated_at": time.time(), "reason": "", "persisted": not bool(warning), "warning": warning}
        try:
            ledger.save(operation, session.agent.session_log)
        except OSError:
            operation.update(persisted=False, warning=ledger.warning)
            ledger.operations[operation["operation_id"]].update(persisted=False, warning=ledger.warning)
            if kind != "stop":
                raise HTTPException(503, ledger.warning) from None
        try:
            result = session.agent._weave_controls.control(kind, batch_id=batch_id, item_id=item_id,
                        execution_id=body["execution_id"], operation_id=operation["operation_id"], text=body.get("text", ""))
            ledger.observe([result])
            operation = ledger.operations[operation["operation_id"]]
        except ValueError as exc:
            operation.update(status="failed", reason=str(exc), updated_at=time.time())
        except Exception:
            operation.update(status="unknown", reason="内核操作未返回确定结果；不会自动重放。", updated_at=time.time())
        try:
            ledger.save(operation, session.agent.session_log)
        except OSError:
            # A precise stop is still useful with a failed journal. Its live outcome
            # remains visible; persistence is never implied by an in-memory receipt.
            operation.update(persisted=False, warning=ledger.warning)
            if kind != "stop":
                operation.update(recorded_status=operation["status"], status="unknown", reason=ledger.warning)
            ledger.operations[operation["operation_id"]] = operation
        session.publish("weave.control", operation=public(operation))
        return JSONResponse({"operation": public(operation), "duplicate": False}, status_code=202)

    @app.post("/session/{session_id}/weaves/{batch_id}/items/{item_id}/messages", dependencies=guard, operation_id="message_weave_item")
    async def message(session_id: str, batch_id: str, item_id: str, body: dict = Body(...), idempotency_key: str = Header(default="")):
        return await act(session_id, batch_id, item_id, body, idempotency_key, "message")

    @app.post("/session/{session_id}/weaves/{batch_id}/items/{item_id}/stop", dependencies=guard, operation_id="stop_weave_item")
    async def stop(session_id: str, batch_id: str, item_id: str, body: dict = Body(...), idempotency_key: str = Header(default="")):
        return await act(session_id, batch_id, item_id, body, idempotency_key, "stop")

    @app.get("/session/{session_id}/weave-operations/{operation_id}", dependencies=guard, operation_id="get_weave_operation")
    async def receipt(session_id: str, operation_id: str):
        operation = pick(session_id).weave_controls.operations.get(operation_id)
        if not operation:
            raise HTTPException(404, "没有该条目操作。")
        return {"operation": public(operation)}
