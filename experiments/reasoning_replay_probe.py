#!/usr/bin/env python3
"""推理载荷的成对探测：默认仅打印计划，--live 才发请求；不执行任何模型工具。"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.request


def chat_pair(model, position, nonce, limit):
    trace = ("The calculation has been checked; preserve the task context. " * 40
             + f"The verification code is {nonce}.")
    messages = [
        {"role": "system", "content": "Answer briefly using the task context."},
        {"role": "user", "content": "Read the task record."},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "read1", "type": "function",
            "function": {"name": "read_record", "arguments": "{}"}}], "reasoning_content": trace},
        {"role": "tool", "tool_call_id": "read1", "content": "Record read. Return the verification code, or UNKNOWN if unavailable."},
    ]
    if position == "cross_turn":
        messages[-1]["content"] = "Record read."
        messages += [{"role": "assistant", "content": "The record is read."},
                     {"role": "user", "content": "Return the verification code, or UNKNOWN if unavailable."}]
    replay = {"model": model, "messages": messages, "max_completion_tokens": limit}
    baseline = copy.deepcopy(replay)
    del baseline["messages"][2]["reasoning_content"]
    return baseline, replay


def responses_seed(model, limit):
    return {"model": model, "store": False, "include": ["reasoning.encrypted_content"],
            "max_output_tokens": limit, "input": [{"role": "user", "content": "Calculate 37 times 43 and report the result with report_value."}],
            "tools": [{"type": "function", "name": "report_value", "description": "Record the calculated value.",
                       "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}}],
            "tool_choice": {"type": "function", "name": "report_value"}}


def responses_pair(seed_request, seed_response, position):
    output = seed_response.get("output") or []
    carriers = [item for item in output if item.get("type") == "reasoning" and item.get("encrypted_content")]
    calls = [item for item in output if item.get("type") == "function_call"]
    if not carriers or not calls:
        raise ValueError("seed_without_replayable_reasoning_or_tool_call")
    history = copy.deepcopy(seed_request["input"])
    #  使用真实返回的加密块；不改签名、不把它换成自造文本。
    history.extend(carriers)
    history.extend({"type": "function_call", "call_id": c["call_id"], "name": c["name"],
                    "arguments": c["arguments"]} for c in calls)
    history.extend({"type": "function_call_output", "call_id": c["call_id"],
                    "output": "Recorded. Return the calculated numeric value only."} for c in calls)
    if position == "cross_turn":
        history += [{"role": "assistant", "content": "Recorded."},
                    {"role": "user", "content": "Return the calculated numeric value only."}]
    replay = {"model": seed_request["model"], "store": False, "input": history,
              "max_output_tokens": seed_request["max_output_tokens"]}
    baseline = copy.deepcopy(replay)
    baseline["input"] = [item for item in baseline["input"] if item.get("type") != "reasoning"]
    return baseline, replay


def observation(response, protocol, expected):
    if not isinstance(response, dict) or response.get("error"):
        raise ValueError("invalid_response")
    usage = response.get("usage") or {}
    tokens = usage.get("prompt_tokens" if protocol == "chat" else "input_tokens")
    if protocol == "chat":
        if not response.get("choices"):
            raise ValueError("missing_choices")
        if any(c.get("finish_reason") in ("length", "content_filter") for c in response["choices"]):
            return {"status": "incomplete", "input_tokens": tokens, "expected_recalled": False}
        text = " ".join(str((c.get("message") or {}).get("content") or "") for c in response.get("choices", []))
    else:
        if not response.get("output") or response.get("status") in ("failed", "incomplete"):
            return {"status": "incomplete", "input_tokens": tokens, "expected_recalled": False}
        text = " ".join(str(part.get("text") or "") for item in response.get("output", [])
                        for part in item.get("content", []) if part.get("type") == "output_text")
    return {"status": "accepted", "input_tokens": tokens if type(tokens) is int else None,
            "output_tokens": usage.get("completion_tokens" if protocol == "chat" else "output_tokens"),
            "expected_recalled": expected in text}


def pair_report(rows):
    baseline, replay = rows["baseline"], rows["replay"]
    accepted = all(r["status"] == "accepted" for r in (baseline, replay))
    before, after = baseline.get("input_tokens"), replay.get("input_tokens")
    return {"paired": accepted,
            "input_token_delta": after - before if accepted and type(before) is int and type(after) is int else None,
            "recall_improved": accepted and replay.get("expected_recalled") is True and baseline.get("expected_recalled") is False,
            "observations": rows}


def request(base_url, protocol, key, payload):
    path = "/chat/completions" if protocol == "chat" else "/responses"
    req = urllib.request.Request(base_url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    #  默认不跟随重定向，避免跨端点传递凭据。
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", choices=("chat", "responses"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", help="API 前缀，包含 /v1（若端点需要）")
    parser.add_argument("--key-env", default="XIAOYU_API_KEY")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--max-output-tokens", type=int, default=2048)
    parser.add_argument("--max-requests", type=int, default=12)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    count = args.repeat * 2 * (3 if args.protocol == "responses" else 2)
    if not 1 <= args.repeat <= 10 or not 64 <= args.max_output_tokens <= 8192 or count > args.max_requests:
        parser.error("重复次数应为 1–10，输出上限 64–8192，计划请求数不能超过 --max-requests")
    report = {"schema": 1, "protocol": args.protocol, "model": args.model,
              "request_cap": count, "positions": ["in_loop", "cross_turn"], "results": []}
    if not args.live:
        print(json.dumps({**report, "mode": "plan_only"}, ensure_ascii=False, indent=2))
        return 0
    key = os.environ.get(args.key_env)
    if not args.base_url or not key or args.output is None:
        parser.error("--live 需要 --base-url、--output 和 --key-env 指定的环境变量")
    #  保存可中断的逐对结果；不存 key、原始推理块、响应正文或错误体。
    with args.output.open("x", encoding="utf-8") as destination:
        def save():
            destination.seek(0)
            json.dump(report, destination, ensure_ascii=False, indent=2)
            destination.write("\n")
            destination.truncate()
            destination.flush()
        save()
        for repeat in range(args.repeat):
            for position in report["positions"]:
                expected = secrets.token_hex(12)
                seed_usage = None
                try:
                    if args.protocol == "chat":
                        baseline, replay = chat_pair(args.model, position, expected, args.max_output_tokens)
                    else:
                        seed = responses_seed(args.model, args.max_output_tokens)
                        raw = request(args.base_url, args.protocol, key, seed)
                        usage = raw.get("usage") or {}
                        seed_usage = {k: usage.get(k) for k in ("input_tokens", "output_tokens")}
                        baseline, replay = responses_pair(seed, raw, position)
                        expected = "1591"
                except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                    report["results"].append({"repeat": repeat, "position": position,
                        "paired": False, "status": "seed_unavailable", "seed_usage": seed_usage,
                        "error_type": type(exc).__name__, "http_status": getattr(exc, "code", None)})
                    save()
                    continue
                rows = {}
                variants = [("baseline", baseline), ("replay", replay)]
                #  交替顺序减少固定的先后顺序对缓存与负载的影响。
                for name, payload in variants[::1 if repeat % 2 == 0 else -1]:
                    started = time.monotonic()
                    try:
                        rows[name] = observation(request(args.base_url, args.protocol, key, payload), args.protocol, expected)
                    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as exc:
                        rows[name] = {"status": "error", "error_type": type(exc).__name__,
                                      "http_status": getattr(exc, "code", None)}
                    rows[name]["duration_ms"] = round((time.monotonic() - started) * 1000)
                report["results"].append({"repeat": repeat, "position": position,
                                           "seed_usage": seed_usage, **pair_report(rows)})
                save()
    print(json.dumps({"output": str(args.output), "pairs": len(report["results"]),
                      "valid_pairs": sum(r["paired"] for r in report["results"])}, ensure_ascii=False))
    return 0 if all(r["paired"] for r in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
