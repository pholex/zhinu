"""只读会话时间线：保留物理行号，不重写历史、不调用模型。"""

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import re
from typing import Any

from . import fsguard, media, ui
from .diagnostics import redact_value


def _clean(value: Any, key: str = "") -> Any:
    if isinstance(value, str):
        return ui.strip_sequences(redact_value(key, value)).encode("utf-8", "replace").decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(k): _clean(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(item, key) for item in value]
    return value


def _brief(value: Any) -> str:
    return " ".join(str(_clean(value)).split())[:240]


def inspect_session(
    path: Path, *, kinds: tuple[str, ...] = (), turn: int | None = None,
    request: int | None = None, tool_call: str | None = None,
    errors_only: bool = False, raw: bool = False, limit: int = 200,
) -> dict[str, Any]:
    """按追加顺序扫描，过滤后只保留最后 limit 行；原始数据仅在 raw 时返回。

    turn 由非注入的 user 消息划分（插话同样是边界），request 是持久化
    request 记录的全文件序号。旧日志没有请求记录时不推测其归属。
    """
    if limit < 1:
        raise ValueError("limit 必须大于 0")
    fsguard.require_regular(path)
    rows: deque[dict[str, Any]] = deque(maxlen=limit)
    warnings: list[str] = []
    matched = 0
    current_turn = 0
    current_request: int | None = None
    request_count = 0
    calls: dict[str, tuple[str, int, int | None]] = {}

    def add(line: int, record: dict, kind: str, summary: str, *, call_id: str = "",
            failed: bool = False, origin: tuple[int, int | None] | None = None) -> None:
        nonlocal matched
        row_turn, row_request = origin or (current_turn, current_request)
        if kinds and not any(kind == k or kind.startswith((k + ".", k + "_")) for k in kinds):
            return
        if turn is not None and row_turn != turn:
            return
        if request is not None and row_request != request:
            return
        if tool_call is not None and call_id != tool_call:
            return
        if errors_only and not failed:
            return
        row = {"line": line, "turn": row_turn, "request": row_request,
               "kind": kind, "tool_call_id": call_id, "error": failed,
               "time": _brief(record.get("ts", "")), "summary": _brief(summary)}
        if raw:
            row["data"] = _clean(record)
        rows.append(row)
        matched += 1

    # 固定读取边界：正在写的会话也可检查，不追随新追加内容无限等待。
    with path.open("rb") as handle:
        remaining = handle.seek(0, 2)
        handle.seek(0)
        line_no = 0
        while remaining:
            line = handle.readline(remaining)
            if not line:
                break
            remaining -= len(line)
            line_no += 1
            try:
                record = json.loads(line.decode("utf-8", errors="replace"))
                if not isinstance(record, dict):
                    raise ValueError("记录不是对象")
            except (ValueError, RecursionError):
                detail = "末尾记录尚未写完" if not remaining and not line.endswith(b"\n") else "损坏的日志记录"
                warnings.append(f"L{line_no}: {detail}")
                add(line_no, {}, "corrupt", detail, failed=True)
                continue
            role = record.get("role")
            event = record.get("event")
            if role == "user" and not media.is_injected_message(record):
                current_turn += 1
                current_request = None
            if event == "request":
                request_count += 1
                current_request = request_count
                outcome = record.get("outcome", "unknown")
                summary = f"{record.get('provider', '')}/{record.get('model', '')} {outcome}"
                for key in ("attempt", "total_ms", "first_chunk_ms", "status", "wait_s", "error_kind", "message"):
                    if key in record:
                        summary += f" {key}={record[key]}"
                add(line_no, record, "request", summary, failed=outcome in ("error", "empty"))
            elif role == "assistant":
                if text := media.text_of(record.get("content")):
                    add(line_no, record, "message.assistant", text)
                tool_calls = record.get("tool_calls") or []
                if not isinstance(tool_calls, list):
                    detail = "tool_calls 字段不是数组"
                    warnings.append(f"L{line_no}: {detail}")
                    add(line_no, record, "corrupt", detail, failed=True)
                    continue
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    function = call.get("function") or {}
                    if not isinstance(function, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    name = str(function.get("name") or "tool")
                    calls[call_id] = (name, current_turn, current_request)
                    add(line_no, record, "tool.call", name + " " + str(function.get("arguments", "")), call_id=call_id)
            elif role == "tool":
                call_id = str(record.get("tool_call_id") or "")
                name, origin_turn, origin_request = calls.pop(call_id, ("tool", current_turn, None))
                text = media.text_of(record.get("content"))
                denied = text.startswith("用户拒绝了这次工具调用") or (
                    text.startswith("ERROR:") and any(word in text for word in ("已拦截", "按拒绝处理", "hook 拦截"))
                )
                exit_match = re.match(r"exit_status:\s*(-?\d+)", text)
                failed = denied or text.startswith("ERROR") or bool(exit_match and int(exit_match[1]) != 0)
                add(line_no, record, "approval.denied" if denied else "tool.result", name + " " + text,
                    call_id=call_id, failed=failed, origin=(origin_turn, origin_request))
            elif role:
                add(line_no, record, "message." + str(role), media.text_of(record.get("content")))
            else:
                kind = str(event or "unknown")
                detail = {k: v for k, v in record.items() if k not in ("ts", "event", "replacement", "baseline")}
                add(line_no, record, kind, json.dumps(_clean(detail), ensure_ascii=False),
                    failed=kind == "error" or (kind == "compact_end" and record.get("ok") is False))
    return {"path": str(path), "turns": current_turn, "requests": request_count,
            "matched": matched, "shown": len(rows), "warnings": warnings, "rows": list(rows)}


def render_report(report: dict[str, Any], *, raw: bool = False) -> str:
    lines = [f"会话诊断：{ui.strip_sequences(report['path'])}",
             f"{report['turns']} 个用户输入段 · {report['requests']} 次请求 · 显示 {report['shown']}/{report['matched']} 条"]
    group = None
    for row in report["rows"]:
        if row["turn"] != group:
            group = row["turn"]
            lines.append(f"\n用户输入段 {group}" if group else "\n会话前言")
        request = f"R{row['request']}" if row["request"] is not None else "R?"
        call = f" [{row['tool_call_id']}]" if row["tool_call_id"] else ""
        marker = "!" if row["error"] else " "
        lines.append(ui.strip_sequences(f"{marker} L{row['line']} {request} {row['kind']}{call}  {row['summary']}"))
        if raw:
            lines.append(json.dumps(row["data"], ensure_ascii=True, indent=2))
    lines.extend(report["warnings"])
    return "\n".join(lines) + "\n"
