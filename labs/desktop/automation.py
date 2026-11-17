"""Persistent application schedules; reservations precede all execution effects."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import json
import math
import os
import re
import stat
from pathlib import Path
import tempfile
import threading
import time
import uuid

MAX_BYTES = 4 * 1024 * 1024
QUESTION_TIMEOUT_SECONDS = 60
ACTIVE = {"reserved", "creating", "submitting", "running", "unknown"}


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def definition(value):
    if not isinstance(value, dict) or set(value) not in ({"name", "workspace", "model", "prompt", "schedule"}, {"name", "workspace", "model", "prompt", "schedule", "project_id"}):
        raise ValueError("自动化任务格式不正确")
    if "project_id" in value and (not isinstance(value["project_id"], str) or not re.fullmatch(r"[0-9a-f]{24}", value["project_id"])):
        raise ValueError("项目标识不正确")
    for field, limit in (("name", 160), ("workspace", 4096), ("model", 300), ("prompt", 100000)):
        if not isinstance(value[field], str) or (not value[field].strip() and not (field == "workspace" and value.get("project_id"))) or len(value[field]) > limit:
            raise ValueError("请填写有效的任务名称、项目、模型和指令")
    schedule = value["schedule"]
    if not isinstance(schedule, dict):
        raise ValueError("调度格式不正确")
    if schedule.get("kind") == "once":
        if set(schedule) != {"kind", "at"} or not _number(schedule["at"]):
            raise ValueError("单次执行时间不正确")
    elif schedule.get("kind") == "interval":
        if set(schedule) != {"kind", "seconds"} or not _number(schedule["seconds"]) or schedule["seconds"] < 60:
            raise ValueError("重复任务间隔至少一分钟")
    elif schedule.get("kind") == "calendar":
        if (set(schedule) != {"kind", "timezone", "time", "weekdays"}
                or not isinstance(schedule["timezone"], str) or len(schedule["timezone"]) > 100
                or not isinstance(schedule["time"], str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", schedule["time"])
                or not isinstance(schedule["weekdays"], list) or not schedule["weekdays"]
                or any(type(day) is not int or not 0 <= day <= 6 for day in schedule["weekdays"])
                or len(set(schedule["weekdays"])) != len(schedule["weekdays"])):
            raise ValueError("日历调度须提供 IANA 时区、HH:MM 与周一为 0 的星期列表")
        try:
            ZoneInfo(schedule["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("调度时区不可用") from None
    else:
        raise ValueError("只支持单次、固定间隔或日历调度")
    return copy.deepcopy(value)



def next_due(schedule, after):
    """Skip nonexistent DST wall times; choose only the first repeated wall time."""
    if schedule["kind"] == "once":
        return schedule["at"]
    if schedule["kind"] == "interval":
        return after + schedule["seconds"]
    zone = ZoneInfo(schedule["timezone"])
    hour, minute = map(int, schedule["time"].split(":"))
    start = datetime.fromtimestamp(after, zone).date()
    for offset in range(370):
        day = start + timedelta(days=offset)
        if day.weekday() not in schedule["weekdays"]:
            continue
        local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone, fold=0)
        timestamp = local.timestamp()
        if datetime.fromtimestamp(timestamp, zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            continue
        if timestamp > after:
            return timestamp
    raise ValueError("一年内没有可用的日历执行时间")


def calendar_view(state, *, count=8):
    result = []
    for tid, task in state["tasks"].items():
        due = task["next_due"]
        schedule = task["definition"]["schedule"]
        occurrences = []
        if task["enabled"]:
            for _ in range(count):
                occurrences.append(due)
                if schedule["kind"] == "once":
                    break
                due = next_due(schedule, due)
        result.append({"task_id": tid, "timezone": schedule.get("timezone", "UTC"),
                       "occurrences": occurrences, "enabled": task["enabled"]})
    return result


def validate(value):
    if (not isinstance(value, dict) or set(value) != {"revision", "tasks", "runs"}
            or type(value["revision"]) is not int or value["revision"] < 0
            or not isinstance(value["tasks"], dict) or len(value["tasks"]) > 100
            or not isinstance(value["runs"], dict) or len(value["runs"]) > 1000):
        raise ValueError("自动化记录格式不正确")
    for key, task in value["tasks"].items():
        if (not isinstance(key, str) or not key or not isinstance(task, dict)
                or set(task) != {"definition", "enabled", "next_due", "warning"}
                or type(task["enabled"]) is not bool or not _number(task["next_due"])
                or not isinstance(task["warning"], str)):
            raise ValueError("自动化配置损坏")
        definition(task["definition"])
    for key, run in value["runs"].items():
        if (not isinstance(key, str) or not key or not isinstance(run, dict)
                or set(run) != {"task_id", "scheduled_at", "created_at", "state", "session_id", "run_id", "message"}
                or not isinstance(run["task_id"], str)
                or run["task_id"] not in value["tasks"]
                or not _number(run["scheduled_at"]) or not _number(run["created_at"])
                or not isinstance(run["state"], str)
                or run["state"] not in ACTIVE | {"completed", "failed", "interrupted", "acknowledged"}
                or any(not isinstance(run[field], str) for field in ("session_id", "run_id", "message"))):
            raise ValueError("自动化运行记录损坏")
    return value


class AutomationConflict(Exception):
    pass


class AutomationStore:
    def __init__(self, path: Path, *, write_guard=None):
        self.path, self.lock = path, threading.RLock()
        self.write_guard = write_guard or (lambda: None)

    def _read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("自动化记录不是独立的普通文件")
                data = stream.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("自动化记录过大")
            return validate(json.loads(data.decode("utf-8", errors="replace")))
        except FileNotFoundError:
            return {"revision": 0, "tasks": {}, "runs": {}}

    def read(self):
        with self.lock:
            return self._read()

    def _save(self, state):
        self.write_guard()
        state = copy.deepcopy(state)
        state["revision"] += 1
        encoded = json.dumps(validate(state), ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValueError("自动化记录超出容量限制")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".automation-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            self.write_guard()
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return state

    def configure(self, revision, task_id, value, now=None, *, create=False):
        value = definition(value)
        if type(create) is not bool or (create and (not isinstance(task_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id))):
            raise ValueError("新任务必须有稳定的客户端标识")
        now = time.time() if now is None else now
        with self.lock:
            state = self._read()
            if create and task_id in state["tasks"]:
                if state["tasks"][task_id]["definition"] != value:
                    raise AutomationConflict()
                return task_id, state
            if type(revision) is not int or revision != state["revision"]:
                raise AutomationConflict()
            if task_id and task_id not in state["tasks"] and not create:
                raise KeyError(task_id)
            if task_id and any(r["task_id"] == task_id and r["state"] in ACTIVE for r in state["runs"].values()):
                raise ValueError("任务仍有执行中或待核对的运行，不能改写配置")
            task_id = task_id or uuid.uuid4().hex
            schedule = value["schedule"]
            state["tasks"][task_id] = {"definition": value, "enabled": False,
                "next_due": next_due(schedule, now), "warning": ""}
            return task_id, self._save(state)

    def enable(self, revision, task_id, enabled, now=None):
        now = time.time() if now is None else now
        if type(enabled) is not bool:
            raise ValueError("启用状态不正确")
        with self.lock:
            state = self._read()
            task = state["tasks"][task_id]
            if task["enabled"] == enabled:
                return state
            if type(revision) is not int or revision != state["revision"]:
                raise AutomationConflict()
            if enabled:
                if any(r["task_id"] == task_id and r["state"] == "unknown" for r in state["runs"].values()):
                    raise ValueError("先核对结果未知的运行，再启用任务")
                schedule = task["definition"]["schedule"]
                if schedule["kind"] == "once":
                    if schedule["at"] < now:
                        raise ValueError("单次时间已经过去，请重新配置未来时间")
                    task["next_due"] = schedule["at"]
                else:
                    task["next_due"] = next_due(schedule, now)
            task["enabled"], task["warning"] = enabled, ""
            return self._save(state)

    def reserve_due(self, now=None):
        now = time.time() if now is None else now
        with self.lock:
            state = self._read()
            for task_id, task in state["tasks"].items():
                if not task["enabled"] or task["next_due"] > now:
                    continue
                if any(r["task_id"] == task_id and r["state"] in ACTIVE for r in state["runs"].values()):
                    continue
                if len(state["runs"]) >= 1000:
                    raise ValueError("自动化运行记录已达上限，已停止新增触发")
                run_id, due = uuid.uuid4().hex, task["next_due"]
                schedule = task["definition"]["schedule"]
                if schedule["kind"] == "once":
                    task["enabled"] = False
                else:
                    task["next_due"] = next_due(schedule, now)
                state["runs"][run_id] = {"task_id": task_id, "scheduled_at": due, "created_at": now,
                    "state": "reserved", "session_id": "", "run_id": "", "message": ""}
                self._save(state)
                return run_id, copy.deepcopy(task["definition"])
            return None

    def record(self, run_id, *, state, session_id=None, execution_id=None, message="", pause=False):
        with self.lock:
            snapshot = self._read()
            run = snapshot["runs"][run_id]
            run.update(state=state, message=message[:1000])
            if session_id is not None:
                run["session_id"] = session_id
            if execution_id is not None:
                run["run_id"] = execution_id
            if state == "unknown":
                task = snapshot["tasks"][run["task_id"]]
                task.update(enabled=False, warning="存在结果未知的运行，已停用后续调度；请先核对")
            elif pause:
                snapshot["tasks"][run["task_id"]].update(enabled=False, warning=message[:1000])
            return self._save(snapshot)

    def recover(self, now=None):
        """Never replay an uncertain reservation or catch up missed offline slots."""
        now = time.time() if now is None else now
        with self.lock:
            state = self._read()
            changed = False
            for run in state["runs"].values():
                if run["state"] in {"reserved", "creating", "submitting"}:
                    run.update(state="unknown", message="服务在提交期间退出；未自动重放，请核对关联会话")
                    state["tasks"][run["task_id"]].update(enabled=False, warning="提交结果未知，请核对")
                    changed = True
            for task in state["tasks"].values():
                if task["enabled"] and task["next_due"] < now:
                    schedule = task["definition"]["schedule"]
                    if schedule["kind"] == "once":
                        task.update(enabled=False, warning="停机期间已错过执行时间；没有补跑")
                    else:
                        task.update(next_due=next_due(schedule, now), warning="停机错过的周期没有补跑")
                    changed = True
            return self._save(state) if changed else state

    def acknowledge(self, revision, run_id, note):
        """Explicit human reconciliation; never changes a run into success."""
        if not isinstance(note, str) or not note.strip() or len(note) > 1000:
            raise ValueError("请填写结果核对说明")
        with self.lock:
            state = self._read()
            run = state["runs"][run_id]
            if run["state"] == "acknowledged" and run["message"] == note.strip():
                return state
            if type(revision) is not int or revision != state["revision"]:
                raise AutomationConflict()
            if run["state"] != "unknown":
                raise ValueError("仅未知运行需要人工核对")
            run.update(state="acknowledged", message=note.strip())
            return self._save(state)


class AutomationRejected(Exception):
    """The local service explicitly rejected session creation before submission."""


class AutomationRunner:
    """Use the existing authenticated service; never create a second executor."""
    def __init__(self, store, request):
        self.store, self.request = store, request
        self.ticking = False
        self.warning = ""

    async def tick(self, now=None):
        if self.ticking:
            return
        self.ticking = True
        try:
            self.store.write_guard()
            await self.refresh()
            self.store.write_guard()
            reserved = self.store.reserve_due(now)
            if reserved is None:
                return
            run_id, task = reserved
            try:
                self.store.record(run_id, state="creating")
                created = await self.request("POST", "/session", {
                    "model": task["model"],
                    **({"project_id": task["project_id"]} if task.get("project_id")
                       else {"workspace": task["workspace"]}),
                    "questions": {"foreground_timeout_seconds": QUESTION_TIMEOUT_SECONDS}}, key=f"automation:{run_id}:create")
                sid = created["session_id"]
                if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", sid):
                    raise ValueError("invalid session receipt")
                self.store.record(run_id, state="submitting", session_id=sid)
                submitted = await self.request("POST", f"/session/{sid}/prompt_async", {
                    "text": f"[自动化任务：{task['name']}]\n\n{task['prompt']}"}, key=f"automation:{run_id}:prompt")
                execution = (submitted.get("in_flight") or {}).get("id")
                if not isinstance(execution, str) or not execution:
                    raise ValueError("missing execution receipt")
                self.store.record(run_id, state="running", execution_id=execution)
            except AutomationRejected:
                self.store.record(run_id, state="failed", pause=True,
                    message="创建会话被拒绝，任务尚未提交；请检查项目与模型配置后重新启用")
            except Exception:
                self.store.record(run_id, state="unknown", message="提交结果无法可靠确认；已停用后续触发，请核对关联会话")
        except (OSError, ValueError):
            # A failed reservation must never lead to an execution effect.
            self.warning = "自动化记录无法可靠保存或读取；已停止本次调度，请检查本机存储"
        finally:
            self.ticking = False

    async def refresh(self):
        for key, record in self.store.read()["runs"].items():
            if record["state"] != "running":
                continue
            sid = record["session_id"]
            try:
                runs, persisted = await self._pages(f"/session/{sid}/runs", "runs")
                index = next((i for i, r in enumerate(runs) if r["id"] == record["run_id"]), None)
                if index is None or not persisted:
                    self.store.record(key, state="unknown", message="找不到可靠的原运行记录，请核对会话")
                    continue
                # This is a dedicated service session. A question may be answered
                # in a later explicit turn; the original turn alone is not its outcome.
                relevant = runs[index:]
                if any(run["status"] in {"running", "accepted", "queued"} for run in relevant):
                    continue
                status = await self.request("GET", f"/session/{sid}/status")
                if status.get("busy") or status.get("pending_approvals"):
                    continue
                if status.get("turns") != relevant[-1].get("turn"):
                    continue  # A new turn crossed the history/status read boundary.
                questions = await self.request("GET", f"/session/{sid}/questions")
                if any(q.get("state") in {"open", "pending", "queued"} for q in questions.get("questions", [])):
                    continue
                fleets, fleet_persisted = await self._pages(f"/session/{sid}/fleets", "fleets")
                if not fleet_persisted or any(f.get("limits", {}).get("members", 0) for f in fleets):
                    self.store.record(key, state="unknown", message="后台成员记录不完整，请核对协作记录")
                    continue
                members = [m for f in fleets for m in f["members"]]
                if any(m.get("execution_state") in {"running", "starting"}
                       or (m.get("status") == "retired" and m.get("thread_alive") is True) for m in members):
                    continue
                if any(m.get("execution_state") != "settled" or m.get("execution_outcome") not in {"completed", "failed", "partial"}
                       for m in members):
                    self.store.record(key, state="unknown", message="后台成员结局未知，请核对协作记录")
                    continue
                settled = await self.request("GET", f"/session/{sid}/status")
                if (settled.get("busy") or settled.get("pending_approvals")
                        or settled.get("turns") != status.get("turns")):
                    continue
                outcomes = [{"completed": "completed", "failed": "failed", "interrupted": "interrupted",
                             "stopped": "interrupted"}.get(run["status"], "unknown") for run in relevant]
                outcomes.extend("completed" if m["execution_outcome"] == "completed" else "failed" for m in members)
                final = next((value for value in ("unknown", "failed", "interrupted") if value in outcomes), "completed")
                self.store.record(key, state=final, message="请在关联会话查看执行记录、审批与成果")
            except Exception:
                # Read failures may be retried. They never resubmit a prompt.
                self.warning = "部分自动化运行状态暂时无法读取；保留上次记录，不重复提交"

    async def _pages(self, path, field):
        values, cursor, persisted = [], 0, True
        seen = set()
        for _ in range(50):
            page = await self.request("GET", f"{path}?cursor={cursor}&limit={200 if field == 'runs' else 64}")
            if not isinstance(page, dict) or not isinstance(page.get(field), list):
                raise ValueError("invalid paged response")
            persisted = persisted and page.get("persisted") is True
            for row in page[field]:
                identity = row.get("id") if field == "runs" else row.get("board_id")
                if not isinstance(identity, str) or not identity or identity in seen:
                    raise ValueError("invalid page identity")
                seen.add(identity)
                values.append(row)
            following = page.get("next_cursor")
            if following is None:
                return values, persisted
            if type(following) is not int or following <= cursor:
                raise ValueError("invalid page cursor")
            cursor = following
        raise ValueError("too many pages")
