"""Serial, resumable host exercise; no background service is installed."""
from dataclasses import asdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from continuity_support import ask, check, child, read, report, save, source
from xiaoyu_agent_sdk import DurableTaskRunner, TaskAcceptance, WorkResult, WorkStep

SCHEMA = {"type": "object", "properties": {"rounding": {"type": "string"}},
          "required": ["rounding"], "additionalProperties": False}
CONTRACT = '''import decimal, json, pathlib, unittest
class InvoiceContract(unittest.TestCase):
    def test_rounding(self):
        config = json.loads(pathlib.Path("invoice.json").read_text(encoding="utf-8", errors="replace"))
        for raw, expected in (("1.005", "1.01"), ("-1.005", "-1.01"), ("2.675", "2.68")):
            self.assertEqual(str(decimal.Decimal(raw).quantize(decimal.Decimal(".01"), rounding=config["rounding"])), expected)
if __name__ == "__main__": unittest.main()
'''


def code_digest():
    paths = list(Path(__file__).parent.glob("continuity*.py"))
    from continuity_support import REPO
    paths += list((REPO / "xiaoyu").rglob("*.py"))
    paths += list((REPO / "packages/xiaoyu-agent-sdk/src").rglob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(REPO)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def worker(root, live, fault):
    config = read(root / "campaign.json")
    if config["code_digest"] != code_digest():
        raise ValueError("Source changed during campaign; start a new campaign")

    def execute(ctx):
        key = hashlib.sha256(ctx.idempotency_key.encode()).hexdigest()
        receipt = root / f"artifact-{key}.json"
        if receipt.exists():
            result = read(receipt)
        else:
            if live:
                # Reserve before I/O; killed/failed requests still consume the campaign budget.
                with closing(sqlite3.connect(root / "requests.sqlite")) as db, db:
                    db.execute("CREATE TABLE IF NOT EXISTS requests (key TEXT, at REAL)")
                    db.execute("BEGIN IMMEDIATE")
                    if db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] >= config["max_model_requests"]:
                        return WorkResult("blocked", "Campaign model request budget exhausted")
                    db.execute("INSERT INTO requests VALUES (?,?)", (key, time.time()))
            prompt = ("Fix the invoice JSON configuration. Use a Python decimal rounding constant so that "
                      "1.005 -> 1.01, -1.005 -> -1.01 and 2.675 -> 2.68 at two decimal places. "
                      "Return only the rounding field. Previous test evidence: " + ctx.evidence)
            result = ask(root, prompt, SCHEMA, live=live, responder=lambda _: json.dumps(
                {"rounding": "ROUND_HALF_EVEN" if ctx.attempt == 1 else "ROUND_HALF_UP"}))
            save(receipt, result)
        if result["output_status"] != "valid":
            return WorkResult("failed", "Invalid model output; inspect artifact receipt")
        # Data only: generated text is never executed as a program.
        save(root / "invoice.json", result["output"])
        if fault:
            save(root / "entered.json", {"after_artifact": key})
            while True:
                time.sleep(1)
        return WorkResult("waiting", "Configuration ready; tests scheduled", checkpoint=key, fingerprint="submitted")

    def probe(ctx):
        if not (root / "release-tests").exists():
            return WorkResult("waiting", "Test service pending", fingerprint="pending")
        with (root / "contract.log").open("wb") as log:
            result = subprocess.run([sys.executable, "-I", str(root / "test_contract.py")],
                                    cwd=root, stdout=log, stderr=log, timeout=5, stdin=subprocess.DEVNULL)
        save(root / "test-result.json", {"key": ctx.checkpoint, "returncode": result.returncode})
        return WorkResult("succeeded", "Independent test process finished")

    def evaluate(ctx):
        result = read(root / "test-result.json")
        passed = result["key"] == ctx.checkpoint and result["returncode"] == 0
        return TaskAcceptance("passed" if passed else "failed", "Invoice tie rounding tests " + ("passed" if passed else "failed"))

    with DurableTaskRunner(root / "work.sqlite", handlers={"fix": execute}, probes={"tests": probe},
                           evaluators={"contract": evaluate}) as runner:
        runs = runner.list()
        if not runs or runs[-1].state == "completed":
            if len(runs) >= config["max_cycles"]:
                save(root / "tick.json", {"limit": "max_cycles"})
                return
            (root / "release-tests").unlink(missing_ok=True)
            run = runner.create((WorkStep("invoice", "fix", probe="tests", evaluator="contract",
                                         retry_safe=True, interval=config["interval"], max_repairs=2),),
                                timeout=max(120, config["interval"] * 20), max_calls=30)
        else:
            run = runs[-1]
        save(root / "tick.json", asdict(runner.tick(run.id)))


