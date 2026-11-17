#!/usr/bin/env python3
"""Frozen-input SDK reliability batches. Real providers are a separate batch.

Usage: python scripts/sdk_reliability.py --mode contracts|lifecycle|soak|backpressure --output DIR
No network/model credentials are read. Reports retain failed trials and missing evidence.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import contextlib
import csv
from dataclasses import asdict
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from types import SimpleNamespace
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "packages/xiaoyu-agent-sdk/src")]
from xiaoyu_agent_sdk import Hook, HookDecision, ModelOptions, Session, SessionOptions, SQLiteSessionStore

GROUPS = {
    "R03": ["tests.test_sdk.AsyncSDKTests.test_stream_early_close_releases_backpressure_and_can_continue"],
    "R04": ["tests.test_sdk.AsyncSDKTests.test_cancel_pending_approval_then_continue", "tests.test_sdk.AsyncSDKTests.test_interrupt_during_repair_is_a_result_then_session_can_continue", "tests.test_sdk_fault_windows.CancellationWindows"],
    "R05": ["tests.test_sdk_storage.StorageTests.test_process_exit_releases_ownership_and_unknown_tool_is_not_replayed", "tests.test_sdk_tasks.TaskTests.test_real_host_exit_retains_success_and_marks_inflight_lost", "tests.test_sdk_fault_windows.HostExitWindows"],
    "R06": ["tests.test_sdk.SDKTests.test_model_error_preserves_cause_and_session_can_continue", "tests.test_sdk_transport_reliability"],
    "R07": ["tests.test_mcp_failure_paths", "tests.test_mcp.StubbornServerShutdownTest", "tests.test_mcp.ShutdownOwnershipTest", "tests.test_mcp.EndToEndTest.test_bad_command_fails_gracefully"],
    "R08": ["tests.test_sdk.SDKTests.test_slow_callback_close_reports_timeout_then_finishes", "tests.test_sdk.AsyncSDKTests.test_approval_timeout_fails_closed", "tests.test_sdk.SDKTests.test_tool_exception_is_redacted_and_model_can_recover", "tests.test_sdk.SDKTests.test_callback_timeout_exception_is_not_mistaken_for_wait_timeout", "tests.test_sdk_accounting.AccountingTests.test_failure_notification_exceptions_do_not_hide_tool_result", "tests.test_sdk_accounting.AccountingTests.test_compaction_pre_hook_failure_preserves_history_and_skips_model"],
    "R09": ["tests.test_sdk_storage.StorageTests.test_exclusive_ownership_across_processes", "tests.test_sdk_storage.StorageTests.test_close_failure_retains_lock_until_successful_retry", "tests.test_sdk_fault_windows.StorageErrorWindows"],
    "R10": ["tests.test_sdk.SDKTests.test_validation_repair_counts_usage_without_replaying_side_effect", "tests.test_sdk.SDKTests.test_token_budget_stops_repair_without_an_extra_request", "tests.test_sdk.SDKTests.test_null_is_valid_and_next_turn_has_no_output_tool", "tests.test_sdk.SDKTests.test_output_failure_statuses", "tests.test_sdk.SDKTests.test_malformed_structured_call_uses_repair_budget", "tests.test_sdk.SDKTests.test_budget_during_repair_never_wraps_up_or_reports_success", "tests.test_sdk.AsyncSDKTests.test_interrupt_during_repair_is_a_result_then_session_can_continue", "tests.test_sdk_accounting.AccountingTests.test_summary_and_output_repair_share_request_gate_and_accounting"],
    "R11": ["tests.test_sdk.SDKTests.test_rewind_conflict_preserves_external_edit_and_conversation", "tests.test_sdk.SDKTests.test_rewind_reports_partial_and_unavailable", "tests.test_sdk_tasks.TaskTests.test_fork_copies_child_history_without_copying_task_execution", "tests.test_rewind"],
    "R12": ["tests.test_sdk.SDKTests.test_blocked_client_cleanup_has_one_worker_and_is_retryable", "tests.test_sdk_tasks.TaskTests.test_close_timeout_retains_parent_lock", "tests.test_sdk.SDKTests.test_slow_callback_close_reports_timeout_then_finishes", "tests.test_sdk.SDKTests.test_mcp_pending_shutdown_keeps_log_locked_until_retry", "tests.test_background.ShutdownOwnershipTest"],
    "R14": ["tests.test_sdk_storage"],
    "R15": ["tests.test_sdk_tasks"],
    "R16": ["tests.test_sdk_accounting.AccountingTests.test_dollar_gate_and_cached_usage", "tests.test_sdk_accounting.AccountingTests.test_unknown_price_and_missing_usage_are_not_zero", "tests.test_sdk_accounting.AccountingTests.test_concurrent_children_do_not_exceed_request_limit", "tests.test_sdk_accounting.AccountingTests.test_request_gate_is_shared_with_children_and_persisted", "tests.test_sdk_accounting.AccountingTests.test_fork_includes_cost_unless_host_explicitly_resets", "tests.test_sdk_accounting.AccountingTests.test_summary_and_output_repair_share_request_gate_and_accounting"],
    "R17": ["tests.test_sdk_accounting.AccountingTests.test_real_opentelemetry_parent_spans_and_borrowed_provider", "tests.test_sdk_accounting.AccountingTests.test_slow_exporter_backpressure_and_close_retry", "tests.test_sdk_accounting.AccountingTests.test_exporter_failure_never_breaks_execution", "tests.test_sdk_accounting.AccountingTests.test_trace_is_content_free_and_spans_close_on_tool_failure", "tests.test_sdk_accounting.AccountingTests.test_stream_cost_and_trace_correlate_requests_and_tool_calls"],
    "R18": ["tests.test_sdk_mcp_management", "tests.test_sdk_accounting.AccountingTests.test_extended_hooks_have_order_identity_and_fail_closed_start"],
}
GROUPS["R11"].append("tests.test_sandbox.WorkspaceTrustCacheTest")
GROUPS["R16"].extend([
    "tests.test_sdk_accounting.AccountingTests.test_protocol_missing_usage_stays_unknown_after_normalization",
    "tests.test_sdk_accounting.AccountingTests.test_cache_creation_requires_explicit_price_and_counts_separately",
])


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8")


def manifest(config, output):
    paths = ("xiaoyu", "packages/xiaoyu-agent-sdk", "tests", "scripts", "examples/sdk", "pyproject.toml", "setup.py",
             "MANIFEST.in", "README.md", "LICENSE", "docs/embedding.md", "docs/sdk*.md",
             ".github/workflows/sdk.yml", ".github/workflows/sdk-reliability.yml")
    names = set(git("ls-files", "--", *paths).splitlines()) | set(git("ls-files", "--others", "--exclude-standard", "--", *paths).splitlines())
    hashes = {}
    with zipfile.ZipFile(output / "source.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(names):
            path = ROOT / name
            if path.is_file():
                data = path.read_bytes()
                hashes[name] = digest(data)
                archive.writestr(name, data)
    (output / "source.patch").write_text(git("diff", "--binary", "HEAD", "--", *paths), encoding="utf-8")
    return {"started_at": utc(), "git_head": git("rev-parse", "HEAD").strip(), "dirty": bool(git("status", "--porcelain")),
            "python": sys.version, "os": platform.platform(), "config": config,
            "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions() if d.metadata["Name"]},
            "config_sha256": digest(json.dumps(config, sort_keys=True).encode()), "source_hashes": hashes,
            "source_archive_sha256": digest((output / "source.zip").read_bytes()), "model": "deterministic-script"}


class Client:
    """Does not retain requests; keeps workload history owned by the SDK only."""
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.summaries = 0

    def create(self, **kwargs):
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=2)
        if not kwargs.get("stream"):
            self.summaries += 1
            text = "FACT-42 remains the business identifier. " + "The prior completed turns verified the identifier and had no external side effects. " * 5
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text), finish_reason="stop")], usage=usage)
        delta = SimpleNamespace(content="42", tool_calls=None)
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason="stop")], usage=usage)])


def options(workspace, client=None, store=None, hooks=()):
    return SessionOptions(ModelOptions("reliability-script", client=client or Client()), workspace,
                          builtin_tools=(), session_store=store, hooks=hooks)


def backpressure(config, output):
    from dataclasses import replace
    records = []
    class Fragments:
        def __init__(self):
            self.chat = SimpleNamespace(completions=self)
            self.fragmented = True

        def create(self, **kwargs):
            count = config["stream_deltas"] if self.fragmented else 1
            text = "x" if self.fragmented else "42"
            for index in range(count):
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=text, tool_calls=None),
                    finish_reason="stop" if index == count - 1 else None)], usage=None)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        for capacity in config["stream_capacities"]:
            client = Fragments()
            opts = replace(options(root, client), event_buffer_size=capacity)
            started = time.monotonic()
            maximum = 0
            with Session(opts) as session:
                pieces, completed = [], []
                for event in session.stream("stream all fragments"):
                    maximum = max(maximum, session._events.qsize())
                    if event.kind == "text.delta":
                        pieces.append(event.text)
                    if event.kind == "run.completed":
                        completed.append(event.result)
                    time.sleep(config["stream_consumer_delay"])
                assert "".join(pieces) == "x" * config["stream_deltas"]
                assert len(completed) == 1 and completed[0].text == "".join(pieces)
                assert maximum <= capacity
                for repeat in range(config["stream_close_repeats"]):
                    client.fragmented = True
                    stream = session.stream("close early")
                    for index, event in enumerate(stream):
                        maximum = max(maximum, session._events.qsize())
                        time.sleep(config["stream_consumer_delay"])
                        if index == 9:
                            break
                    stream.close()
                    client.fragmented = False
                    assert session.run("continue").text == "42"
            checks = {"queue_bound": maximum <= capacity, "no_sdk_threads": not sdk_threads()}
            records.append({"capacity": capacity, "deltas": config["stream_deltas"],
                "early_closes": config["stream_close_repeats"], "max_queue": maximum,
                "seconds": time.monotonic() - started, "checks": checks})
            (output / "trials.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
    return {"scenario": "R03", "status": "passed" if all(all(t["checks"].values()) for t in records) else "failed",
            "stream_trials": records, "scope": "Local full-consumption and 100 early-close cases per queue capacity"}


def lifecycle(config, output):
    counts = []
    resource_checks = []
    for persist in (False, True):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            store = SQLiteSessionStore(root / "sessions.sqlite") if persist else None
            def run(index):
                start = time.monotonic()
                with Session(options(root, store=store)) as session:
                    for turn in range(config["turns_per_session"]):
                        result = session.run(f"session {index} turn {turn}")
                        assert result.text == "42" and result.stopped == "done"
                return time.monotonic() - start
            for trial in range(config["baseline_trials"]):
                durations = [run(i) for i in range(config["baseline_sessions"])]
                counts.append({"phase": "serial_baseline", "trial": trial, "persisted": persist, "durations": durations})
            gc.collect()
            baseline = metrics()
            with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
                durations = list(pool.map(run, range(config["lifecycle_sessions"])))
            # Stopped worker Thread objects retain Windows handles while the
            # harness executor remains referenced. Exclude the harness itself.
            del pool
            counts.append({"phase": "parallel", "persisted": persist, "durations": durations})
            gc.collect()
            final = metrics()
            checks = {"fd_growth": final["fds"] - baseline["fds"] <= config["thresholds"]["max_fd_growth"],
                      "no_children": final["children"] == 0,
                      "no_sdk_threads": not sdk_threads()}
            resource_checks.append({"persisted": persist, "baseline": baseline, "final": final, "checks": checks})
    return {"scenario": "R02", "status": "passed" if all(all(r["checks"].values()) for r in resource_checks) else "failed",
            "trials": counts, "resource_checks": resource_checks,
            "scope": "Lifecycle count, result correctness, cleanup ownership; sustained RSS gates are tested separately by soak"}


def sdk_threads():
    return [t.name for t in threading.enumerate()
            if t.name.startswith(("xiaoyu-session", "xiaoyu-host", "xiaoyu-child", "xiaoyu-telemetry"))]


def metrics():
    import psutil
    process = psutil.Process()
    return {"at": time.monotonic(), "rss": process.memory_info().rss, "python_bytes": tracemalloc.get_traced_memory()[0],
            "threads": process.num_threads(), "fds": process.num_handles() if os.name == "nt" else process.num_fds(),
            "children": len(process.children(recursive=True))}


def soak(config, output):
    tracemalloc.start()
    rows = []
    started = time.monotonic()
    summaries = 0
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        client = Client()
        changed = []
        def compacted(event):
            if event.get("changed"):
                changed.append(1)
            return HookDecision(False)
        with Session(options(root, client, hooks=(Hook("AfterCompact", compacted),))) as session:
            # A bounded historical payload makes real compaction worthwhile.
            prompt = "Preserve FACT-42. " + "deterministic background material " * 30
            turns = 0
            last_sample = 0.0
            while turns < config["soak_min_turns"] or time.monotonic() - started < config["soak_seconds"]:
                result = session.run(prompt if turns == 0 else "Continue FACT-42. " + prompt)
                assert result.text == "42" and result.stopped == "done"
                turns += 1
                if turns % config["compact_every"] == 0:
                    session._agent.maybe_compact(force=True)
                    assert any("FACT-42" in str(m.get("content", "")) for m in session._agent.messages)
                now = time.monotonic()
                if now - last_sample >= config["sample_seconds"]:
                    gc.collect()
                    rows.append(metrics())
                    with (output / "progress.json").open("w", encoding="utf-8") as stream:
                        json.dump({"turns": turns, "elapsed": now - started, "compactions": len(changed)}, stream)
                    with (output / "resources.csv").open("w", newline="", encoding="utf-8") as stream:
                        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                        writer.writeheader()
                        writer.writerows(rows)
                    last_sample = now
                target = started + turns * config["soak_seconds"] / config["soak_min_turns"]
                time.sleep(max(0, min(target - time.monotonic(), .5)))
            assert changed and client.summaries, "No actual compaction was observed"
            summaries = client.summaries
    gc.collect()
    final = metrics()
    tracemalloc.stop()
    warm = [r for r in rows if config["warmup_seconds"] <= r["at"] - started < config["warmup_seconds"] + 600]
    tail = [r for r in rows if r["at"] - started >= config["soak_seconds"] - 600]
    trend = [r for r in rows if r["at"] - started >= config["soak_seconds"] - 1800]
    assert len(warm) >= 2 and len(tail) >= 2, "Insufficient resource samples"
    rss_base = statistics.median(r["rss"] for r in warm)
    rss_growth = statistics.median(r["rss"] for r in tail) - rss_base
    x = [(r["at"] - trend[0]["at"]) / 60 for r in trend]
    y = [r["rss"] for r in trend]
    slope = sum((a - statistics.mean(x)) * (b - statistics.mean(y)) for a, b in zip(x, y)) / sum((a - statistics.mean(x)) ** 2 for a in x)
    thresholds = config["thresholds"]
    checks = {
        "rss_growth": rss_growth <= max(thresholds["max_rss_growth_bytes"], rss_base * thresholds["max_rss_growth_ratio"]),
        "python_growth": final["python_bytes"] - warm[0]["python_bytes"] <= thresholds["max_python_growth_bytes"],
        "rss_slope": slope <= thresholds["max_rss_slope_bytes_per_minute"],
        "fd_growth": final["fds"] - warm[0]["fds"] <= thresholds["max_fd_growth"],
        "no_children": final["children"] == 0,
        "no_sdk_threads": not sdk_threads(),
    }
    return {"scenario": "R01", "status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "turns": turns, "seconds": time.monotonic() - started, "summaries": summaries,
            "rss_growth": rss_growth, "rss_slope_bytes_per_minute": slope, "final_resources": final}


def contracts(config, output):
    trials = []
    names = config.get("contract_scenarios", list(GROUPS))
    if not names or any(name not in GROUPS for name in names):
        raise ValueError("Unknown or empty contract scenario selection")
    groups = {name: GROUPS[name] for name in names}
    for repeat in range(config["contract_repeats"]):
        for scenario, names in groups.items():
            path = output / f"{scenario}-{repeat:03}.log"
            start = time.monotonic()
            suite = unittest.defaultTestLoader.loadTestsFromNames(names)
            with path.open("w", encoding="utf-8") as stream, contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            trials.append({"scenario": scenario, "trial": repeat, "scope": "contract_subset", "tests": result.testsRun,
                "status": "passed" if result.wasSuccessful() and not result.skipped else "failed" if not result.wasSuccessful() else "blocked",
                "skipped": [{"test": test.id(), "reason": reason} for test, reason in result.skipped],
                "seconds": time.monotonic() - start, "log": path.name})
            (output / "trials.json").write_text(json.dumps(trials, indent=2), encoding="utf-8")
    status = "failed" if any(t["status"] == "failed" for t in trials) else "blocked" if any(t["status"] == "blocked" for t in trials) else "passed"
    return {"status": status, "trials": trials,
            "scope": "Repeated contract subsets; not every fault window in the full P4-R matrix"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "tests/reliability/sdk-local.json")
    parser.add_argument("--mode", choices=("contracts", "lifecycle", "soak", "backpressure"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    random.seed(config["seed"])
    args.output.mkdir(parents=True, exist_ok=False)
    evidence = manifest(config, args.output)
    (args.output / "manifest.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    try:
        result = {"contracts": contracts, "lifecycle": lifecycle, "soak": soak, "backpressure": backpressure}[args.mode](config, args.output)
    except BaseException as exc:
        import traceback
        (args.output / "failure.log").write_text(traceback.format_exc(), encoding="utf-8")
        result = {"status": "failed", "exception": type(exc).__name__}
    result.update(ended_at=utc(), profile=config["profile"], full_p4r="not_run",
                  missing=["real-provider batch and cost ceiling", "other OS/Python combinations", "remaining fault windows and frozen full matrix"])
    (args.output / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "report": str(args.output / "report.json")}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
