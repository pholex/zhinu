"""Read-only previews for explicit orchestration commands through existing tools.

A preview grants no permission. The runtime calls Prepared.guard under its
mutation lock after tool approval; it rechecks the reviewed facts and supplies
validated state/archive bytes without a second pathname lookup.
"""
from __future__ import annotations

from . import serve_ownership

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import chenshu, session_log, tokens, worktree
from .config import EFFORT_LEVELS
from .serve_commands import CommandError

MAX_MISSIONS = 32
MAX_STATE_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024


@dataclass
class Prepared:
    tool_name: str
    args: dict[str, Any]
    label: str
    command: dict[str, Any]
    preview: dict[str, Any]
    guard: Callable[..., dict[str, Any]]


def identity(body: dict[str, Any]) -> str:
    return "chenshu:" + json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _runtime(session: Any) -> Any:
    runtime = getattr(session.agent, "chenshu", None)
    if runtime is None or session.agent.toolbox.get("chenshu_init") is None:
        raise CommandError("当前会话没有加载宸枢能力。", 409)
    return runtime


def _ready(session: Any) -> None:
    if problem := serve_ownership.reason(session):
        raise CommandError(problem, 503)
    runtime = _runtime(session)
    checker = getattr(session, "fleet_owner_check", None)
    if checker is not None and (reason := checker()):
        raise CommandError(reason, 409)
    if session.busy:
        raise CommandError("当前会话正在执行，请等待本轮完成。", 409)
    if runtime._closing:
        raise CommandError("当前宸枢运行时正在关闭。", 409)
    if session.agent.plan_mode:
        raise CommandError("当前处于规划模式，批准退出后才能发起成员任务。", 409)
    if (not getattr(getattr(session, "cfg", None), "persist", True)
            or not getattr(getattr(session, "fleets", None), "integrity", True)
            or not getattr(getattr(session, "fleet_controls", None), "integrity", True)
            or bool(getattr(session, "closing", False))):
        raise CommandError("会话或协作记录不可可靠写入，请先恢复服务。", 409)
    if session.history_warning or not session.agent.session_log or not session.agent.session_log.complete:
        raise CommandError("会话持久化不可用，请先恢复服务。", 409)
    if session.check_budget():
        raise CommandError("当前会话预算已耗尽。", 409)


def _lease_free(runtime: Any) -> None:
    if runtime._host_lease is not None:
        return
    path = session_log.lock_path(runtime.root / "host-owner")
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CommandError("宸枢归属锁不可读取。", 409) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or not session_log._try_lock(fd):
            raise CommandError("另一进程正在持有此宸枢工作区。", 409)
    finally:
        os.close(fd)


def release_owner(session: Any) -> None:
    """Release only after all member threads have actually left the runtime."""
    runtime = getattr(session.agent, "chenshu", None)
    if runtime is None:
        return
    with runtime.lock:
        if any(thread.is_alive() for thread in runtime._threads.values()):
            return
        if runtime._host_lease is not None:
            runtime._host_lease.close()
            runtime._host_lease = None


def _git_raw(workspace: Path, *args: str) -> str:
    try:
        result = worktree._run_git(list(args), cwd=workspace)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("无法核对仓库状态。", 409) from exc
    if result.returncode:
        raise CommandError("仓库状态不满足本次操作。", 409)
    return result.stdout


def _git(workspace: Path, *args: str) -> str:
    return _git_raw(workspace, *args).strip()


def _read_json(path: Path, root: Path, limit: int, *, missing: bool = False) -> tuple[Any, str]:
    # Validate each parent and open the final file without following a link.
    current = path.parent
    while current != root:
        if current.is_symlink() or not current.is_relative_to(root):
            raise CommandError("协议或存档目录不能使用符号链接。", 409)
        current = current.parent
    if root.is_symlink():
        raise CommandError("工作目录不能使用符号链接。", 409)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if missing:
            return None, "missing"
        raise CommandError("成员存档不存在，请检查历史记录。", 409) from None
    except OSError as exc:
        raise CommandError("协议或存档不可安全读取。", 409) from exc
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > limit:
            raise CommandError("协议或存档必须是大小受限的普通独立文件。", 409)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise CommandError("协议或存档超过读取上限。", 409)
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        if isinstance(exc, CommandError):
            raise
        raise CommandError("协议或存档内容损坏，不能执行。", 409) from exc
    finally:
        os.close(fd)
    return payload, hashlib.sha256(raw).hexdigest()


