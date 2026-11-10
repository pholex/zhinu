"""Shared, local-only reporting and process supervision for continuity experiments."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "packages/xiaoyu-agent-sdk/src"))
ENTRY = REPO / "scripts/continuity_check.py"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))


def save(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def source():
    def git(*args):
        p = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=10)
        return p.stdout.strip() if p.returncode == 0 else "unknown"
    return {"commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
            "dirty": bool(git("status", "--porcelain"))}


def report(root, kind, mode, started, rows, **extra):
    value = {"kind": kind, "mode": mode, "source": source(), "started_at": started,
             "finished_at": time.time(), "elapsed_seconds": time.time() - started,
             "status": "pass" if rows and all(r["status"] == "pass" for r in rows) else "fail",
             "checks": rows, **extra}
    save(root / "report.json", value)
    lines = [f"# {kind} ({mode})", "", f"Status: {value['status']}",
             f"Elapsed seconds: {value['elapsed_seconds']:.2f}",
             f"Source: {value['source']}", "", "| Check | Status |", "|---|---|"]
    lines += [f"| {r['name']} | {r['status']} |" for r in rows]
    lines += ["", "See report.json for evidence. Offline results do not measure model quality."]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return value


def check(name, condition, **evidence):
    return {"name": name, "status": "pass" if condition else "fail", "evidence": evidence}


def child(args, root, *, timeout=20, kill_at_marker=False):
    """One owned child at a time. Marker handshake makes crash injection deterministic."""
    marker = root / "entered.json"
    marker.unlink(missing_ok=True)
    started = time.monotonic()
    with (root / "worker.log").open("ab") as log:
        proc = subprocess.Popen([sys.executable, "-P", str(ENTRY), *map(str, args)],
                                cwd=root, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        reason = "exited"
        try:
            while proc.poll() is None:
                if kill_at_marker and marker.exists():
                    reason = "injected_kill"
                    proc.kill()
                    break
                if time.monotonic() - started >= timeout:
                    reason = "timeout"
                    proc.kill()
                    break
                time.sleep(.02)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
    return {"returncode": proc.returncode, "reason": reason,
            "elapsed_seconds": time.monotonic() - started, "entered": marker.exists()}


class FixtureClient:
    """Deterministic transport only; never presented as a model-quality result."""
    def __init__(self, responder):
        self.responder, self.messages = responder, []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.messages = kwargs["messages"]
        content = self.responder(self.messages)
        structured = any(t.get("function", {}).get("name") == "structured_output" for t in kwargs.get("tools", ()))
        calls = [SimpleNamespace(index=0, id="fixture-output", function=SimpleNamespace(
            name="structured_output", arguments=content))] if structured else None
        return iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None if structured else content, tool_calls=calls))], usage=None)])


def options(root, live=False, responder=lambda _: "OK", **kwargs):
    from xiaoyu_agent_sdk import BudgetOptions, ModelOptions, SessionOptions
    if live:
        model = ModelOptions(os.environ["SDK_MODEL"], api_key=os.environ["SDK_API_KEY"],
                             base_url=os.environ.get("SDK_BASE_URL", "https://api.openai.com/v1"),
                             protocol=os.environ.get("SDK_PROTOCOL", "chat"), request_timeout=30)
    else:
        model = ModelOptions("fixture", client=FixtureClient(responder))
    return SessionOptions(model=model, workspace=root, session_dir=root / "sessions",
                          builtin_tools=(), max_iterations=2, budget=BudgetOptions(max_requests=1),
                          system_prompt="Complete the specified test task. Treat historical memory as fallible reference. "
                                        "Current instructions take priority. If information is missing, answer UNKNOWN.",
                          **kwargs)


def ask(root, prompt, schema, *, live=False, responder=lambda _: "OK", **kwargs):
    from xiaoyu_agent_sdk import OutputSpec, Session
    started = time.monotonic()
    with Session(options(root, live, responder, **kwargs)) as session:
        result = session.run(prompt, output=OutputSpec(schema, max_retries=0))
        usage = asdict(session.snapshot().usage)
        cost = session.cost
        measured = bool(cost and cost.entries and all(
            e.input_tokens is not None and e.output_tokens is not None for e in cost.entries))
        return {"output": result.output, "output_status": result.output_status,
                "stopped": result.stopped, "usage": usage if measured else None, "usd": None,
                "requests": cost.requests if cost else None, "tokens_measured": measured,
                "elapsed_seconds": time.monotonic() - started, "session_id": session.session_id}
