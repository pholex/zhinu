"""Read-only, session-owned views of observed orchestration events.

The query path never loads or adopts a workspace runtime. A restored observation
is historical evidence, not proof that any former worker is currently alive.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

JOURNAL_EVENT = "serve.chenshu.updated"
MAX_FLEETS = 64
_COLLECTIONS = ("missions", "members", "reviews", "findings", "activity")


class FleetView:
    def __init__(self) -> None:
        self.fleets: dict[str, dict[str, Any]] = {}
        self.warning = ""
        self.integrity = True
        self.truncated = False

    def _invalid(self, message: str) -> None:
        self.integrity = False
        self.warning = message

    def accept(self, value: dict[str, Any]) -> bool:
        board_id, revision = value.get("board_id"), value.get("revision")
        if (not isinstance(board_id, str) or not board_id or type(revision) is not int or revision < 1
                or type(value.get("active")) is not bool
                or not isinstance(value.get("source"), dict)
                or not isinstance(value.get("limits"), dict)
                or not isinstance(value.get("gaps"), list)
                or not all(isinstance(note, str) for note in value["gaps"])
                or any(not isinstance(value.get(field), list)
                       or not all(isinstance(item, dict) for item in value[field]) for field in _COLLECTIONS)):
            self._invalid("部分宸枢快照格式不可读，记录可能不完整。")
            return False
        previous = self.fleets.get(board_id)
        if previous and previous["revision"] >= revision:
            return False
        snapshot = copy.deepcopy(value)
        snapshot["source_run_id"] = value.get("source_run_id") if isinstance(value.get("source_run_id"), str) else ""
        snapshot.update(live=True, recovered=False, runtime_state="observed")
        self.fleets[board_id] = snapshot
        while len(self.fleets) > MAX_FLEETS:
            del self.fleets[next(iter(self.fleets))]
            self.truncated = True
        return True

    def record(self, event: dict[str, Any], log: Any, run_id: str) -> None:
        snapshot = {key: copy.deepcopy(value) for key, value in event.items()
                    if key not in {"kind", "session_id", "run_id", "request_id", "task_id"}}
        for field in _COLLECTIONS:
            if isinstance(snapshot.get(field), tuple):
                snapshot[field] = list(snapshot[field])
        if isinstance(snapshot.get("gaps"), tuple):
            snapshot["gaps"] = list(snapshot["gaps"])
        board_id = snapshot.get("board_id")
        previous = self.fleets.get(board_id) if isinstance(board_id, str) else None
        # Member events may arrive after their initiating round, or during a
        # different parent round. Keep the first observed origin, never rebind.
        snapshot["source_run_id"] = previous["source_run_id"] if previous else run_id
        if not self.accept(snapshot):
            return
        if log is None:
            self._invalid("宸枢快照没有持久化日志，退出服务后无法恢复。")
            return
        try:
            log.commit_event(JOURNAL_EVENT, fleet=snapshot)
        except OSError:
            self._invalid("宸枢快照保存失败；当前观察仍可查看，重启后可能不完整。")

    def restore(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.endswith("\n"):
                        self._invalid("日志尾部尚未完整写入，宸枢历史可能缺失。")
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        self._invalid("日志含损坏记录，宸枢历史可能缺失。")
                        continue
                    if not isinstance(record, dict) or record.get("event") != JOURNAL_EVENT:
                        continue
                    snapshot = record.get("fleet")
                    if isinstance(snapshot, dict):
                        self.accept(snapshot)
                    else:
                        self._invalid("部分宸枢快照格式不可读，记录可能不完整。")
        except OSError:
            self._invalid("宸枢历史日志无法读取。")
        for snapshot in self.fleets.values():
            snapshot.update(live=False, recovered=True, runtime_state="unknown_after_restart")
            for member in snapshot["members"]:
                member["recorded_thread_alive"] = member.get("thread_alive")
                member["thread_alive"] = None
                # A settled outcome can be emitted from the worker's finally
                # block while its Python thread is still briefly alive.
                if member.get("execution_state") in {"running", "starting"}:
                    for field in ("execution_state", "execution_outcome"):
                        member[f"recorded_{field}"] = member.get(field)
                    member.update(execution_state="unknown", execution_outcome="unknown")

    def snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(reversed(self.fleets.values())))

    def warning_text(self) -> str:
        return " ".join(filter(None, (self.warning, "仅显示最近 64 个宸枢运行时快照。" if self.truncated else "")))


def register_routes(app: Any, guard: list, pick: Any, cfg: Any) -> None:
    from fastapi import Query
    from .serve_history import page

    @app.get("/session/{session_id}/fleets", dependencies=guard, tags=["sessions"], operation_id="get_session_fleets")
    async def listing(session_id: str, cursor: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=64)):
        session = pick(session_id)
        snapshots = session.fleets.snapshot()
        from .serve_fleet_controls import enrich
        enrich(session, snapshots)
        available = session.agent.toolbox.get("chenshu_init") is not None
        if not available:
            reason = "本会话未启用宸枢。" if not snapshots else "本会话未启用宸枢；以下仅为已保存的历史记录。"
        elif not snapshots:
            reason = "尚无宸枢活动记录；看板只展示已发生的协作。"
        elif all(snapshot["recovered"] for snapshot in snapshots):
            reason = "仅有历史快照；尚未观察到本进程的宸枢活动。"
        else:
            reason = ""
        paged = page(snapshots, cursor, limit)
        log = session.agent.session_log
        return {"session_id": session_id, "fleets": paged.pop("items"), **paged,
                "available": available, "reason": reason,
                "persisted": bool(cfg.persist and log and log.complete and session.fleets.integrity),
                "warning": " ".join(filter(None, (session.fleets.warning_text(), session.fleet_controls.warning)))}


def register_command_routes(app: Any, guard: list, pick: Any, earlier_submission: Any, start_turn: Any) -> None:
    import asyncio
    from fastapi import Body, Header, HTTPException
    from . import serve_fleet_commands as commands
    from .serve_commands import CommandError

    @app.get("/session/{session_id}/fleets/{board_id}/missions/{mission_id}/changes", dependencies=guard,
             operation_id="get_fleet_mission_changes")
    async def changes(session_id: str, board_id: str, mission_id: str):
        try:
            return await asyncio.to_thread(commands.changes, pick(session_id), board_id, mission_id)
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.get("/session/{session_id}/fleet-commands/capabilities", dependencies=guard, operation_id="get_fleet_command_capabilities")
    async def capabilities(session_id: str):
        return await asyncio.to_thread(commands.capabilities, pick(session_id))

    @app.post("/session/{session_id}/fleet-commands/preview", dependencies=guard, operation_id="preview_fleet_command")
    async def preview(session_id: str, body: dict[str, Any] = Body(...)):
        try:
            return (await asyncio.to_thread(commands.prepare, pick(session_id), body)).preview
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/session/{session_id}/fleet-commands", dependencies=guard, status_code=202, operation_id="start_fleet_command")
    async def submit(session_id: str, body: dict[str, Any] = Body(...), idempotency_key: str = Header(default="")):
        session, key = pick(session_id), idempotency_key.strip()
        if not key or len(key) > 200:
            raise HTTPException(400, "宸枢命令需要 1 至 200 字符的 Idempotency-Key")
        fingerprint = commands.identity(body)

        def duplicate():
            previous = earlier_submission(session, key, fingerprint)
            if previous is None:
                return None
            run = next((item for item in session.runs if item["turn"] == previous["turn"]), {})
            return {"session_id": session_id, "accepted": True, **session.status_dict(),
                    "duplicate": True, "turn": previous["turn"], "run_id": run.get("id")}

        prior = duplicate()
        if prior is not None:
            return prior
        try:
            prepared = await asyncio.to_thread(commands.prepare, session, body, require_preview=True)
        except CommandError as exc:
            prior = duplicate()
            if prior is not None:
                return prior
            raise HTTPException(exc.status, str(exc)) from None
        prior = duplicate()
        if prior is not None:
            return prior
        await start_turn(session, fingerprint, key=key, command=prepared)
        return {"session_id": session_id, "accepted": True, **session.status_dict(), "run_id": session.in_flight["id"]}
