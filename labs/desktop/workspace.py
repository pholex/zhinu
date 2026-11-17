"""Project context and session organization owned by the desktop application."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading

from .app_ownership import OwnershipUnavailable, require_owner

MAX_BYTES = 2 * 1024 * 1024


def project_id(workspace: str) -> str:
    return hashlib.sha256(workspace.encode("utf-8", errors="replace")).hexdigest()[:24]



def session_project_id(session) -> str:
    scope = getattr(getattr(session, "cfg", None), "project_scope", {})
    return scope.get("project_id") or project_id(str(session.agent.config.workspace))


def project_catalogue(sessions, root=None, state=None):
    definitions = (state or {}).get("projects", {})
    projects = {}
    paths = ([str(root)] if root is not None else [])
    paths += [str(s.agent.config.workspace) for s in sessions.values()
              if not getattr(getattr(s, "cfg", None), "project_scope", {})]
    for workspace in paths:
        pid = project_id(workspace)
        projects[pid] = {"project_id": pid, "name": Path(workspace).name or workspace,
                         "workspace": workspace, "primary": workspace, "folders": [workspace]}
    for pid, item in definitions.items():
        if "folders" in item:
            projects[pid] = {"project_id": pid, "name": item["name"], "workspace": item["primary"],
                             "primary": item["primary"], "folders": list(item["folders"])}
    return {"projects": list(projects.values()),
            "sessions": {sid: session_project_id(s) for sid, s in sessions.items()}}


class ProjectResolver:
    """Application-owned project definitions, independent of execution directories."""
    def __init__(self, path: Path, root: Path):
        self.store, self.root = WorkspaceState(path), root
        self.scratch = path.parent / "project-workspaces"

    def __call__(self, pid, workspace="", snapshot=None):
        if not isinstance(pid, str) or len(pid) != 24 or any(c not in "0123456789abcdef" for c in pid):
            raise ValueError("项目标识不正确")
        known = project_catalogue({}, self.root, self.store.read())["projects"]
        project = next((p for p in known if p["project_id"] == pid), None)
        if project is None and workspace:
            target = Path(workspace).resolve()
            if target.is_relative_to(self.root) and project_id(str(target)) == pid:
                project = {"folders": [str(target)], "primary": str(target)}
        if project is None:
            raise ValueError("项目不存在，请重新选择项目")
        if snapshot is not None:
            if (not isinstance(snapshot, dict) or set(snapshot) != {"project_id", "workspace", "folders"}
                    or snapshot["project_id"] != pid or snapshot["workspace"] != workspace
                    or not isinstance(snapshot["folders"], list)
                    or any(not isinstance(p, str) or not Path(p).is_absolute() for p in snapshot["folders"])):
                raise ValueError("会话项目范围不可识别")
            # Restore the session's original scope; project edits apply to new chats.
            return copy.deepcopy(snapshot)
        folders = list(project["folders"])
        for folder in folders:
            if not Path(folder).is_dir() or str(Path(folder).resolve()) != folder:
                raise ValueError("项目文件夹不可用，请在项目设置中重新选择：" + folder)
        primary = project["primary"]
        if not primary:
            target = self.scratch / pid
            target.mkdir(parents=True, exist_ok=True, mode=0o700)
            primary = str(target.resolve())
        if workspace and workspace != primary:
            raise ValueError("项目主文件夹已改变，请重新打开新对话；输入草稿已保留")
        return {"project_id": pid, "workspace": primary, "folders": folders}


def project_artifacts(sessions, pid: str, *, limit: int = 300) -> dict:
    """Aggregate registered results only; no workspace scanning or implicit reads."""
    values = []
    warning = []
    found = False
    def append(sid, source, artifacts, created):
        for artifact in artifacts or []:
            if isinstance(artifact, dict) and isinstance(artifact.get("id"), str):
                values.append({"session_id": sid, "source": source, "artifact": copy.deepcopy(artifact),
                               "created_at": created if type(created) in {int, float} else 0})
    for sid, session in list(sessions.items()):
        if session_project_id(session) != pid:
            continue
        found = True
        if getattr(session, "history_warning", ""):
            warning.append("部分会话历史不完整，成果列表可能缺项")
        for run in copy.deepcopy(session.runs):
            append(sid, {"run_id": run["id"]}, (run.get("result") or {}).get("artifacts"), run.get("started_at", 0))
        for batch in copy.deepcopy(session.weaves.batches).values():
            for item in batch.get("items", []):
                append(sid, {"batch_id": batch["batch_id"], "item_id": item["id"]}, item.get("artifacts"), batch.get("updated_at", 0))
        for contest in copy.deepcopy(session.contests.contests).values():
            for candidate in contest.get("candidates", []):
                append(sid, {"contest_id": contest["contest_id"], "candidate_id": candidate["id"]}, candidate.get("artifacts"), contest.get("updated_at", 0))
    values.sort(key=lambda item: item["created_at"], reverse=True)
    return {"project_id": pid, "artifacts": values[:limit], "truncated": len(values) > limit,
            "warning": "；".join(dict.fromkeys(warning)), "found": found}


def validate(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {"revision", "projects", "sessions"}
            or type(value["revision"]) is not int or value["revision"] < 0
            or not isinstance(value["projects"], dict) or len(value["projects"]) > 500
            or not isinstance(value["sessions"], dict) or len(value["sessions"]) > 2000):
        raise ValueError("项目工作台状态格式不正确")
    for key, item in value["projects"].items():
        if (not isinstance(key, str) or len(key) != 24 or any(c not in "0123456789abcdef" for c in key)
                or not isinstance(item, dict) or set(item) not in ({"context"}, {"context", "name", "folders", "primary"})
                or not isinstance(item["context"], str) or len(item["context"]) > 20000):
            raise ValueError("项目上下文格式或长度不正确")
        if "folders" in item:
            folders, primary = item["folders"], item["primary"]
            if (not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 100
                    or not isinstance(folders, list) or len(folders) > 20
                    or any(not isinstance(p, str) or not p or len(p) > 4096 or not Path(p).is_absolute() for p in folders)
                    or len(set(folders)) != len(folders) or not isinstance(primary, str)
                    or (primary not in folders if folders else primary != "")):
                raise ValueError("项目名称或文件夹设置不正确")
    for key, item in value["sessions"].items():
        if (not isinstance(key, str) or not key or len(key) > 200
                or not isinstance(item, dict) or set(item) != {"pinned", "archived"}
                or any(type(flag) is not bool for flag in item.values())):
            raise ValueError("会话组织状态格式不正确")
    return value


class Conflict(Exception):
    pass


class WorkspaceState:
    def __init__(self, path: Path, *, write_guard=None):
        self.path = path
        self.write_guard = write_guard or (lambda: None)
        self.lock = threading.Lock()

    def _read(self):
        try:
            with self.path.open(encoding="utf-8", errors="replace") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw.encode("utf-8")) > MAX_BYTES:
                raise ValueError("项目状态文件过大")
            return validate(json.loads(raw))
        except FileNotFoundError:
            return {"revision": 0, "projects": {}, "sessions": {}}

    def read(self):
        with self.lock:
            return self._read()

    def update(self, revision: int, group: str, key: str, item: dict):
        if type(revision) is not int or revision < 0 or not isinstance(group, str) or group not in {"projects", "sessions"}:
            raise ValueError("项目更新格式不正确")
        candidate = {"revision": revision, "projects": {}, "sessions": {}}
        if not isinstance(key, str):
            raise ValueError("项目键格式不正确")
        candidate[group][key] = item
        validate(candidate)
        with self.lock:
            current = self._read()
            if group == "projects" and set(item) == {"context"} and "folders" in current[group].get(key, {}):
                item = {**current[group][key], **item}
            # A lost reply can be retried without overwriting a newer different edit.
            if current[group].get(key) == item:
                return current
            if revision != current["revision"]:
                raise Conflict()
            saved = copy.deepcopy(current)
            saved[group][key] = copy.deepcopy(item)
            saved["revision"] += 1
            self._write(saved)
            return saved

    def _write(self, saved):
        validate(saved)
        encoded = json.dumps(saved, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_BYTES:
            raise ValueError("项目状态超出容量限制")
        self.write_guard()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(prefix=".workspace-", dir=self.path.parent)
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


def project_state_path(directory: Path) -> Path:
    """One-way copy leaves the previous app's project metadata available for rollback."""
    target, legacy = directory / "projects-v2.json", directory / "workspace.json"
    if target.exists() or not legacy.exists():
        return target
    value = WorkspaceState(legacy).read()
    fd, temporary = tempfile.mkstemp(prefix=".projects-v2-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(value, ensure_ascii=False).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def install_workspace_routes(app, path: Path, token: str, root: Path | None = None) -> None:
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse

    store = WorkspaceState(path, write_guard=lambda: require_owner(app))

    def authorize(request):
        supplied = request.headers.get("authorization", "").encode("utf-8")
        if not secrets.compare_digest(supplied, ("Bearer " + token).encode("utf-8")):
            raise HTTPException(401, "需要本机应用令牌")

    def catalogue():
        return project_catalogue(app.state.sessions, root, store.read())

    async def read_state(request):
        authorize(request)
        try:
            state = store.read()
        except (OSError, ValueError):
            raise HTTPException(503, "无法读取项目状态；原文件已保留") from None
        return JSONResponse({"state": state, "catalogue": catalogue()}, headers={"Cache-Control": "no-store"})

    async def update_state(request):
        authorize(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 100000:
                raise HTTPException(413, "项目更新超出容量限制")
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(body, dict) or set(body) != {"revision", "group", "key", "value"}:
                raise ValueError()
            group, key = body["group"], body["key"]
            if not isinstance(group, str) or group not in {"projects", "sessions"} or not isinstance(key, str):
                raise ValueError()
            known = catalogue()
            keys = {p["project_id"] for p in known["projects"]} if group == "projects" else set(known["sessions"])
            value = body["value"]
            new_project = group == "projects" and isinstance(value, dict) and "folders" in value
            if key not in keys and not new_project:
                raise HTTPException(404, "此项目或会话不属于当前工作台")
            if new_project:
                validate({"revision": 0, "projects": {key: value}, "sessions": {}})
                folders = []
                for folder in value["folders"]:
                    try:
                        target = Path(folder).expanduser().resolve(strict=True)
                    except (OSError, RuntimeError):
                        raise HTTPException(422, "文件夹不可用，请重新选择：" + folder) from None
                    if not target.is_dir():
                        raise ValueError("请选择文件夹")
                    folders.append(str(target))
                primary = str(Path(value["primary"]).resolve()) if value["primary"] else ""
                value = {**value, "folders": list(dict.fromkeys(folders)), "primary": primary}
            else:
                value = body["value"]
            state = store.update(body["revision"], group, key, value)
        except Conflict:
            raise HTTPException(409, "项目状态已有新修改，请重新读取后核对") from None
        except ValueError:
            raise HTTPException(422, "项目更新格式不正确") from None
        except OwnershipUnavailable as error:
            raise HTTPException(503, str(error)) from None
        except OSError:
            raise HTTPException(503, "项目状态保存失败，请保留输入") from None
        return JSONResponse({"state": state, "catalogue": catalogue()}, headers={"Cache-Control": "no-store"})

    async def delete_archived(request):
        authorize(request)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 100000:
                raise HTTPException(413, "删除请求过大")
        try:
            body = json.loads(raw.decode("utf-8", errors="replace"))
            if (not isinstance(body, dict) or set(body) != {"revision", "session_ids"}
                    or type(body["revision"]) is not int or body["revision"] < 0
                    or not isinstance(body["session_ids"], list) or not 1 <= len(body["session_ids"]) <= 2000
                    or any(not isinstance(sid, str) or not sid or len(sid) > 200 for sid in body["session_ids"])
                    or len(set(body["session_ids"])) != len(body["session_ids"])):
                raise ValueError()
            with store.lock:
                require_owner(app)
                state = store._read()
                ids = body["session_ids"]
                live = [sid for sid in ids if sid in app.state.sessions]
                # A repeated request cannot acquire new targets. Already removed ids
                # are harmless; live targets still require the confirmed revision.
                if live and (state["revision"] != body["revision"]
                        or any(not state["sessions"].get(sid, {}).get("archived") for sid in live)):
                    raise HTTPException(409, {"code": "archive_delete_changed"})
                result = app.state.delete_idle_sessions(live)
                removed = [sid for sid in ids if sid not in app.state.sessions]
                if any(sid in state["sessions"] for sid in removed):
                    state = copy.deepcopy(state)
                    for sid in removed:
                        state["sessions"].pop(sid, None)
                    state["revision"] += 1
                    store._write(state)
        except (ValueError, KeyError):
            raise HTTPException(422, "删除请求格式不正确") from None
        except OSError:
            raise HTTPException(503, "删除尚未完成，请刷新核对剩余会话") from None
        return JSONResponse({"state": state, "catalogue": catalogue(),
                             "deleted": removed, "incomplete": result["incomplete"]},
                            headers={"Cache-Control": "no-store"})

    async def artifacts(request, pid):
        authorize(request)
        if pid not in {p["project_id"] for p in catalogue()["projects"]}:
            raise HTTPException(404, "此项目不属于当前工作台")
        value = project_artifacts(app.state.sessions, pid)
        value.pop("found", None)
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    read_state.__annotations__["request"] = Request
    update_state.__annotations__["request"] = Request
    delete_archived.__annotations__["request"] = Request
    app.add_api_route("/desktop/archived-sessions/delete", delete_archived, methods=["POST"], include_in_schema=False)
    artifacts.__annotations__["request"] = Request
    artifacts.__annotations__["pid"] = str
    app.add_api_route("/desktop/workspace", read_state, methods=["GET"], include_in_schema=False)
    app.add_api_route("/desktop/workspace", update_state, methods=["POST"], include_in_schema=False)
    app.add_api_route("/desktop/projects/{pid}/artifacts", artifacts, methods=["GET"], include_in_schema=False)
