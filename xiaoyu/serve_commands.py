"""Validated GUI commands mapped onto existing collaboration tools.

No client supplied archive handle or filesystem path is accepted. Preview is a
consistency check, never an approval grant; execution still uses Agent policy.
"""
from __future__ import annotations

from . import serve_ownership

import copy
import hashlib
import json
import os
import stat
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import agents, tokens, worktree
from .config import user_config_dir
from .qixiang import MAX_ITEMS, MIN_NEW_ITEMS


class CommandError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Prepared:
    args: dict[str, Any]
    command: dict[str, Any]
    preview: dict[str, Any]
    archives: dict[str, Any]


def identity(body: dict[str, Any]) -> str:
    return "qixiang:" + json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _specs(session: Any) -> dict[str, Any]:
    tool = session.agent.toolbox.get("qixiang")
    if tool is None or not tool.available():
        return {}
    advertised = tool.parameters.get("properties", {}).get("spec", {}).get("enum", [])
    return {spec.name: spec for spec in getattr(session.agent, "_mounted_agent_specs", ())
            if spec.name in advertised}


def _ready(session: Any) -> str:
    if problem := serve_ownership.reason(session):
        return problem
    if session.busy:
        return "当前会话正在执行，请等待完成或停止后再操作。"
    if not _specs(session):
        return "此会话没有已加载且可用的七襄执行规格。"
    if session.agent.plan_mode:
        return "当前处于规划模式，批准退出规划后才能执行。"
    if (session.history_warning or not session.agent.session_log or not session.agent.session_log.complete
            or not session.weaves.integrity or not session.weave_controls.integrity):
        return "会话持久化不可用，请先核对并恢复服务。"
    if session.check_budget():
        return "当前会话预算已耗尽。"
    return ""


def _repository(workspace: Path) -> None:
    if worktree.git_root(workspace) is None:
        raise CommandError("此规格需要独立工作树，当前工作区不在 Git 仓库内。", 409)
    try:
        result = worktree._run_git(["rev-parse", "--verify", "HEAD"], cwd=workspace)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandError("无法检查工作树隔离条件。", 409) from exc
    if result.returncode:
        raise CommandError("工作区没有可用于隔离执行的有效 Git HEAD。", 409)


def _archive(session: Any, spec: Any, item: dict[str, Any]) -> tuple[Any, str]:
    rid = item.get("run_id")
    store = getattr(session.agent, "subagent_runs", None)
    if not isinstance(rid, str) or not agents._RUN_ID_RE.fullmatch(rid) or store is None:
        raise CommandError("该条目没有可恢复的委托存档。", 409)
    own = store.directory()
    directories = ([own] if own is not None else []) + list(store.sources)
    record = None
    digest = ""
    for directory in directories:
        path = directory / f"{rid}.json"
        try:
            if path.is_symlink() or directory.is_symlink():
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                raw = handle.read()
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(payload, dict) or payload.get("id") != rid:
                continue
            record = agents._decode_run(payload, rid)
            if record is not None:
                digest = hashlib.sha256(raw).hexdigest()
                break
        except (OSError, ValueError):
            continue
    if record is None:
        raise CommandError("条目的持久化存档已丢失或不可读取，不能续跑。", 409)
    if record.spec_name != spec.name:
        raise CommandError("条目存档与批次执行规格不一致。", 409)
    if len(record.messages) < 2 or tokens.estimate_messages(record.messages) >= int(session.agent.config.context_limit * agents._SAFE_RESUME_RATIO):
        raise CommandError("条目上下文为空或超过可恢复窗口，请新建任务。", 409)
    try:
        session.agent.registry.resolve(record.model)
    except Exception as exc:
        raise CommandError("条目原模型当前不可用，不能静默更换模型续跑。", 409) from exc
    workspace = session.workspace.resolve()
    if record.isolated or record.worktree is not None:
        _repository(workspace)
        root = worktree.git_root(workspace)
        slug = f"{root.name}-{hashlib.sha256(str(root).encode('utf-8')).hexdigest()[:8]}"
        allowed = (user_config_dir() / "worktrees" / slug).resolve()
        if record.worktree is None or not record.worktree.is_dir() or not (record.worktree / ".git").is_file():
            raise CommandError("原隔离工作树已丢失或被移动，不能保留原文件续跑，请新建任务。", 409)
        for candidate in (record.worktree, record.workdir):
            if candidate is not None and (not candidate.is_dir() or candidate.is_symlink() or not candidate.resolve().is_relative_to(allowed)):
                raise CommandError("条目隔离目录不属于当前项目，拒绝续跑。", 409)
        try:
            original = worktree._run_git(["rev-parse", "--git-common-dir"], cwd=workspace)
            resumed = worktree._run_git(["rev-parse", "--git-common-dir"], cwd=record.worktree)
            original_git = (workspace / original.stdout.strip()).resolve()
            resumed_git = (record.worktree / resumed.stdout.strip()).resolve()
            if original.returncode or resumed.returncode or original_git != resumed_git:
                raise CommandError("原隔离工作树不再属于当前仓库，拒绝续跑。", 409)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CommandError("无法验证原隔离工作树，请检查目录后重试。", 409) from exc
    elif record.workdir is not None and record.workdir.resolve() != workspace:
        raise CommandError("条目工作区与当前会话不一致，拒绝续跑。", 409)
    # Snapshot the exact validated archive before fan-out; no later filesystem
    # lookup should replace a reviewed selection with a different archive.
    return copy.deepcopy(record), digest