def _state(runtime: Any) -> tuple[dict[str, Any] | None, str]:
    payload, digest = _read_json(runtime._state_path(), runtime.config.workspace, MAX_STATE_BYTES, missing=True)
    if payload is None:
        return None, digest
    try:
        if (not isinstance(payload, dict) or not isinstance(payload.get("base_branch"), str)
                or not payload["base_branch"] or not isinstance(payload.get("missions"), list)
                or not isinstance(payload.get("members"), list)
                or len(payload["missions"]) > 512 or len(payload["members"]) > 256):
            raise ValueError()
        missions = [chenshu.Mission(**raw) for raw in payload["missions"]]
        members = [chenshu.Member(**raw) for raw in payload["members"]]
        if (len({m.id for m in missions}) != len(missions)
                or len({m.name for m in members}) != len(members)
                or any(not isinstance(m.id, str) or not m.id or m.kind not in chenshu.MISSION_KINDS for m in missions)
                or any(not isinstance(m.name, str) or not chenshu._NAME_RE.fullmatch(m.name)
                       or m.name in {chenshu.CHENSHU, chenshu.BROADCAST}
                       or m.kind not in {"worker", "reviewer"} for m in members)):
            raise ValueError()
        ids = {m.id for m in missions}
        if [m.id for m in missions] != [f"M{i + 1}" for i in range(len(missions))]:
            raise ValueError()
        previous: set[str] = set()
        for mission in missions:
            if mission.status not in {"pending", "active", "completed", "merged"}:
                raise ValueError()
            if not isinstance(mission.deps, (list, tuple)) or any(dep not in previous for dep in mission.deps):
                raise ValueError()
            previous.add(mission.id)
            for key in ("title", "branch", "worktree", "status", "owner"):
                if not isinstance(getattr(mission, key), str):
                    raise ValueError()
            for key in ("scope", "deps", "notes"):
                values = getattr(mission, key)
                if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
                    raise ValueError()
        roster = {m.name: m for m in members}
        for mission in missions:
            if mission.owner and (mission.owner not in roster or roster[mission.owner].kind != "worker"
                                  or roster[mission.owner].mission_id != mission.id):
                raise ValueError()
        references = ids | {m.branch for m in missions if m.branch}
        for member in members:
            if (member.status not in {"running", "done", "failed", "retired"}
                    or (member.kind == "worker" and member.mission_id not in ids)
                    or (member.kind == "reviewer" and member.review_target not in references)):
                raise ValueError()
            if any(not isinstance(getattr(member, field), str) for field in
                   ("mission_id", "review_target", "status", "model", "run_id", "effort")):
                raise ValueError()
    except (TypeError, ValueError, AttributeError) as exc:
        raise CommandError("旧宸枢状态格式不完整，不能静默忽略条目后接管。", 409) from exc
    return payload, digest


def _worktree(runtime: Any, mission: Any, *, required: bool) -> dict[str, str]:
    if mission.kind != "build":
        return {}
    if not mission.worktree:
        if required:
            raise CommandError("原成员的隔离工作树已缺失，不能重建后冒充原任务续跑。", 409)
        return {}
    path = Path(mission.worktree)
    root = worktree.git_root(runtime.config.workspace)
    if root is None:
        raise CommandError("当前目录不在 Git 仓库内。", 409)
    slug = f"{root.name}-{hashlib.sha256(str(root).encode()).hexdigest()[:8]}"
    allowed = (worktree.user_config_dir() / "worktrees" / slug).resolve()
    if not path.is_dir() or path.is_symlink() or not path.resolve().is_relative_to(allowed):
        raise CommandError("原隔离工作树不属于当前项目或已经丢失。", 409)
    own = (runtime.config.workspace / _git(runtime.config.workspace, "rev-parse", "--git-common-dir")).resolve()
    other = (path / _git(path, "rev-parse", "--git-common-dir")).resolve()
    if own != other or _git(path, "branch", "--show-current") != mission.branch:
        raise CommandError("原隔离工作树的仓库或分支已变化。", 409)
    listing = _git_raw(runtime.config.workspace, "worktree", "list", "--porcelain", "-z")
    record = next((entry.split("\0") for entry in listing.split("\0\0")
                   if worktree.listed(entry.split("\0", 1)[0], path.resolve())), None)
    if record is None or any(line.startswith(("locked", "prunable")) for line in record):
        raise CommandError("原工作树登记缺失、已锁定或不可用。", 409)
    return {"path": str(path), "head": _git(path, "rev-parse", "HEAD"),
            "status": _git(path, "status", "--porcelain", "--untracked-files=normal")}


