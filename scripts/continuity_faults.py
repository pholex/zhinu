"""Crash recovery checked against a separate durable business ledger."""
from dataclasses import asdict
from contextlib import closing
import sqlite3
import time
import uuid

from continuity_support import check, child, read, report, save
from xiaoyu_agent_sdk import DurableTaskRunner, TaskAcceptance, WorkResult, WorkStep


def ledger(root, ctx):
    with closing(sqlite3.connect(root / "business.sqlite")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS attempts (step TEXT, key TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS effects (key TEXT PRIMARY KEY, step TEXT)")
        db.execute("INSERT INTO attempts VALUES (?,?)", (ctx.step.name, ctx.idempotency_key))
        key = ctx.idempotency_key if ctx.step.retry_safe else uuid.uuid4().hex
        db.execute("INSERT OR IGNORE INTO effects VALUES (?,?)", (key, ctx.step.name))


def business(root):
    if not (root / "business.sqlite").exists():
        return {"attempts": [], "effects": []}
    with closing(sqlite3.connect(root / "business.sqlite")) as db:
        return {name: db.execute(f"SELECT * FROM {name}").fetchall() for name in ("attempts", "effects")}


def worker(root, now, fault):
    config = read(root / "config.json")

    def hang():
        save(root / "entered.json", {"fault": fault})
        while True:
            time.sleep(1)

    def execute(ctx):
        if fault in {"before", "hang"}:
            hang()
        ledger(root, ctx)
        if fault == "after":
            hang()
        if config["kind"] in {"wait", "network"}:
            return WorkResult("waiting", "Submitted once", checkpoint="receipt:1", fingerprint="pending")
        return WorkResult("succeeded", "Business effect committed")

    def probe(ctx):
        if config["kind"] == "network":
            raise OSError("Injected unavailable service")
        return WorkResult("succeeded", "External approval arrived") if (root / "approved").exists() else WorkResult(
            "waiting", "Still pending", fingerprint="pending")

    def evaluate(ctx):
        passed = config["kind"] != "repair_fail" and ctx.attempt >= 2
        return TaskAcceptance("passed" if passed else "failed", "Independent acceptance result")

    with DurableTaskRunner(root / "work.sqlite", handlers={"run": execute}, probes={"probe": probe},
                           evaluators={"check": evaluate}, clock=lambda: now) as runner:
        run = runner.list()[0]
        save(root / "result.json", asdict(runner.tick(run.id)))


def setup(root, kind, safe=True, timeout=1000, max_calls=30):
    root.mkdir()
    save(root / "config.json", {"kind": kind})
    steps = (WorkStep("work", "run", retry_safe=safe, interval=10, max_retries=2,
                      probe="probe" if kind in {"wait", "network"} else ""),)
    if kind.startswith("repair"):
        steps = (WorkStep("first", "run"), WorkStep("second", "run", retry_safe=True,
                 evaluator="check", interval=10, max_repairs=2))
    with DurableTaskRunner(root / "work.sqlite", handlers={}, clock=lambda: 100) as runner:
        runner.create(steps, timeout=timeout, max_calls=max_calls)


def tick(root, now=100, fault="none", *, kill=False, timeout=20):
    outcome = child(["_work", root, now, fault], root, kill_at_marker=kill, timeout=timeout)
    if fault == "none" and outcome["returncode"] != 0:
        raise RuntimeError(f"Worker failed: {outcome}; see {root / 'worker.log'}")
    return outcome


def snapshot(root):
    return read(root / "result.json")


def run(root):
    started, rows = time.time(), []
    for safe in (True, False):
        for boundary in ("before", "after"):
            path = root / f"{'safe' if safe else 'unsafe'}_{boundary}"
            setup(path, "effect", safe)
            killed = tick(path, fault=boundary, kill=True)
            tick(path)
            state, book = snapshot(path), business(path)
            expected_effects = 1 if safe or boundary == "after" else 0
            expected_attempts = (2 if boundary == "after" else 1) if safe else (1 if boundary == "after" else 0)
            keys = [a[1] for a in book["attempts"]]
            rows.append(check(path.name, killed["reason"] == "injected_kill" and
                state["state"] == ("completed" if safe else "uncertain") and
                len(book["effects"]) == expected_effects and len(keys) == expected_attempts and len(set(keys)) <= 1,
                process=killed, state=state, business=book))

    path = root / "waiting_restarts"
    setup(path, "wait")
    tick(path)
    for now in (101, 109, 110, 111, 119):
        tick(path, now)
    waiting = snapshot(path)
    (path / "approved").touch()
    tick(path, 120)
    rows.append(check(path.name, waiting["calls"] == 2 and waiting["steps"][0]["next_check_at"] == 120
        and waiting["steps"][0]["checkpoint"] == "receipt:1" and snapshot(path)["state"] == "completed"
        and len(business(path)["attempts"]) == 1, waiting=waiting, final=snapshot(path), business=business(path)))

    path = root / "network_backoff"
    setup(path, "network")
    due = []
    for now in (100, 110, 119, 120, 139, 140):
        tick(path, now)
        due.append((now, snapshot(path)["calls"], snapshot(path)["steps"][0]["next_check_at"]))
    state = snapshot(path)
    rows.append(check(path.name, state["state"] == "failed" and state["calls"] == 4
        and [r[1] for r in due] == [1, 2, 2, 3, 3, 4] and due[1][2] == 120 and due[3][2] == 140
        and state["steps"][0]["repairs"] == 0 and len(business(path)["attempts"]) == 1, timeline=due, state=state))

    for kind in ("repair_pass", "repair_fail"):
        path = root / kind
        setup(path, kind)
        for now in (100, 100, 100, 110, 110, 120, 120, 130):
            tick(path, now)
        state, book = snapshot(path), business(path)
        attempts = [a[0] for a in book["attempts"]]
        rows.append(check(kind, state["state"] == ("completed" if kind == "repair_pass" else "failed")
            and attempts.count("first") == 1 and attempts.count("second") == (2 if kind == "repair_pass" else 3),
            state=state, business=book))

    for name, timeout, calls in (("deadline", 5, 30), ("call_budget", 1000, 1)):
        path = root / name
        setup(path, "wait", timeout=timeout, max_calls=calls)
        tick(path)
        tick(path, 110)
        tick(path, 120)
        rows.append(check(name, snapshot(path)["state"] == "failed" and snapshot(path)["calls"] == 1,
                          state=snapshot(path)))

    path = root / "hung_callback"
    setup(path, "effect", safe=False)
    killed = tick(path, fault="hang", timeout=3)
    tick(path)
    rows.append(check(path.name, killed["reason"] == "timeout" and killed["entered"]
        and snapshot(path)["state"] == "uncertain" and not business(path)["effects"],
        process=killed, state=snapshot(path), boundary="Host test supervisor, not a new SDK watchdog"))
    return report(root, "faults", "offline-processes", started, rows)
