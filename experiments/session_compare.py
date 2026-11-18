#!/usr/bin/env python3
"""同题对照：把小羽与小雪（对照侧）各自的一场会话归一成同一张时间线表。

    .venv/bin/python experiments/session_compare.py --yu <会话 id 或文件> --xue <会话文件>

只读、不调模型。两边的日志格式不同，各有一个适配器，产出同一种步骤记录：
用户输入、正文、工具调用（带成败与耗时）。汇总表逐项并排，不打总分。

小雪一侧只认它会话 jsonl 的结构（主链上的 assistant / user 记录、tool_use /
tool_result 块、usage 字段），路径由参数给；也可以给 id 加 `--xue-root <目录>`，
在目录下按文件名找。

几个口径：

- 时间一律是距**首条用户输入**的秒数，两边时钟与时区不必对齐。
- 总耗时到最后一步结束为止，不含用户停在提示符上的时间。
- 前台等待秒数用内核空等检测的同一个算法从命令里数 sleep（循环体里的只算一次，
  是下限），每步再封顶到该步实际耗时——被拦下、被打断的 sleep 不会虚增。
- 每请求上下文：小羽取 request 记录的 prompt_tokens；小雪取 usage 里三项输入之和
  （新输入 + 读缓存 + 写缓存）。都是"这次请求模型看到多少"。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from xiaoyu import ui  # noqa: E402
from xiaoyu.agent import Agent  # noqa: E402
from xiaoyu.diagnostics import redact_value  # noqa: E402
from xiaoyu.session_inspect import inspect_session  # noqa: E402

INTERRUPTED = "用户按 Ctrl-C 中断了本轮"
#  小雪日志里由客户端代写、不是用户敲的 user 文本
_XUE_NOT_TYPED = ("<local-command", "<command-", "<system-reminder>", "<task-notification>",
                  "<bash-input>", "<bash-stdout>", "<bash-stderr>", "[Request interrupted")
STATUS_MARK = {"ok": "✓", "fail": "✗", "denied": "拒", "interrupted": "断", "pending": "…"}


@dataclass
class Step:
    at: float                    # 距首条用户输入的秒数
    kind: str                    # user / say / tool
    request: int | None = None   # 第几次模型请求（从 1 起）
    tool: str = ""
    detail: str = ""             # 用户输入、正文或工具参数的摘要
    status: str = ""             # 仅工具：ok / fail / denied / interrupted / pending
    took: float | None = None    # 仅工具：调用到结果的秒数
    asked_wait: float = 0.0      # 仅工具：命令里申请的 sleep 秒数


@dataclass
class Request:
    n: int
    context: int | None = None   # 这次请求的输入 token
    output: int | None = None
    said: bool = False           # 这次请求有没有正文
    failed: bool = False


@dataclass
class Timeline:
    label: str
    name: str                    # 会话文件名（不带目录：路径里有用户名）
    model: str = ""
    steps: list[Step] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _clean(text: Any) -> str:
    return redact_value("", ui.strip_sequences(str(text)))


def _parse_ts(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp.replace(tzinfo=None)  # 只在同一侧内部相减，时区无关


def _args_detail(args: Any) -> tuple[str, float]:
    """工具参数 → (摘要, 申请的 sleep 秒数)。有 command 的只显示命令本身。"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return _clean(args), 0.0
    if isinstance(args, dict) and isinstance(args.get("command"), str):
        command = args["command"]
        return _clean(command), Agent._slept_seconds(command)
    return _clean(json.dumps(args, ensure_ascii=False)), 0.0


class _Clock:
    """首条用户输入之前的记录记为 0 秒。"""

    def __init__(self) -> None:
        self.origin: datetime | None = None
        self.last = 0.0

    def start(self, stamp: datetime | None) -> None:
        if self.origin is None and stamp is not None:
            self.origin = stamp

    def at(self, stamp: datetime | None) -> float:
        if self.origin is not None and stamp is not None:
            self.last = max(0.0, (stamp - self.origin).total_seconds())
        return self.last


def _close(step: Step, at: float, status: str) -> None:
    step.took = max(0.0, at - step.at)
    step.status = status