def _resume(runtime: Any, session: Any, member: Any) -> tuple[list[dict[str, Any]], str]:
    if member.status == "running" or (thread := runtime._threads.get(member.name)) is not None and thread.is_alive():
        raise CommandError("成员仍在执行，不能同时续跑同一份上下文。", 409)
    if not member.run_id or not member.model:
        raise CommandError("旧记录没有精确成员轮次或模型，不能作为可验证的续跑来源。", 409)
    payload, digest = _read_json(runtime._archive_path(member.name), runtime.config.workspace, MAX_ARCHIVE_BYTES)
    if not member.archive_sha256 or digest != member.archive_sha256:
        raise CommandError("存档无法与所选成员轮次精确对齐，不能验证续跑来源。", 409)
    if (not isinstance(payload, list) or len(payload) < 2
            or not all(isinstance(m, dict) and m.get("role") in {"system", "user", "assistant", "tool"} for m in payload)
            or payload[0].get("role") != "system"):
        raise CommandError("成员存档不是完整对话。", 409)
    if tokens.estimate_messages(payload) >= int(session.agent.config.context_limit * .8):
        raise CommandError("成员存档超过安全续跑窗口，请另建任务。", 409)
    pending: set[str] = set()
    for message in payload:
        if message["role"] == "assistant":
            calls = message.get("tool_calls") or []
            if pending or not isinstance(calls, list):
                raise CommandError("成员存档含未结算工具调用，不能续跑。", 409)
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"] or call["id"] in pending:
                    raise CommandError("成员存档工具调用格式不可读取。", 409)
                pending.add(call["id"])
        elif message["role"] == "tool":
            if message.get("tool_call_id") not in pending:
                raise CommandError("成员存档工具结果没有对应调用。", 409)
            pending.remove(message["tool_call_id"])
        elif pending:
            raise CommandError("成员存档含未结算工具调用，不能续跑。", 409)
    if pending:
        raise CommandError("成员存档含未结算工具调用，不能续跑。", 409)
    return payload, digest


def _string(body: dict[str, Any], key: str, limit: int, *, required: bool = False) -> str:
    value = body.get(key, "")
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise CommandError(f"{key} 必须是{'非空' if required else ''}字符串且不超过 {limit} 字符。")
    return value.strip()


def _block(blockers: list[dict[str, str]], code: str, message: str) -> None:
    if not any(item["code"] == code for item in blockers):
        blockers.append({"code": code, "message": message})


def _member_activity(runtime: Any, mission: Any | None = None) -> tuple[bool, bool]:
    members = runtime.members if mission is None else [m for m in runtime.members
        if m.mission_id == mission.id or m.review_target in {mission.id, mission.branch}]
    live = any((t := runtime._threads.get(m.name)) is not None and t.is_alive() for m in members)
    unknown = any(m.status == "running" and not ((t := runtime._threads.get(m.name)) is not None and t.is_alive())
                  or m.status == "retired" and m.execution_outcome == "unknown" for m in members)
    return live, unknown


def _review_proof(runtime: Any, mission: Any) -> tuple[dict[str, Any] | None, str]:
    directory = runtime.root / "reviews"
    if directory.is_symlink() or runtime.root.is_symlink() or runtime.root.parent.is_symlink():
        raise CommandError("评审目录不能是符号链接。", 409)
    if not directory.exists():
        return None, "missing"
    paths = list(directory.glob(f"{mission.id}-r*.md"))
    if len(paths) > 1000:
        raise CommandError("评审记录过多，无法安全核对最新轮次。", 409)
    numbered = []
    for path in paths:
        match = re.fullmatch(re.escape(mission.id) + r"-r([1-9][0-9]*)\.md", path.name)
        if not match:
            raise CommandError("评审记录名称无法核对。", 409)
        numbered.append((int(match[1]), path))
    if not numbered:
        return None, "missing"
    number, path = max(numbered)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 256 * 1024:
                raise ValueError("not a bounded regular file")
            with os.fdopen(fd, "rb", closefd=False) as handle:
                raw = handle.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("review too large")
        finally:
            os.close(fd)
        #  旧版本在 Windows 上按文本模式写出的记录是 CRLF；摘要仍按原始字节算
        text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
        header, sep, _ = text[4:].partition("\n---\n") if text.startswith("---\n") else ("", "", "")
        values = {}
        for line in header.splitlines():
            key, colon, value = line.partition(":")
            if not colon or key in values:
                raise ValueError("invalid header")
            values[key.strip()] = value.strip()
        if (not sep or values.get("target") != mission.id or values.get("branch") != mission.branch
                or values.get("round") != str(number)
                or not re.fullmatch(r"[0-9a-f]{40,64}", values.get("reviewed_commit", ""))):
            raise ValueError("review identity mismatch")
        return values, hashlib.sha256(raw).hexdigest()
    except (OSError, ValueError) as exc:
        raise CommandError("最新评审记录无法安全读取或身份不匹配。", 409) from exc