def prepare(session: Any, body: dict[str, Any], *, require_preview: bool = False) -> Prepared:
    reason = _ready(session)
    if reason:
        raise CommandError(reason, 409)
    if not isinstance(body, dict):
        raise CommandError("请求必须是对象。")
    request = {key: value for key, value in body.items() if key != "preview_token"}
    specs = _specs(session)
    archives: dict[str, Any] = {}
    fingerprints: dict[str, str] = {}
    source = request.get("source_batch_id")
    if source is not None:
        if set(request) != {"source_batch_id", "item_ids", "instruction"}:
            raise CommandError("续跑仅接受 source_batch_id、item_ids、instruction。")
        if not isinstance(source, str) or source not in session.weaves.batches:
            raise CommandError("来源批次不属于当前会话或已经不可用。", 404)
        batch = session.weaves.batches[source]
        if not batch.get("final") or batch.get("interrupted_by_restart"):
            raise CommandError("来源批次没有可靠结算，请先检查成果。", 409)
        spec = specs.get(batch.get("spec"))
        if spec is None:
            raise CommandError("来源批次的执行规格当前不可用。", 409)
        ids, instruction = request.get("item_ids"), request.get("instruction")
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_ITEMS
                or not all(isinstance(value, str) and value for value in ids) or len(set(ids)) != len(ids)):
            raise CommandError("请选择 1 至 64 个不同的批次条目。")
        if not isinstance(instruction, str) or not instruction.strip():
            raise CommandError("续跑补充说明不能为空。")
        known = {item["id"]: item for item in batch["items"]}
        selected = []
        resume: dict[str, str] = {}
        for item_id in ids:
            item = known.get(item_id)
            if item is None:
                raise CommandError("选择包含不属于来源批次的条目。")
            record, fingerprint = _archive(session, spec, item)
            if record.id in resume:
                raise CommandError("不同条目指向了相同存档，拒绝重复续跑。", 409)
            archives[record.id] = record
            fingerprints[record.id] = fingerprint
            resume[record.id] = instruction
            selected.append({"label": item.get("label") or item_id, "task": instruction})
        args = {"spec": spec.name, "resume": resume}
        command = {"kind": "qixiang", "operation": "resume", "spec": spec.name,
                   "source_batch_id": source, "item_ids": ids}
    else:
        if set(request) != {"spec", "prompt_template", "items"}:
            raise CommandError("新批仅接受 spec、prompt_template、items。")
        spec = specs.get(request.get("spec")) if isinstance(request.get("spec"), str) else None
        if spec is None:
            raise CommandError("执行规格没有在当前会话加载。")
        template, items = request.get("prompt_template"), request.get("items")
        if not isinstance(template, str) or "{{item}}" not in template:
            raise CommandError("任务模板必须包含 {{item}} 占位符。")
        if (not isinstance(items, list) or not MIN_NEW_ITEMS <= len(items) <= MAX_ITEMS
                or not all(isinstance(item, str) and item.strip() for item in items)):
            raise CommandError("新批需要 2 至 64 项非空任务。")
        prompts = [template.replace("{{item}}", item) for item in items]
        if len(set(prompts)) != len(prompts):
            raise CommandError("模板展开后的任务不能重复。")
        if not spec.readonly:
            _repository(session.workspace)
        args = {"spec": spec.name, "prompt_template": template, "items": list(items)}
        command = {"kind": "qixiang", "operation": "new", "spec": spec.name}
        selected = [{"label": item, "task": prompt} for item, prompt in zip(items, prompts)]
    try:
        session.agent.registry.resolve(spec.model or session.agent.config.model)
    except Exception as exc:
        raise CommandError("执行规格使用的模型当前不可用。", 409) from exc
    review = {"request": request, "spec": asdict(spec), "archives": fingerprints,
              "workspace": str(session.workspace), "model": session.agent.config.model}
    token = hashlib.sha256(json.dumps(review, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if require_preview and body.get("preview_token") != token:
        raise CommandError("任务配置或来源存档已变化，请重新预览后提交。", 409)
    return Prepared(args, command, {"operation": command["operation"], "spec": spec.name,
                    "count": len(selected), "items": selected, "preview_token": token}, archives)


def capabilities(session: Any) -> dict[str, Any]:
    specs = _specs(session)
    reason = _ready(session)
    batches = []
    for batch in session.weaves.snapshot():
        entries = []
        spec = specs.get(batch.get("spec"))
        for item in batch["items"]:
            item_reason = reason
            if not item_reason:
                if not spec:
                    item_reason = "执行规格当前不可用。"
                elif not batch.get("final") or batch.get("interrupted_by_restart"):
                    item_reason = "来源批次尚未可靠结算。"
                else:
                    try:
                        _archive(session, spec, item)
                    except CommandError as exc:
                        item_reason = str(exc)
            entries.append({"id": item["id"], "can_resume": not item_reason, "resume_reason": item_reason})
        batches.append({"batch_id": batch["batch_id"], "items": entries})
    return {"available": bool(specs), "reason": reason,
            "specs": [{"name": spec.name, "description": spec.description,
                       "readonly": spec.readonly, "default_isolation": "none" if spec.readonly else "worktree"}
                      for spec in specs.values()],
            "min_items": MIN_NEW_ITEMS, "max_items": MAX_ITEMS,
            "can_start": not reason, "can_resume": any(item["can_resume"] for batch in batches for item in batch["items"]),
            "batches": batches}
