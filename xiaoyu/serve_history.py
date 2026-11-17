"""Persistent, chronological conversation and run views for service clients.

Read original message records, never the compacted model context. Journal line
numbers are stable identifiers. Runtime events remain a separate, bounded feed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .media import is_injected_message, is_peer_message, text_of

START = "serve.run.started"
SETTLED = "serve.run.settled"


def read(path: Path | None, *, before: int | None = None, include_activity: bool = False) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    runs: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    active: str | None = None
    legacy = False
    tool_calls: dict[str, dict[str, Any]] = {}
    pending_model: dict[str, Any] | None = None
    if path is None:
        return {"messages": [], "runs": [], "warning": "此会话没有持久化日志。",
                "available": False, "integrity": False}
    available = True
    integrity = True
    from .agent import SYNTHETIC_USER_TEXTS

    def lines():
        nonlocal available, integrity
        try:
            with path.open("rb") as handle:
                remaining = before
                while remaining is None or remaining > 0:
                    raw = handle.readline() if remaining is None else handle.readline(remaining)
                    if not raw:
                        break
                    if remaining is not None:
                        remaining -= len(raw)
                    yield raw
        except OSError:
            available = integrity = False
            warnings.append("会话日志不可读取，历史可能不完整。")

    for number, raw in enumerate(lines(), 1):
        if not raw.endswith(b"\n"):
            integrity = False
            warnings.append("日志尾部尚未完整写入，未显示该记录。")
            continue
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            integrity = False
            warnings.append("日志含损坏记录，历史可能不完整。")
            continue
        if not isinstance(record, dict):
            integrity = False
            warnings.append("日志含无效记录，历史可能不完整。")
            continue
        event = record.get("event")
        if event == START and isinstance(record.get("run"), dict):
            run = record["run"]
            if isinstance(run.get("id"), str):
                active = run["id"]
                runs[active] = dict(run)
        elif event == SETTLED and isinstance(record.get("run"), dict):
            run = record["run"]
            if isinstance(run.get("id"), str):
                runs[run["id"]] = dict(run)
                active = None
        elif event == "resumed_from":
            warnings.append("此会话包含接入的历史；来源会话的完整时间线与历轮结算未自动复制。")
        elif event in ("clear", "rewind"):
            warnings.append("此视图保留原始消息；包含清空或回退之前的记录，不等同于当前模型上下文。")
        if include_activity and event == "model" and record.get("from_model") != record.get("model"):
            pending_model = {"id": f"log:{number}", "role": "model",
                             "from_model": pending_model["from_model"] if pending_model else record.get("from_model", ""),
                             "model": record.get("model", ""), "content": ""}
        message = record.get("message") if event == "sdk.question.delivery" else record
        if (event is None or event == "sdk.question.delivery") and isinstance(message, dict):
            role = message.get("role")
            if include_activity and role == "assistant":
                for call in message.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    if not isinstance(function, dict):
                        continue
                    args = function.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {"input": args}
                    tool_calls[str(call.get("id", ""))] = {"name": function.get("name", "工具调用"), "args": args if isinstance(args, dict) else {"input": args}}
            if include_activity and role == "tool":
                content = text_of(message.get("content"))
                call = tool_calls.pop(str(message.get("tool_call_id", "")), {})
                messages.append({"id": f"log:{number}", "role": "tool", "content": content,
                                 "run_id": active, "name": call.get("name", message.get("name", "工具调用")),
                                 "args": call.get("args", {}), "ok": not content.startswith("ERROR:")})
                continue
            if role not in ("user", "assistant"):
                continue
            if role == "user" and (is_injected_message(message, SYNTHETIC_USER_TEXTS)
                                   or is_peer_message(message)):
                continue
            if include_activity and pending_model and role in ("user", "assistant"):
                if pending_model["from_model"] != pending_model["model"]:
                    messages.append(pending_model)
                pending_model = None
            content = text_of(message.get("content"))
            parts = message.get("content")
            attachments = isinstance(parts, list) and any(
                isinstance(part, dict) and part.get("type") != "text" for part in parts)
            if attachments and not content:
                content = "[附件]"
            if not content:
                continue
            item = {"id": f"log:{number}", "role": role, "content": content,
                    "created_at": record.get("ts", ""), "run_id": active,
                    "has_attachments": attachments}
            messages.append(item)
            if active is None:
                legacy = True
    if legacy:
        warnings.append("部分旧消息没有轮次结算记录；正文可读，历轮结果与成果索引可能不完整。")
    return {"messages": messages, "runs": list(runs.values()),
            "warning": " ".join(dict.fromkeys(warnings)), "available": available,
            "integrity": integrity}


def page(items: list[dict[str, Any]], cursor: int, limit: int) -> dict[str, Any]:
    end = min(cursor + limit, len(items))
    return {"items": items[cursor:end], "total": len(items),
            "next_cursor": end if end < len(items) else None}