def _diff_preview(workspace: Path, base: str, target: str) -> dict[str, Any]:
    from . import gitsafe
    from .tools import _subprocess_hardening
    import threading

    limit = 64 * 1024
    argv, env = gitsafe.prepare(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", base, target, "--"], workspace)
    try:
        with subprocess.Popen(argv, cwd=workspace, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, **_subprocess_hardening()) as process:
            timed_out = threading.Event()
            def expire() -> None:
                timed_out.set()
                try:
                    process.kill()
                except OSError:
                    pass
            timer = threading.Timer(10, expire)
            timer.daemon = True
            timer.start()
            try:
                assert process.stdout is not None
                data = process.stdout.read(limit + 1)
                truncated = len(data) > limit
                if truncated:
                    process.kill()
                process.wait(timeout=2)
            finally:
                timer.cancel()
            if timed_out.is_set() or (process.returncode and not truncated):
                return {"text": "", "available": False, "truncated": False, "note": "差异读取失败或超时。"}
            return {"text": data[:limit].decode("utf-8", errors="replace"), "available": True,
                    "truncated": truncated, "note": "仅显示前 64 KiB。" if truncated else ""}
    except (OSError, subprocess.TimeoutExpired):
        return {"text": "", "available": False, "truncated": False, "note": "差异不可读取。"}


def _merge_facts(session: Any, runtime: Any, mission: Any) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    blockers: list[dict[str, str]] = []
    root = worktree.git_root(session.workspace)
    if root is None:
        raise CommandError("当前工作区不属于 Git 仓库。", 409)
    deps = [{"id": mid, "status": (runtime.mission(mid).status if runtime.mission(mid) else "missing"),
             "ready": runtime.mission(mid) is not None and runtime.mission(mid).status == "merged"} for mid in mission.deps]
    view = {"mission": asdict(mission), "base_branch": runtime.base_branch,
            "base_head": "", "target_branch": mission.branch, "target_head": "", "merge_base": "",
            "review": {"round": 0, "verdict": "", "reviewed_commit": "", "current": False},
            "deps": deps, "changed_files": [], "changed_files_truncated": False,
            "diff": {"text": "", "available": False, "truncated": False, "note": ""},
            "conflict_check": "not_checked"}
    proof: dict[str, Any] = {}
    if mission.status == "merged":
        _block(blockers, "already_merged", "使命已经完结，无需重复合并。")
    if any(not dep["ready"] for dep in deps):
        _block(blockers, "deps_unmerged", "依赖使命尚未完结，请先按依赖顺序处理。")
    live, unknown = _member_activity(runtime, mission)
    if live:
        _block(blockers, "member_running", "该使命仍有成员或评审员在执行。")
    if unknown:
        _block(blockers, "member_unknown", "该使命成员的存活状态无法确认。")
    try:
        view["base_head"] = _git(root, "rev-parse", "--verify", f"refs/heads/{runtime.base_branch}")
        proof["workspace_head"] = _git(root, "rev-parse", "HEAD")
        proof["workspace_status"] = _git(root, "status", "--porcelain", "--untracked-files=normal")
        proof["workspace_branch"] = _git(root, "branch", "--show-current")
        if proof["workspace_branch"] != runtime.base_branch:
            _block(blockers, "base_mismatch", "主工作区未停在已登记的基础分支。")
        if proof["workspace_status"]:
            _block(blockers, "workspace_dirty", "主工作区存在未提交或未跟踪改动，先处理后合并。")
        if mission.kind == "survey":
            if mission.status != "completed":
                _block(blockers, "survey_incomplete", "调研使命必须先完成，才能登记完结。")
            view["diff"]["note"] = "调研使命只登记完结，不执行代码合并。"
        else:
            view["target_head"] = _git(root, "rev-parse", "--verify", f"refs/heads/{mission.branch}")
            view["merge_base"] = _git(root, "merge-base", view["base_head"], view["target_head"])
            view["diff"] = _diff_preview(root, view["merge_base"], view["target_head"])
            record, digest = _review_proof(runtime, mission)
            proof["review"], proof["review_digest"] = record, digest
            if record is None:
                _block(blockers, "review_missing", "尚未提交评审。")
            else:
                current = record["reviewed_commit"] == view["target_head"]
                view["review"] = {"round": int(record["round"]), "verdict": record.get("verdict", ""),
                                  "reviewed_commit": record["reviewed_commit"], "current": current}
                if record.get("verdict") != "clean":
                    _block(blockers, "review_not_clean", "最新评审没有给出 clean 结论。")
                if not current:
                    _block(blockers, "review_stale", "评审后目标分支已改变，需要重新评审。")
            names = _git_raw(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only", "-z",
                         view["merge_base"], view["target_head"], "--").split("\0")
            files = [name for name in names if name]
            proof["files"] = files
            view["changed_files"] = files[:200]
            view["changed_files_truncated"] = len(files) > 200
            if not files:
                _block(blockers, "empty_diff", "目标分支没有可交付的已提交改动。")
            if any(not chenshu.scope_match(name, mission.scope) for name in files):
                _block(blockers, "out_of_scope", "目标分支改动超出使命 scope。")
            if mission.worktree:
                proof["worktree"] = _worktree(runtime, mission, required=True)
                if proof["worktree"]["status"]:
                    _block(blockers, "worktree_dirty", "成员工作树还有未提交改动，交付尚未完整提交。")
            if not view["diff"]["available"]:
                _block(blockers, "diff_unavailable", "无法读取当前差异，不能据此合并。")
    except CommandError as exc:
        _block(blockers, "proof_unavailable", str(exc))
    proof.update(base_head=view["base_head"], target_head=view["target_head"], merge_base=view["merge_base"], blockers=blockers)
    return view, proof, blockers


def _teardown_facts(runtime: Any) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, str]]]:
    blockers: list[dict[str, str]] = []
    live, unknown = _member_activity(runtime)
    if live:
        _block(blockers, "member_running", "仍有成员在执行；清理不会替你停止成员。")
    if unknown:
        _block(blockers, "member_unknown", "成员存活状态无法确认，不能删除工作树。")
    items, proofs = [], []
    paths: set[str] = set()
    for mission in runtime.missions:
        if not mission.worktree:
            continue
        item = {"mission_id": mission.id, "path": mission.worktree, "branch": mission.branch,
                "merged": mission.status == "merged", "dirty": None, "live": live if not unknown else None,
                "can_remove": False, "reason": ""}
        try:
            if mission.worktree in paths:
                raise CommandError("多个使命指向同一工作树，不能安全清理。", 409)
            paths.add(mission.worktree)
            proof = _worktree(runtime, mission, required=True)
            # Ignored files would also be removed by git worktree remove.
            status = _git(Path(mission.worktree), "status", "--porcelain", "--ignored", "--untracked-files=all")
            proof["all_status"] = status
            proofs.append(proof)
            item["dirty"] = bool(status)
            if status:
                item["reason"] = "包含未提交、未跟踪或被忽略的文件。"
                _block(blockers, "worktree_dirty", "至少一棵工作树存在本地文件，整次清理已停用。")
            elif live or unknown:
                item["reason"] = "须先确认全部成员已结束。"
            else:
                item["can_remove"] = True
        except CommandError as exc:
            item["reason"] = str(exc)
            _block(blockers, "worktree_unknown", "工作树归属或状态不可验证，整次清理已停用。")
        items.append(item)
    return {"worktrees": items, "retained_branches": [m.branch for m in runtime.missions if m.branch],
            "archives_retained": True}, {"worktrees": proofs, "blockers": blockers}, blockers