def load_yu(path: Path) -> Timeline:
    """小羽会话 → 时间线。归属（第几段输入、第几次请求）沿用 sessions inspect 的。"""
    report = inspect_session(path, raw=True, limit=10_000_000)
    out = Timeline(label="小羽", name=path.name, warnings=list(report["warnings"]))
    clock = _Clock()
    seen_turn = 0
    open_calls: dict[str, Step] = {}
    by_request: dict[int, Request] = {}
    for row in report["rows"]:
        data = row.get("data") or {}
        stamp = _parse_ts(data.get("ts"))
        kind = row["kind"]
        if kind == "meta":
            out.model = str(data.get("model") or "")
        elif kind == "message.user":
            if row["turn"] > seen_turn:  # 注入的 user 消息不开新段
                seen_turn = row["turn"]
                clock.start(stamp)
                out.steps.append(Step(clock.at(stamp), "user", detail=row["summary"]))
        elif kind == "request":
            request = Request(row["request"], data.get("prompt_tokens"), data.get("completion_tokens"),
                              failed=bool(row["error"]))
            by_request[request.n] = request
            out.requests.append(request)
            if data.get("model"):
                out.model = str(data["model"])
        elif kind == "message.assistant":
            out.steps.append(Step(clock.at(stamp), "say", row["request"], detail=row["summary"]))
            if row["request"] in by_request:
                by_request[row["request"]].said = True
        elif kind == "tool.call":
            call_id = row["tool_call_id"]
            call = next((c for c in data.get("tool_calls") or []
                         if isinstance(c, dict) and str(c.get("id") or "") == call_id), {})
            function = call.get("function") if isinstance(call.get("function"), dict) else {}
            detail, asked = _args_detail(function.get("arguments", ""))
            step = Step(clock.at(stamp), "tool", row["request"], str(function.get("name") or "tool"),
                        detail, "pending", asked_wait=asked)
            open_calls[call_id] = step
            out.steps.append(step)
        elif kind in ("tool.result", "approval.denied"):
            step = open_calls.pop(row["tool_call_id"], None)
            if step is None:
                continue
            body = row["summary"].partition(" ")[2]
            status = ("denied" if kind == "approval.denied" else
                      "interrupted" if body.startswith(INTERRUPTED) else
                      "fail" if row["error"] else "ok")
            _close(step, clock.at(stamp), status)
    return out


def _xue_client_written(record: dict[str, Any]) -> bool:
    """客户端代写的 user 记录：注入的说明、压缩摘要、后台任务的完成通知。"""
    return bool(record.get("isMeta") or record.get("isCompactSummary")
                or record.get("promptSource") == "system")