def run(root, *, seconds, interval, live, max_cycles, max_model_requests, resume):
    if resume:
        config = read(root / "campaign.json")
        if config["code_digest"] != code_digest():
            raise ValueError("Source changed; start a new campaign to keep evidence attributable")
        if config["live"] != live:
            raise ValueError("Resume must retain the original live/offline mode")
        state = read(root / "progress.json")
        if state["status"] != "running":
            raise ValueError("Campaign has ended; start a new output directory")
        seconds, interval = config["seconds"], config["interval"]
        state["unobserved_seconds"] += max(0, time.time() - state["last_seen"])
    else:
        config = {"seconds": seconds, "interval": interval, "live": live, "max_cycles": max_cycles,
                  "max_model_requests": max_model_requests, "code_digest": code_digest(), "source": source()}
        save(root / "campaign.json", config)
        state = {"started_at": time.time(), "last_seen": time.time(), "observed_seconds": 0,
                 "unobserved_seconds": 0, "ticks": 0, "injected_kills": 0, "status": "running"}
        save(root / "progress.json", state)
        (root / "test_contract.py").write_text(CONTRACT, encoding="utf-8")
    previous = time.monotonic()
    try:
        while state["observed_seconds"] < seconds:
            # One forced exit after a committed artifact per campaign, then normal restarts every tick.
            inject = state["injected_kills"] == 0
            outcome = child(["_soak", root, int(live), int(inject)], root,
                            timeout=60 if live else 15, kill_at_marker=inject)
            if outcome["reason"] == "injected_kill":
                state["injected_kills"] += 1
            elif outcome["returncode"] != 0:
                state["status"] = "failed"
            state["ticks"] += 1
            if (root / "tick.json").exists():
                current = read(root / "tick.json")
                if "limit" in current:
                    state["status"] = "budget_exhausted"
                elif current["state"] in {"failed", "uncertain", "paused"}:
                    state["status"] = "budget_exhausted" if "budget exhausted" in current["steps"][0]["evidence"] else "failed"
                # An external fixture releases tests after a real waiting interval, without a model call.
                elif current["steps"][0]["state"] == "waiting":
                    if time.time() >= current["steps"][0]["next_check_at"]:
                        (root / "release-tests").touch()
                    # After first observation, release on next loop; the worker still enforces due time.
                    elif current["steps"][0]["unchanged"] >= 1:
                        (root / "release-tests").touch()
            state["observed_seconds"] += time.monotonic() - previous
            state["last_seen"] = time.time()
            previous = time.monotonic()
            save(root / "progress.json", state)
            if state["status"] != "running" or state["observed_seconds"] >= seconds:
                break
            time.sleep(min(interval, max(0, seconds - state["observed_seconds"])))
    except KeyboardInterrupt:
        # Completed intervals remain resumable; downtime is not credited as supervision.
        save(root / "progress.json", state)
    if state["status"] == "running" and state["observed_seconds"] >= seconds:
        state["status"] = "completed"
    save(root / "progress.json", state)
    with DurableTaskRunner(root / "work.sqlite", handlers={}) as runner:
        runs = [asdict(r) for r in runner.list()]
    requests = 0
    if (root / "requests.sqlite").exists():
        with closing(sqlite3.connect(root / "requests.sqlite")) as db:
            requests = db.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    rows = [check("observed_duration", state["status"] == "completed", progress=state),
            check("accepted_cycles", any(r["state"] == "completed" for r in runs), runs=runs),
            check("no_failed_runs", all(r["state"] in {"active", "completed"} for r in runs)),
            check("campaign_request_budget", requests <= config["max_model_requests"], reserved_requests=requests)]
    return report(root, "soak", "live-model" if live else "offline-real-processes", state["started_at"], rows,
                  progress=state, reservations=requests, usd=None,
                  limitations=["Local invoice fixture, not production CI/network availability",
                               "Elapsed calendar time is separate from observed running time"])
