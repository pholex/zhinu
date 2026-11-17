"""Durable, generation-specific member controls; recovery never replays effects."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

JOURNAL = "serve.chenshu.control"
MAX_OPERATIONS = 1000
_FINAL = {"accepted", "not_consumed", "stopped", "failed"}


from . import serve_ownership
from .serve_control_ledger import DurableControlLedger, public


class ControlLedger(DurableControlLedger):
    def __init__(self):
        super().__init__(journal=JOURNAL, snapshot_event="serve.chenshu.updated", payload_field="fleet",
                         identity_fields=("board_id", "member_name", "member_run_id", "kind"),
                         warning="成员操作回执保存失败或不完整；已停用新操作，请勿盲目重试。",
                         status_map={"requested": "stop_requested", "settled": "stopped"}, final=_FINAL)


def live_reason(session, board):
    if session.closing:
        return "会话正在关闭。"
    if not board or board.get("recovered") or not board.get("live"):
        return "历史看板无法控制；需要本进程的有效成员。"
    runtime = getattr(session.agent, "chenshu", None)
    if runtime is None or getattr(runtime, "_board_id", None) != board["board_id"]:
        return "该运行时已不属于当前会话。"
    return ""


def disabled_reason(session, board):
    if problem := serve_ownership.reason(session):
        return problem
    log = session.agent.session_log
    if not (session.cfg.persist and log and log.complete and session.fleets.integrity
            and session.fleet_controls.integrity and not session.history_warning):
        return "缺少可靠的持久化记录，不能发送成员操作。"
    return live_reason(session, board)


def enrich(session, snapshots):
    for board in snapshots:
        reason = disabled_reason(session, board)
        for member in board["members"]:
            member_reason = reason or ("成员已结束或运行状态未知。" if member.get("execution_state") not in {"running", "starting"} else "")
            stopped = bool(member.get("stop_requested"))
            member["controls"] = {"can_message": not member_reason and not stopped,
                                  "can_stop": not live_reason(session, board) and member.get("execution_state") in {"running", "starting"} and not stopped,
                                  "reason": member_reason or ("已请求停止，等待成员结束。" if stopped else "")}
            member["operations"] = [public(op) for op in session.fleet_controls.operations.values()
                                    if op["board_id"] == board["board_id"] and op["member_name"] == member["name"]
                                    and op["member_run_id"] == member.get("run_id")]


def register_routes(app, guard, pick):
    from fastapi import Body, Header, HTTPException
    from fastapi.responses import JSONResponse

    async def act(session_id, board_id, member_name, body, key, kind):
        session = pick(session_id)
        ledger = session.fleet_controls
        if not key or len(key) > 200:
            raise HTTPException(400, "需要有效 Idempotency-Key（最多 200 字符）。")
        expected = {"run_id", "text"} if kind == "message" else {"run_id"}
        if set(body) != expected or not isinstance(body.get("run_id"), str) or not body["run_id"]:
            raise HTTPException(400, "必须提供精确的 run_id，且不能含额外字段。")
        if kind == "message" and (not isinstance(body["text"], str) or not body["text"].strip() or len(body["text"]) > 8000):
            raise HTTPException(400, "消息须为 1–8000 字符的非空文本。")
        digest = hashlib.sha256(json.dumps([board_id, member_name, kind, body], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        prior = ledger.operations.get(ledger.keys.get(key, ""))
        if prior:
            if prior["digest"] != digest:
                raise HTTPException(409, "该幂等键已用于不同的成员操作。")
            return JSONResponse({"operation": public(prior), "duplicate": True})
        board = session.fleets.fleets.get(board_id)
        warning = disabled_reason(session, board)
        blocked = live_reason(session, board) if kind == "stop" else warning
        if blocked:
            raise HTTPException(409, blocked)
        if kind == "stop":
            member = next((value for value in board["members"] if value.get("name") == member_name
                           and value.get("run_id") == body["run_id"]), None)
            if not member or member.get("execution_state") not in {"starting", "running"} or member.get("stop_requested"):
                raise HTTPException(409, "该精确成员已结束、换代或请求停止。")
        if kind == "stop" and any(operation.get("kind") == "stop"
                and operation.get("board_id") == board_id and operation.get("member_name") == member_name
                and operation.get("member_run_id") == body["run_id"]
                and operation.get("status") != "failed" for operation in ledger.operations.values()):
            raise HTTPException(409, "该精确成员已有停止回执，请用原幂等键核对。")
        if kind == "message" and len(ledger.operations) >= MAX_OPERATIONS:
            raise HTTPException(409, "本会话已达到 1000 条成员操作上限。")
        operation = {"operation_id": uuid.uuid4().hex, "key": key, "digest": digest, "kind": kind,
                     "board_id": board_id, "member_name": member_name, "member_run_id": body["run_id"],
                     "run_id": body["run_id"], "text": body.get("text", ""), "status": "reserved",
                     "created_at": time.time(), "updated_at": time.time(), "reason": "",
                     "persisted": not bool(warning), "warning": warning}
        try:
            ledger.save(operation, session.agent.session_log)
        except OSError as exc:
            operation.update(persisted=False, warning=ledger.warning)
            ledger.operations[operation["operation_id"]].update(persisted=False, warning=ledger.warning)
            if kind != "stop":
                raise HTTPException(503, str(exc)) from None
        try:
            result = session.agent.chenshu.member_control(kind, board_id=board_id, name=member_name,
                        run_id=body["run_id"], operation_id=operation["operation_id"], text=body.get("text", ""))
            ledger.observe([result])
            operation = ledger.operations[operation["operation_id"]]
        except ValueError as exc:
            operation.update(status="failed", reason=str(exc), updated_at=time.time())
        except Exception:
            operation.update(status="unknown", reason="内核操作未返回确定结果；不会自动重放。", updated_at=time.time())
        try:
            ledger.save(operation, session.agent.session_log)
        except OSError:
            # The operation may have happened. Keep explicit uncertainty in the receipt.
            operation.update(persisted=False, warning=ledger.warning)
            if kind != "stop":
                operation.update(recorded_status=operation["status"], status="unknown", reason=ledger.warning)
            ledger.operations[operation["operation_id"]] = operation
        session.publish("fleet.control", operation=public(operation))
        return JSONResponse({"operation": public(operation), "duplicate": False}, status_code=202)

    @app.post("/session/{session_id}/fleets/{board_id}/members/{member_name}/messages", dependencies=guard, operation_id="message_fleet_member")
    async def message(session_id: str, board_id: str, member_name: str, body: dict = Body(...), idempotency_key: str = Header(default="")):
        return await act(session_id, board_id, member_name, body, idempotency_key, "message")

    @app.post("/session/{session_id}/fleets/{board_id}/members/{member_name}/stop", dependencies=guard, operation_id="stop_fleet_member")
    async def stop(session_id: str, board_id: str, member_name: str, body: dict = Body(...), idempotency_key: str = Header(default="")):
        return await act(session_id, board_id, member_name, body, idempotency_key, "stop")

    @app.get("/session/{session_id}/fleet-operations/{operation_id}", dependencies=guard, operation_id="get_fleet_operation")
    async def receipt(session_id: str, operation_id: str):
        operation = pick(session_id).fleet_controls.operations.get(operation_id)
        if not operation:
            raise HTTPException(404, "没有该成员操作。")
        return {"operation": public(operation)}