def changes(session: Any, board_id: str, mission_id: str) -> dict[str, Any]:
    runtime = _runtime(session)
    with runtime.lock:
        if not runtime.active or board_id != runtime._board_id:
            raise CommandError("只能检查当前运行时的使命；历史看板不会自动接管。", 409)
        mission = runtime.mission(mission_id)
        if mission is None:
            raise CommandError("使命不属于当前看板。", 404)
        merge, _, blockers = _merge_facts(session, runtime, mission)
        try:
            _ready(session)
            _lease_free(runtime)
        except CommandError as exc:
            _block(blockers, "session_unavailable", str(exc))
        return {"board_id": board_id, "revision": runtime._view_revision, "mission_id": mission_id,
                "can_merge": not blockers, "blockers": blockers, "merge": merge}


def _facts(session: Any, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    runtime = _runtime(session)
    if runtime._closing:
        raise CommandError("宸枢运行时正在关闭。", 409)
    action = request.get("action")
    allowed = {"init": {"action", "mode"}, "plan": {"action", "board_id", "missions"},
               "spawn": {"action", "board_id", "name", "kind", "mission_id", "review_target", "model", "effort", "instructions"},
               "resume": {"action", "board_id", "name", "run_id", "effort", "instructions"},
               "merge": {"action", "board_id", "mission_id"}, "teardown": {"action", "board_id"}}
    if not isinstance(action, str) or action not in allowed or set(request) - allowed[action]:
        raise CommandError("宸枢命令字段无效。")
    head = _git(session.workspace, "rev-parse", "--verify", "HEAD")
    branch = _git(session.workspace, "branch", "--show-current")
    if not branch:
        raise CommandError("主工作区处于 detached HEAD，请先切回分支。", 409)
    state, state_hash = _state(runtime)
    _lease_free(runtime)
    review: dict[str, Any] = {"request": request, "runtime_id": runtime._board_id,
        "revision": runtime._view_revision, "active": runtime.active,
        "missions": [asdict(m) for m in runtime.missions], "members": [asdict(m) for m in runtime.members],
        "head": head, "branch": branch, "state_hash": state_hash,
        "config": asdict(session.agent.config), "workspace": str(session.workspace)}
    preview: dict[str, Any] = {"action": action, "board_id": runtime._board_id,
        "revision": runtime._view_revision, "repo_head": head, "base_branch": runtime.base_branch or branch,
        "missions": [], "member": None, "can_execute": True, "blockers": [],
        "existing": None if state is None else {"state_id": state.get("state_id", ""),
            "missions_count": len(state["missions"]), "members_count": len(state["members"])},
        "warnings": ["预览不授予工具权限；执行时仍按当前权限和审批规则处理。"]}
    checked: dict[str, Any] = {"state": state, "seed": None}
    if action == "init":
        mode = request.get("mode")
        if mode not in {"new", "adopt"}:
            raise CommandError("请选择明确的新建或接管模式。")
        if runtime.active or runtime._live_count():
            raise CommandError("当前运行时已启用，不能重复初始化或接管。", 409)
        if (mode == "new") != (state is None):
            raise CommandError("工作区已有状态须明确接管；没有状态时只能新建。", 409)
        if state is not None:
            if state.get("host_protocol") != 1 and any(m.get("status") == "running" for m in state["members"]):
                raise CommandError("旧运行时没有可验证的归属锁且成员记录仍在运行，不能安全接管。", 409)
            for raw in state["missions"]:
                mission = chenshu.Mission(**raw)
                if mission.worktree:
                    _worktree(runtime, mission, required=True)
            if state.get("host_protocol") != 1:
                preview["warnings"].append("旧记录不支持归属锁；须先结束使用该工作区的其他终端会话。")
            _git(session.workspace, "rev-parse", "--verify", f"refs/heads/{state['base_branch']}")
            preview["base_branch"] = state["base_branch"]
        args, tool = {}, "chenshu_init"
        label = "接管已有宸枢工作区" if mode == "adopt" else "新建宸枢工作区"
    else:
        if not runtime.active or request.get("board_id") != runtime._board_id:
            raise CommandError("目标不是当前已启用的看板；历史记录须先明确接管。", 409)
        if action in {"merge", "teardown"}:
            if action == "merge":
                mid = _string(request, "mission_id", 100, required=True)
                mission = runtime.mission(mid)
                if mission is None:
                    raise CommandError("使命不属于当前看板。", 404)
                view, proof, blockers = _merge_facts(session, runtime, mission)
                preview["merge"] = view
                checked["merge"] = proof
                args, tool, label = {"target": mid}, "chenshu_merge", f"合并使命 {mid}"
            else:
                view, proof, blockers = _teardown_facts(runtime)
                preview["teardown"] = view
                args, tool, label = {}, "chenshu_teardown", "清理工作树并结束协作"
            review[action] = proof
            preview.update(can_execute=not blockers, blockers=blockers)
        elif action == "plan":
            missions = request.get("missions")
            if not isinstance(missions, list) or not 1 <= len(missions) <= MAX_MISSIONS:
                raise CommandError("每次请登记 1 至 32 个使命。")
            for item in missions:
                if not isinstance(item, dict) or set(item) - {"title", "kind", "scope", "deps"}:
                    raise CommandError("使命仅接受 title、kind、scope、deps。")
                _string(item, "title", 300, required=True)
                if "kind" in item and item["kind"] not in {"build", "survey"}:
                    raise CommandError("使命 kind 必须是 build 或 survey。")
                for key in ("scope", "deps"):
                    values = item.get(key, [])
                    if not isinstance(values, list) or len(values) > 32 or not all(isinstance(v, str) and v.strip() and len(v) <= 200 for v in values):
                        raise CommandError(f"{key} 必须是最多 32 项的非空字符串数组。")
                if any(Path(v).is_absolute() or ".." in Path(v).parts or "\\" in v for v in item.get("scope", [])):
                    raise CommandError("scope 只能使用工作区内的相对路径或模式。")
            try:
                planned = chenshu._planned_missions(missions, runtime.missions)
            except chenshu.ChenshuError as exc:
                raise CommandError(str(exc)) from exc
            preview["missions"] = [asdict(m) for m in planned]
            args, tool, label = {"missions": copy.deepcopy(missions)}, "chenshu_plan", f"登记 {len(planned)} 个宸枢使命"
        else:
            name = _string(request, "name", 32, required=True)
            if not chenshu._NAME_RE.fullmatch(name) or name in {chenshu.CHENSHU, chenshu.BROADCAST}:
                raise CommandError("成员名称须为小写字母开头的 2–32 位标识，且不能使用保留名。")
            member = runtime.member(name)
            effort = _string(request, "effort", 32)
            instructions = _string(request, "instructions", 8000)
            if effort and effort not in EFFORT_LEVELS:
                raise CommandError("推理深度不在当前支持范围。")
            if runtime._live_count() >= max(1, runtime.config.chenshu_max_workers):
                raise CommandError("当前执行成员已达上限。", 409)
            if action == "resume":
                if member is None or _string(request, "run_id", 100, required=True) != member.run_id:
                    raise CommandError("成员执行代次已经改变或不存在。", 409)
                seed, digest = _resume(runtime, session, member)
                checked["seed"], review["archive_hash"] = seed, digest
                args = {"name": name, "kind": member.kind, "mission_id": member.mission_id,
                        "review_target": member.review_target, "model": member.model,
                        "effort": effort or member.effort, "instructions": instructions, "resume": True}
            else:
                if member is not None:
                    raise CommandError("成员名已经存在；请选择精确续跑或使用新名称。", 409)
                kind = _string(request, "kind", 20, required=True)
                if kind not in {"worker", "reviewer"}:
                    raise CommandError("成员 kind 必须是 worker 或 reviewer。")
                args = {"name": name, "kind": kind, "mission_id": _string(request, "mission_id", 100),
                        "review_target": _string(request, "review_target", 200),
                        "model": _string(request, "model", 512), "effort": effort, "instructions": instructions}
            if args["kind"] == "worker":
                if args["review_target"]:
                    raise CommandError("worker 只接受 mission_id。")
                mission = runtime.mission(args["mission_id"])
                if mission is None or mission.status == "merged":
                    raise CommandError("请选择尚未合并的有效使命。", 409)
                owner = runtime.member(mission.owner)
                if mission.owner != name and owner is not None and owner.status != "retired":
                    raise CommandError("使命已由其他成员认领。", 409)
                review["worktree"] = _worktree(runtime, mission, required=action == "resume")
            else:
                if args["mission_id"]:
                    raise CommandError("reviewer 只接受 review_target。")
                mission = runtime._mission_by_ref(args["review_target"])
                if mission is None or mission.kind != "build":
                    raise CommandError("请选择有实际分支的 build 使命进行评审。", 409)
                _git(session.workspace, "rev-parse", "--verify", f"refs/heads/{mission.branch}")
                review["review_tip"] = _git(session.workspace, "rev-parse", f"refs/heads/{mission.branch}")
            actual_model = args["model"] or session.agent.config.model
            try:
                session.agent.registry.resolve(actual_model)
            except Exception as exc:
                raise CommandError("所选成员模型当前不可用。", 409) from exc
            preview["member"] = {**args, "model": actual_model, "effort": args["effort"] or session.agent.config.effort,
                                  "source_run_id": member.run_id if member is not None else "",
                                  "workspace": mission.worktree if args["kind"] == "worker" and mission.kind == "build" else str(session.workspace)}
            if args["kind"] == "worker" and mission.kind == "build" and not mission.worktree:
                preview["member"]["workspace"] = ""
                preview["warnings"].append("执行时创建独立工作树；预览不会创建目录或分支。")
            preview["missions"] = [asdict(mission)]
            tool, label = "chenshu_spawn", ("续跑" if action == "resume" else "派遣") + f"成员 {name}"
    args = {key: value for key, value in args.items() if value != ""}
    mounted = session.agent.toolbox.get(tool)
    if mounted is None or not mounted.available():
        raise CommandError("本次操作对应的宸枢工具当前不可用。", 409)
    preview.update(tool=tool, args=args, label=label)
    return review, preview, checked


def prepare(session: Any, body: dict[str, Any], *, require_preview: bool = False) -> Prepared:
    _ready(session)
    if not isinstance(body, dict):
        raise CommandError("请求必须是对象。")
    request = copy.deepcopy({key: value for key, value in body.items() if key != "preview_token"})
    runtime = _runtime(session)
    with runtime.lock:
        review, preview, _ = _facts(session, request)
    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    token = digest(review)
    if require_preview and not preview["can_execute"]:
        raise CommandError("操作仍被阻止：" + "；".join(item["message"] for item in preview["blockers"]), 409)
    if require_preview and body.get("preview_token") != token:
        raise CommandError("看板、仓库或存档已变化，请重新预览。", 409)
    preview["preview_token"] = token if preview["can_execute"] else ""
    tool = preview["tool"]

    def guard(tool_name: str, actual_args: dict[str, Any] | None = None) -> dict[str, Any]:
        if problem := serve_ownership.reason(session):
            raise CommandError(problem, 503)
        if tool_name != tool:
            raise chenshu.ChenshuError("ERROR: 本次预览不允许执行其他宸枢工具。")
        if actual_args is not None:
            normalized = {key: value for key, value in actual_args.items()
                          if value != "" and not (key in {"resume", "force"} and value is False)}
            if normalized != preview["args"]:
                raise chenshu.ChenshuError("ERROR: 执行参数与已预览的宸枢命令不一致，请重新预览。")
        try:
            latest, current, checked = _facts(session, request)
            if not current["can_execute"]:
                raise CommandError("操作仍被阻止：" + "；".join(item["message"] for item in current["blockers"]), 409)
            if digest(latest) != token:
                raise CommandError("审批期间看板、仓库或存档已变化，请重新预览。", 409)
            checker = getattr(session, "fleet_owner_check", None)
            if checker is not None and (reason := checker(claim=True)):
                raise CommandError(reason, 409)
            if runtime._host_lease is None:
                try:
                    lease = session_log.SessionWriteLock(runtime.root / "host-owner")
                except (OSError, session_log.SessionLockedError) as exc:
                    raise CommandError("无法取得此宸枢工作区的独占归属锁。", 409) from exc
                try:
                    if _state(runtime)[1] != latest["state_hash"]:
                        raise CommandError("取得归属锁时工作区状态已变化，请重新预览。", 409)
                except BaseException:
                    lease.close()
                    raise
                runtime._host_lease = lease
            return checked
        except CommandError as exc:
            raise chenshu.ChenshuError("ERROR: " + str(exc)) from exc

    return Prepared(tool, copy.deepcopy(preview["args"]), preview["label"],
                    {"kind": "chenshu", "action": request["action"], "tool": tool,
                     "board_id": runtime._board_id}, preview, guard)


def capabilities(session: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"available": False, "reason": "", "board_id": "", "revision": 0,
        "can_init": False, "can_adopt": False, "can_plan": False, "can_spawn": False,
        "can_inspect_merge": False, "can_inspect_teardown": False,
        "can_merge": False, "can_teardown": False,
        "models": [], "default_model": session.agent.config.model, "efforts": list(EFFORT_LEVELS),
        "missions": [], "members": [], "next_mission_id": "M1"}
    try:
        runtime = _runtime(session)
        result["available"] = True
        with runtime.lock:
            result.update(board_id=runtime._board_id, revision=runtime._view_revision,
                          can_inspect_merge=runtime.active, can_inspect_teardown=runtime.active,
                          missions=[asdict(m) for m in runtime.missions],
                          members=[asdict(m) for m in runtime.members], next_mission_id=f"M{len(runtime.missions) + 1}")
        names = {session.agent.config.model}
        names.update(item.model for item in session.agent.registry.listing())
        names.update(session.agent.registry.remote_cached())
        result["models"] = sorted(names)
        _ready(session)
        with runtime.lock:
            state, _ = _state(runtime)
            _lease_free(runtime)
            if not runtime.active and state is not None and state.get("host_protocol") != 1 and any(m.get("status") == "running" for m in state["members"]):
                raise CommandError("旧成员的存活状态无法确认，不能安全接管。", 409)
            _git(session.workspace, "rev-parse", "--verify", "HEAD")
            result.update(can_merge=runtime.active and bool(runtime.missions), can_teardown=runtime.active,
                          can_init=not runtime.active and state is None,
                          can_adopt=not runtime.active and state is not None,
                          can_plan=runtime.active,
                          can_spawn=runtime.active and runtime._live_count() < max(1, runtime.config.chenshu_max_workers))
            for item in result["members"]:
                try:
                    _facts(session, {"action": "resume", "board_id": runtime._board_id,
                                     "name": item["name"], "run_id": item["run_id"]})
                    item.update(can_resume=True, resume_reason="")
                except CommandError as exc:
                    item.update(can_resume=False, resume_reason=str(exc))
    except CommandError as exc:
        result["reason"] = str(exc)
        for item in result["members"]:
            item.update(can_resume=False, resume_reason=str(exc))
    return result