def load_xue(path: Path) -> Timeline:
    """小雪会话 jsonl → 时间线。只看主链；一次请求的多个内容块按 message.id 归并。"""
    out = Timeline(label="小雪", name=path.name)
    clock = _Clock()
    open_calls: dict[str, Step] = {}
    by_message: dict[str, Request] = {}
    with path.open("rb") as handle:
        for line_no, raw in enumerate(handle, 1):
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except (ValueError, RecursionError):
                out.warnings.append(f"L{line_no}: 损坏的日志记录")
                continue
            if not isinstance(record, dict) or record.get("isSidechain"):
                continue
            message = record.get("message")
            if record.get("type") not in ("user", "assistant") or not isinstance(message, dict):
                continue
            stamp = _parse_ts(record.get("timestamp"))
            content = message.get("content")
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
            blocks = [b for b in blocks if isinstance(b, dict)]
            if record["type"] == "user":
                for block in blocks:
                    if block.get("type") == "tool_result":
                        step = open_calls.pop(str(block.get("tool_use_id") or ""), None)
                        if step is not None:
                            _close(step, clock.at(stamp), "fail" if block.get("is_error") else "ok")
                    elif block.get("type") == "text" and not _xue_client_written(record):
                        text = str(block.get("text") or "").strip()
                        if text and not text.startswith(_XUE_NOT_TYPED):
                            clock.start(stamp)
                            out.steps.append(Step(clock.at(stamp), "user", detail=_brief(text)))
                continue
            message_id = str(message.get("id") or f"L{line_no}")
            request = by_message.get(message_id)
            if request is None:
                request = by_message[message_id] = Request(len(out.requests) + 1)
                out.requests.append(request)
            if message.get("model"):
                out.model = str(message["model"])
            usage = message.get("usage")
            if isinstance(usage, dict):
                request.context = sum(int(usage.get(key) or 0) for key in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
                request.output = int(usage.get("output_tokens") or 0)
            for block in blocks:
                if block.get("type") == "text" and str(block.get("text") or "").strip():
                    request.said = True
                    out.steps.append(Step(clock.at(stamp), "say", request.n, detail=_brief(block["text"])))
                elif block.get("type") == "tool_use":
                    detail, asked = _args_detail(block.get("input"))
                    step = Step(clock.at(stamp), "tool", request.n, str(block.get("name") or "tool"),
                                _brief(detail), "pending", asked_wait=asked)
                    open_calls[str(block.get("id") or "")] = step
                    out.steps.append(step)
    return out


def _brief(text: Any) -> str:
    return " ".join(_clean(text).split())[:240]


def _covered(spans: list[tuple[float, float]]) -> float:
    """区间并集的总长：同一批并行调用的结果一起落盘，逐步相加会重复计时。"""
    total, reach = 0.0, 0.0
    for start, end in sorted(spans):
        total += max(0.0, end - max(start, reach))
        reach = max(reach, end)
    return total


def summarize(timeline: Timeline) -> dict[str, Any]:
    tools = [s for s in timeline.steps if s.kind == "tool"]
    spoken = [s for s in timeline.steps if s.kind != "user"]
    contexts = [r.context for r in timeline.requests if r.context]
    ends = [s.at + (s.took or 0.0) for s in timeline.steps]
    return {
        "tool_steps": len(tools),
        "bad_steps": sum(s.status in ("fail", "denied") for s in tools),
        "user_inputs": sum(s.kind == "user" for s in timeline.steps),
        "seconds": round(max(ends, default=0.0)),
        "requests": len(timeline.requests),
        "context_median": round(statistics.median(contexts)) if contexts else None,
        "context_max": max(contexts, default=None),
        "context_total": sum(contexts) if contexts else None,
        "output_total": sum(r.output or 0 for r in timeline.requests) if timeline.requests else None,
        "tool_seconds": round(_covered([(s.at, s.at + s.took) for s in tools if s.took is not None])),
        "wait_asked": round(sum(s.asked_wait for s in tools)),
        "wait_seconds": round(sum(min(s.asked_wait, s.took) for s in tools if s.took is not None)),
        "said_requests": sum(r.said for r in timeline.requests),
        "ends_with_text": bool(spoken) and spoken[-1].kind == "say",
    }


def _cell(text: str, width: int) -> str:
    text = " ".join(text.split()).replace("|", "\\|")
    return text if len(text) <= width else text[: width - 1] + "…"


def _num(value: Any) -> str:
    return "—" if value is None else f"{value:,}" if isinstance(value, int) else str(value)


def render_timeline(timeline: Timeline, width: int = 100) -> str:
    said = {r.n for r in timeline.requests if r.said}
    lines = [f"### {timeline.label} · {timeline.model or '模型未知'} · {timeline.name}", "",
             "| +秒 | 请求 | 步骤 | 结果 | 耗时 | 正文 |", "|---:|---:|---|:---:|---:|:---:|"]
    for step in timeline.steps:
        request = f"R{step.request}" if step.request else ""
        if step.kind == "user":
            what, result, took, text = "**用户**：" + _cell(step.detail, width), "", "", ""
        elif step.kind == "say":
            what, result, took, text = "正文：" + _cell(step.detail, width), "", "", "有"
        else:
            wait = f"（sleep {step.asked_wait:g}s）" if step.asked_wait else ""
            what = f"`{step.tool}` {_cell(step.detail, width)}{wait}"
            result = STATUS_MARK.get(step.status, step.status)
            took = "" if step.took is None else f"{step.took:.0f}s"
            text = "有" if step.request in said else "—"
        lines.append(f"| {step.at:.0f} | {request} | {what} | {result} | {took} | {text} |")
    lines.extend(f"- 警告：{w}" for w in timeline.warnings)
    return "\n".join(lines) + "\n"


SUMMARY_ROWS = (
    ("工具步数", lambda s: _num(s["tool_steps"])),
    ("其中失败 / 被拒", lambda s: _num(s["bad_steps"])),
    ("用户输入次数", lambda s: _num(s["user_inputs"])),
    ("总耗时（秒）", lambda s: _num(s["seconds"])),
    ("请求数", lambda s: _num(s["requests"])),
    ("每请求上下文（中位 / 最大）", lambda s: f"{_num(s['context_median'])} / {_num(s['context_max'])}"),
    ("输入 token 合计", lambda s: _num(s["context_total"])),
    ("输出 token 合计", lambda s: _num(s["output_total"])),
    ("工具执行秒数", lambda s: _num(s["tool_seconds"])),
    ("前台等待秒数（计入 / 申请）", lambda s: f"{_num(s['wait_seconds'])} / {_num(s['wait_asked'])}"),
    ("带正文的请求", lambda s: f"{s['said_requests']}/{s['requests']}"),
    ("以正文收尾", lambda s: "是" if s["ends_with_text"] else "否"),
)


def render_summary(timelines: list[Timeline]) -> str:
    summaries = [summarize(t) for t in timelines]
    lines = ["| 项 | " + " | ".join(t.label for t in timelines) + " |",
             "|---|" + "---:|" * len(timelines)]
    for title, cell in SUMMARY_ROWS:
        lines.append(f"| {title} | " + " | ".join(cell(s) for s in summaries) + " |")
    return "\n".join(lines) + "\n"


def render(timelines: list[Timeline], width: int = 100) -> str:
    parts = ["## 汇总", "", render_summary(timelines), "## 时间线", ""]
    parts.extend(render_timeline(t, width) for t in timelines)
    parts.append("结果列：✓ 成功 · ✗ 失败 · 拒 被拒绝 · 断 被用户打断 · … 没等到结果。"
                 "正文列是这一步所在的请求有没有带正文。")
    return "\n".join(parts) + "\n"


def resolve_yu(ref: str) -> Path:
    path = Path(ref).expanduser()
    if path.is_file():
        return path
    from xiaoyu.session_log import find_session

    info = find_session(ref)
    if info is None:
        raise SystemExit(f"找不到小羽会话：{ref}")
    return info.path


def resolve_xue(ref: str, root: str | None) -> Path:
    path = Path(ref).expanduser()
    if path.is_file():
        return path
    if root:
        found = sorted(Path(root).expanduser().rglob(f"{ref}.jsonl"))
        if found:
            return found[0]
    raise SystemExit(f"找不到小雪会话：{ref}（给文件路径，或 id 加 --xue-root <目录>）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--yu", help="小羽会话：id 或 jsonl 路径")
    parser.add_argument("--xue", help="小雪会话：jsonl 路径，或 id（配 --xue-root）")
    parser.add_argument("--xue-root", help="小雪会话目录，按 <id>.jsonl 递归查找")
    parser.add_argument("--width", type=int, default=100, help="步骤摘要的最大字符数")
    parser.add_argument("--json", action="store_true", help="输出归一后的结构化数据")
    parser.add_argument("--out", help="写到文件（默认打到标准输出）")
    args = parser.parse_args(argv)
    if not args.yu and not args.xue:
        parser.error("--yu 与 --xue 至少给一个")
    timelines = []
    if args.yu:
        timelines.append(load_yu(resolve_yu(args.yu)))
    if args.xue:
        timelines.append(load_xue(resolve_xue(args.xue, args.xue_root)))
    if args.json:
        text = json.dumps([{**asdict(t), "summary": summarize(t)} for t in timelines],
                          ensure_ascii=False, indent=2) + "\n"
    else:
        text = render(timelines, max(20, args.width))
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
