"""Authenticated inspection of files explicitly registered as task results."""
from __future__ import annotations

import asyncio
import base64
import binascii
import difflib
from pathlib import Path
from typing import Any

from .artifact_views import TEXT_BYTES, is_text, read_file
from .artifact_images import image_preview


def _text(data: bytes | None, note: str = "") -> dict[str, Any]:
    if data is None:
        return {"available": False, "text": "", "truncated": False, "note": note or "未保存此版本。"}
    if not is_text(data):
        return {"available": False, "text": "", "truncated": False, "note": "二进制文件不提供文本预览。"}
    return {"available": True, "text": data[:TEXT_BYTES].decode("utf-8", errors="replace"),
            "truncated": len(data) > TEXT_BYTES, "note": note}


def decode_record(record: Any) -> bytes | None:
    if not isinstance(record, dict) or record.get("available") is not True:
        return None
    encoded = record.get("base64")
    if not isinstance(encoded, str) or len(encoded) > (TEXT_BYTES + 2) // 3 * 4:
        return None
    try:
        data = base64.b64decode(encoded, validate=True)
        return data if len(data) <= TEXT_BYTES else None
    except (ValueError, binascii.Error):
        return None


def _resolve(session: Any, artifact_id: str, run_id: str, batch_id: str, item_id: str,
             contest_id: str = "", candidate_id: str = ""):
    from fastapi import HTTPException

    if (sum(map(bool, (run_id, batch_id, contest_id))) != 1
            or bool(batch_id) != bool(item_id) or bool(contest_id) != bool(candidate_id)):
        raise HTTPException(400, "请选择成果所属轮次、批次条目或比选候选。")
    if run_id:
        owner = next((run for run in session.runs if run.get("id") == run_id), None)
        artifacts = ((owner or {}).get("result") or {}).get("artifacts") or []
        previews = (owner or {}).get("artifact_previews") or []
        workspace = session.workspace
    elif batch_id:
        batch = session.weaves.batches.get(batch_id) or {}
        owner = next((item for item in batch.get("items", []) if item.get("id") == item_id), None)
        artifacts = (owner or {}).get("artifacts") or []
        previews = (owner or {}).get("artifact_previews") or []
        workspace = Path((owner or {}).get("workspace") or "")
    else:
        contest = session.contests.contests.get(contest_id) or {}
        owner = next((candidate for candidate in contest.get("candidates", [])
                      if candidate.get("id") == candidate_id), None)
        artifacts = (owner or {}).get("artifacts") or []
        previews = (owner or {}).get("artifact_previews") or []
        workspace = Path((owner or {}).get("workspace") or "")
    artifact = next((a for a in artifacts if isinstance(a, dict) and a.get("id") == artifact_id), None)
    if owner is None or artifact is None or not workspace.is_absolute():
        raise HTTPException(404, "此任务没有登记该成果。")
    preview = next((p for p in previews if isinstance(p, dict) and p.get("id") == artifact_id
                    and p.get("path") == artifact.get("path")), {})
    return dict(artifact), workspace, preview


def inspect(artifact: dict[str, Any], workspace: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    import hashlib

    delivered = decode_record(snapshot.get("delivered"))
    if delivered is not None and hashlib.sha256(delivered).hexdigest() != artifact.get("sha256"):
        delivered = None
    before = decode_record(snapshot.get("before"))
    warning = str(snapshot.get("note") or "")
    current_data = None
    current_hash = ""
    can_reveal = False
    version = "unavailable"
    current_note = ""
    try:
        current_data, current_hash = read_file(workspace, artifact["path"])
        version = "delivered" if current_hash == artifact.get("sha256") else "current_changed"
        can_reveal = True
        # Old runs may lack snapshots; matching the recorded digest proves these
        # bytes are the delivery, without silently substituting a changed file.
        if delivered is None and version == "delivered" and len(current_data) <= TEXT_BYTES:
            delivered = current_data
    except FileNotFoundError:
        version = "missing"
        current_note = "当前文件已不存在；已保存的交付快照仍可查看。"
    except (OSError, ValueError, KeyError, TypeError) as exc:
        current_note = f"当前文件无法安全读取：{exc}"
    before_text, delivered_text = _text(before), _text(delivered, (snapshot.get("delivered") or {}).get("note", ""))
    diff = {"available": False, "text": "", "truncated": False,
            "label": "本次执行前 → 交付时", "note": "缺少改前或交付文本快照，无法计算此次改动。"}
    if before_text["available"] and delivered_text["available"]:
        old, new = before_text["text"].splitlines(keepends=True), delivered_text["text"].splitlines(keepends=True)
        if len(old) <= 2000 and len(new) <= 2000:
            rendered = "".join(difflib.unified_diff(old, new, fromfile="改动前", tofile="交付时"))
            diff.update(available=True, text=rendered[:24000], truncated=len(rendered) > 24000,
                        note="" if rendered else "文本内容没有差异。")
        else:
            diff["note"] = "文本行数超过差异预览上限，请分别检查两个版本。"
    return {"artifact": artifact, "version": version, "delivered": delivered_text,
            "current": {**_text(current_data, current_note), "sha256": current_hash},
            "diff": diff, "can_reveal": can_reveal, "warning": warning,
            "image": image_preview(current_data, version)}


def register_artifact_routes(app: Any, guard: list, pick: Any) -> None:
    from fastapi import HTTPException

    @app.get("/session/{session_id}/artifacts/{artifact_id}", dependencies=guard,
             tags=["sessions"], operation_id="inspect_session_artifact")
    async def preview(session_id: str, artifact_id: str, run_id: str = "", batch_id: str = "", item_id: str = "",
                      contest_id: str = "", candidate_id: str = ""):
        artifact, workspace, snapshot = _resolve(pick(session_id), artifact_id, run_id, batch_id, item_id, contest_id, candidate_id)
        return await asyncio.to_thread(inspect, artifact, workspace, snapshot)

    @app.get("/session/{session_id}/artifacts/{artifact_id}/location", dependencies=guard,
             tags=["sessions"], operation_id="locate_session_artifact")
    async def location(session_id: str, artifact_id: str, run_id: str = "", batch_id: str = "", item_id: str = "",
                       contest_id: str = "", candidate_id: str = ""):
        artifact, workspace, _ = _resolve(pick(session_id), artifact_id, run_id, batch_id, item_id, contest_id, candidate_id)
        try:
            await asyncio.to_thread(read_file, workspace, artifact["path"])
        except (OSError, ValueError, KeyError) as exc:
            raise HTTPException(409, "成果文件已变化、缺失或不可安全定位，请刷新预览。") from exc
        return {"path": str(workspace / artifact["path"]), "workspace": str(workspace)}
