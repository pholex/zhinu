"""Durable competition views and explicit host commands/selection decisions."""
from __future__ import annotations

from . import serve_ownership

import copy
import hashlib
import json
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import agents
from .douqiao import MAX_CONTESTANTS, MIN_CONTESTANTS
from .serve_commands import CommandError, Prepared, _repository
from .tools import Toolbox

JOURNAL_EVENT = "serve.douqiao.updated"
SELECTION_EVENT = "serve.douqiao.selection"
MAX_CONTESTS = 64
MAX_SELECTIONS = 1000
MAX_REASON = 4000


class ContestView:
    def __init__(self) -> None:
        self.contests: dict[str, dict[str, Any]] = {}
        self.receipts: dict[str, dict[str, Any]] = {}
        self.warning = ""
        self.selection_fault = False

    def accept(self, value: dict[str, Any]) -> bool:
        cid, revision, candidates = value.get("contest_id"), value.get("revision"), value.get("candidates")
        if (not isinstance(cid, str) or not cid or type(revision) is not int or revision < 1
                or not isinstance(candidates, list) or not 2 <= len(candidates) <= MAX_CONTESTANTS
                or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in candidates)
                or len({item["id"] for item in candidates}) != len(candidates)
                or type(value.get("final")) is not bool
                or not isinstance(value.get("judge"), dict)):
            self.warning = "部分斗巧记录无法读取。"
            self.selection_fault = True
            return False
        previous = self.contests.get(cid)
        if previous and previous["revision"] >= revision:
            return False
        contest = copy.deepcopy(value)
        for field in ("selection", "selections", "selection_revision"):
            contest.pop(field, None)
        contest.update(selection=(previous or {}).get("selection"),
                       selections=(previous or {}).get("selections", []),
                       selection_revision=(previous or {}).get("selection_revision", 0))
        self.contests[cid] = contest
        while len(self.contests) > MAX_CONTESTS:
            del self.contests[next(iter(self.contests))]
            self.warning = "仅显示最近 64 场斗巧；采纳回执仍保留。"
        return True

    def record(self, event: dict[str, Any], log: Any, run_id: str) -> None:
        value = {key: copy.deepcopy(item) for key, item in event.items()
                 if key not in {"kind", "session_id", "run_id", "request_id", "task_id"}}
        if isinstance(value.get("candidates"), tuple):
            value["candidates"] = list(value["candidates"])
        value["run_id"] = run_id
        if not self.accept(value):
            return
        if log is None:
            self.warning = "斗巧记录没有持久化日志，无法可靠恢复或采纳。"
            self.selection_fault = True
            return
        try:
            log.commit_event(JOURNAL_EVENT, contest=value)
        except OSError:
            self.warning = "斗巧记录保存失败；可查看当前结果，不能据此提交采纳。"
            self.selection_fault = True

    def _decision(self, record: dict[str, Any]) -> bool:
        key, digest, cid, selection = (record.get(field) for field in ("key", "digest", "contest_id", "selection"))
        if (not isinstance(key, str) or not key or not isinstance(digest, str)
                or not isinstance(cid, str) or not isinstance(selection, dict)
                or type(selection.get("sequence")) is not int or selection["sequence"] < 1
                or not isinstance(selection.get("id"), str)
                or not isinstance(selection.get("candidate_id"), str)
                or not isinstance(selection.get("reason"), str)
                or not isinstance(selection.get("created_at"), (float, int))
                or selection.get("source") != "user"):
            return False
        previous = self.receipts.get(key)
        if previous is not None:
            return previous == record
        contest = self.contests.get(cid)
        if contest is not None:
            if selection["sequence"] != contest["selection_revision"] + 1:
                return False
            contest["selection_revision"] = selection["sequence"]
            contest["selection"] = copy.deepcopy(selection)
            contest["selections"].append(copy.deepcopy(selection))
        self.receipts[key] = copy.deepcopy(record)
        return True

    def restore(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        if not line.endswith("\n"):
                            raise ValueError("unfinished record")
                        record = json.loads(line)
                    except ValueError:
                        self.warning = "日志含损坏或未写完记录，斗巧采纳已停用。"
                        self.selection_fault = True
                        continue
                    if not isinstance(record, dict):
                        continue
                    if record.get("event") == JOURNAL_EVENT:
                        value = record.get("contest")
                        if isinstance(value, dict):
                            self.accept(value)
                        else:
                            self.warning = "斗巧记录格式不可读。"
                            self.selection_fault = True
                    elif record.get("event") == SELECTION_EVENT:
                        decision = {key: record.get(key) for key in ("key", "digest", "contest_id", "selection")}
                        if not self._decision(decision):
                            self.warning = "采纳记录不连续或不可读取，已停用新的采纳操作。"
                            self.selection_fault = True
        except OSError:
            self.warning = "斗巧记录无法读取。"
            self.selection_fault = True
        for contest in self.contests.values():
            if not contest.get("final"):
                contest["interrupted_by_restart"] = True
                for item in contest["candidates"]:
                    if item.get("status") in {"queued", "running"}:
                        item.update(status="interrupted_by_restart", error="服务中断，候选没有可靠结算。")
                if contest["judge"].get("status") in {"pending", "running"}:
                    contest["judge"].update(status="aborted", reason="服务中断，未保存可靠裁决。", recommendation_id="")

    def snapshot(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(reversed(self.contests.values())))


def _specs(session: Any) -> dict[str, Any]:
    tool = session.agent.toolbox.get("douqiao")
    if tool is None or not tool.available():
        return {}
    advertised = tool.parameters.get("properties", {}).get("spec", {}).get("enum", [])
    return {spec.name: spec for spec in getattr(session.agent, "_mounted_agent_specs", ())
            if spec.name in advertised and spec.tools}


def _ready(session: Any) -> str:
    if problem := serve_ownership.reason(session):
        return problem
    if session.busy:
        return "当前会话正在执行，请等待完成或停止。"
    if not _specs(session):
        return "此会话没有已加载且可用的斗巧规格。"
    if session.agent.plan_mode:
        return "当前处于规划模式，批准退出规划后才能执行。"
    if session.history_warning or not session.agent.session_log or not session.agent.session_log.complete:
        return "会话持久化不可用，请先核对并恢复服务。"
    if session.check_budget():
        return "当前会话预算已耗尽。"
    return ""


def _needs_isolation(spec: Any, capability: str | None = None) -> bool:
    tools = spec.tools if not capability else tuple(name for name in spec.tools if name in agents.CAPABILITY_TOOLS[capability])
    if not tools:
        raise CommandError("指定能力范围内没有可执行工具。")
    return any(name not in Toolbox.READONLY for name in tools)


def capabilities(session: Any) -> dict[str, Any]:
    specs, reason = _specs(session), _ready(session)
    default = session.agent.config.model
    names = {default, *(spec.model or default for spec in specs.values())}
    names.update(item.model for item in session.agent.registry.listing())
    names.update(session.agent.registry.remote_cached())
    return {"available": bool(specs), "reason": reason, "can_start": not reason,
            "specs": [{"name": spec.name, "description": spec.description,
                       "default_model": spec.model or default,
                       "default_isolation": "worktree" if _needs_isolation(spec) else "none"}
                      for spec in specs.values()],
            "models": sorted(names), "default_model": default, "default_judge_model": default,
            "min_candidates": MIN_CONTESTANTS, "max_candidates": MAX_CONTESTANTS}


def identity(body: dict[str, Any]) -> str:
    return "douqiao:" + json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prepare(session: Any, body: dict[str, Any], *, require_preview: bool = False) -> Prepared:
    reason = _ready(session)
    if reason:
        raise CommandError(reason, 409)
    allowed = {"spec", "task", "models", "criteria", "judge_model", "capability_mode", "preview_token"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise CommandError("斗巧仅接受 spec、task、models、criteria、judge_model、capability_mode、preview_token。")
    name = body.get("spec")
    spec = _specs(session).get(name) if isinstance(name, str) else None
    if spec is None:
        raise CommandError("执行规格未在当前会话加载。")
    task, models = body.get("task"), body.get("models")
    criteria, judge = body.get("criteria", ""), body.get("judge_model", session.agent.config.model)
    if not isinstance(task, str) or not task.strip() or len(task) > 64000:
        raise CommandError("共同任务需要 1 至 64000 字符。")
    if not isinstance(criteria, str) or len(criteria) > 16000:
        raise CommandError("评审标准须为不超过 16000 字符的文本。")
    if (not isinstance(models, list) or not MIN_CONTESTANTS <= len(models) <= MAX_CONTESTANTS
            or not all(isinstance(model, str) and model.strip() and len(model) <= 512 for model in models)):
        raise CommandError("斗巧需要 2 至 6 个模型席位，每席填写模型名；可重复同一模型。")
    if not isinstance(judge, str) or len(judge) > 512:
        raise CommandError("判官模型须为模型名称。")
    models = [model.strip() for model in models]
    judge = judge.strip() or session.agent.config.model
    capability = body.get("capability_mode")
    if capability is not None and (not isinstance(capability, str) or capability not in agents.CAPABILITY_TOOLS):
        raise CommandError("能力范围仅接受 read-only、read-write、execute、all。")
    isolated = _needs_isolation(spec, capability)
    if isolated:
        _repository(session.workspace)
    for model in [*models, judge]:
        try:
            session.agent.registry.resolve(model)
        except Exception as exc:
            raise CommandError(f"模型 {model!r} 当前不可用，请先完成配置。", 409) from exc
    args = {"spec": name, "task": task.strip(), "models": models, "criteria": criteria.strip(), "judge_model": judge}
    if capability is not None:
        args["capability_mode"] = capability
    review = {"args": args, "spec": asdict(spec), "workspace": str(session.workspace)}
    token = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if require_preview and body.get("preview_token") != token:
        raise CommandError("任务配置已经变化，请重新预览后提交。", 409)
    preview = {**args, "count": len(models), "isolation": "worktree" if isolated else "none", "preview_token": token}
    return Prepared(args, {"kind": "douqiao", "operation": "new", "spec": name}, preview, {})


def select(session: Any, cid: str, body: dict[str, Any], key: str) -> dict[str, Any]:
    if not key or len(key) > 200:
        raise CommandError("采纳操作需要 1 至 200 字符的 Idempotency-Key。")
    digest = hashlib.sha256(json.dumps({"contest_id": cid, "body": body}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    view = session.contests
    previous = view.receipts.get(key)
    if previous is not None:
        if previous["digest"] != digest:
            raise CommandError("此幂等键已经用于另一项采纳操作。", 409)
        selection = copy.deepcopy(previous["selection"])
        return {"contest_id": cid, "selection": selection, "selection_revision": selection["sequence"], "duplicate": True}
    if problem := serve_ownership.reason(session):
        raise CommandError(problem, 503)
    if not isinstance(body, dict) or set(body) != {"candidate_id", "reason", "expected_revision"}:
        raise CommandError("采纳仅接受 candidate_id、reason、expected_revision。")
    candidate_id, reason, revision = (body.get(field) for field in ("candidate_id", "reason", "expected_revision"))
    if not isinstance(candidate_id, str) or not candidate_id or len(candidate_id) > 256:
        raise CommandError("请选择当前比赛的候选。")
    if not isinstance(reason, str) or len(reason) > MAX_REASON:
        raise CommandError("采纳理由须为不超过 4000 字符的文本。")
    if type(revision) is not int or revision < 0:
        raise CommandError("expected_revision 必须是非负整数。")
    if session.busy:
        raise CommandError("会话执行中不能更新采纳，请等待结束。", 409)
    log = session.agent.session_log
    if view.selection_fault or session.history_warning or log is None or not log.complete:
        raise CommandError("会话记录不完整，无法可靠保存采纳；请核对并恢复服务。", 503)
    contest = view.contests.get(cid)
    if contest is None:
        raise CommandError("当前会话没有此斗巧记录。", 404)
    if not contest.get("final") or contest.get("interrupted_by_restart"):
        raise CommandError("比赛尚未可靠结算，不能采纳候选。", 409)
    if revision != contest["selection_revision"]:
        raise CommandError("采纳决定已经变化，请刷新后重新选择。", 409)
    candidate = next((item for item in contest["candidates"] if item["id"] == candidate_id), None)
    if (candidate is None or candidate.get("status") != "completed"
            or not isinstance(candidate.get("answer"), str) or not candidate["answer"].strip()
            or candidate.get("started") is not True):
        raise CommandError("只能采纳本场比赛中真实完成的候选。", 409)
    if len(view.receipts) >= MAX_SELECTIONS:
        raise CommandError("本会话已达到 1000 次采纳记录上限；旧记录和幂等回执均保留。", 409)
    selection = {"id": f"selection-{uuid.uuid4().hex}", "candidate_id": candidate_id,
                 "reason": reason.strip(), "source": "user", "created_at": time.time(), "sequence": revision + 1}
    record = {"contest_id": cid, "key": key, "digest": digest, "selection": selection}
    try:
        log.commit_event(SELECTION_EVENT, **record)
    except OSError as exc:
        # The write may have reached disk before failure was reported. Do not
        # publish it or let a second attempt create another decision.
        view.selection_fault = True
        view.warning = "采纳保存未获可靠确认，请重启后核对记录；当前不再接受新的采纳。"
        raise CommandError(view.warning, 503) from exc
    view._decision(record)
    session.updated_at = selection["created_at"]
    session.publish("douqiao.selection", contest_id=cid, selection=selection, selection_revision=selection["sequence"])
    return {"contest_id": cid, "selection": copy.deepcopy(selection), "selection_revision": selection["sequence"], "duplicate": False}


def register_routes(app: Any, guard: list, pick: Any, earlier_submission: Any, start_turn: Any, cfg: Any) -> None:
    import asyncio
    from fastapi import Body, Header, HTTPException, Query
    from .serve_history import page

    @app.get("/session/{session_id}/contests", dependencies=guard, tags=["sessions"], operation_id="get_session_contests")
    async def listing(session_id: str, cursor: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=64)):
        session = pick(session_id)
        snapshot = session.contests.snapshot()
        for contest in snapshot:
            for candidate in contest["candidates"]:
                candidate.pop("artifact_previews", None)
        paged = page(snapshot, cursor, limit)
        log = session.agent.session_log
        return {"session_id": session_id, "contests": paged.pop("items"), **paged,
                "persisted": bool(cfg.persist and log and log.complete and not session.contests.selection_fault),
                "warning": session.contests.warning}

    @app.get("/session/{session_id}/contests/capabilities", dependencies=guard, tags=["sessions"], operation_id="get_contest_capabilities")
    async def capabilities_route(session_id: str):
        return await asyncio.to_thread(capabilities, pick(session_id))

    @app.post("/session/{session_id}/contests/preview", dependencies=guard, tags=["sessions"], operation_id="preview_contest")
    async def preview(session_id: str, body: dict[str, Any] = Body(...)):
        try:
            return (await asyncio.to_thread(prepare, pick(session_id), body)).preview
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from None

    @app.post("/session/{session_id}/contests", dependencies=guard, status_code=202, tags=["sessions"], operation_id="start_contest")
    async def start(session_id: str, body: dict[str, Any] = Body(...), idempotency_key: str = Header(default="")):
        session, key = pick(session_id), idempotency_key.strip()
        if not key or len(key) > 200:
            raise HTTPException(400, "斗巧执行需要 1 至 200 字符的 Idempotency-Key")
        fingerprint = identity(body)

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
            prepared = await asyncio.to_thread(prepare, session, body, require_preview=True)
        except CommandError as exc:
            # A concurrent request can reserve the key while this preflight is
            # waiting for a worker. Its durable receipt outranks a stale error.
            prior = duplicate()
            if prior is not None:
                return prior
            raise HTTPException(exc.status, str(exc)) from None
        prior = duplicate()
        if prior is not None:
            return prior
        await start_turn(session, fingerprint, key=key, command=prepared)
        return {"session_id": session_id, "accepted": True, **session.status_dict(), "run_id": session.in_flight["id"]}

    @app.post("/session/{session_id}/contests/{contest_id}/selection", dependencies=guard, tags=["sessions"], operation_id="select_contest_candidate")
    async def selection(session_id: str, contest_id: str, body: dict[str, Any] = Body(...), idempotency_key: str = Header(default="")):
        try:
            return select(pick(session_id), contest_id, body, idempotency_key.strip())
        except CommandError as exc:
            raise HTTPException(exc.status, str(exc)) from None
