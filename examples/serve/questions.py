"""Explicit HTTP question host; --demo exercises the real app without a network."""
from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


RequestJSON = Callable[[str, str, dict[str, Any] | None, dict[str, str] | None], dict[str, Any]]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        # Keep the instance credential at the explicitly configured endpoint.
        return None


def http_transport(base_url: str, token: str = "") -> RequestJSON:
    opener = build_opener(NoRedirect())

    def request(method: str, path: str, body: dict[str, Any] | None = None,
                headers: dict[str, str] | None = None) -> dict[str, Any]:
        fields = {"Accept": "application/json", **(headers or {})}
        if token:
            fields["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            fields["Content-Type"] = "application/json"
        req = Request(base_url.rstrip("/") + path, data=data, headers=fields, method=method)
        try:
            with opener.open(req, timeout=35) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {detail}") from None
        return json.loads(raw) if raw else {}

    return request


class QuestionHost:
    """Observe latest states; submission and starting a turn are explicit actions."""

    def __init__(self, request: RequestJSON, session_id: str):
        self.request = request
        self.session_id = session_id
        self.path = "/session/" + quote(session_id, safe="")
        self.cursor = 1
        self.questions: dict[str, dict[str, Any]] = {}

    def reconnect(self) -> list[dict[str, Any]]:
        snapshot = self.request("GET", self.path + "/questions?include_terminal=true", None, None)
        changed = []
        for question in snapshot["questions"]:
            ident = question["question_id"]
            previous = self.questions.get(ident)
            if previous is None or question["version"] > previous["version"]:
                self.questions[ident] = question
                changed.append(question)
        # The server captures this cursor before reading the snapshot. Repeated
        # events are expected; version comparison above makes them harmless.
        self.cursor = snapshot["next_seq"]
        return changed

    def poll(self, wait: float = 20) -> list[dict[str, Any]]:
        query = urlencode({"from": self.cursor, "wait": wait})
        page = self.request("GET", self.path + "/events?" + query, None, None)
        if page["first_seq"] > self.cursor or page["events"]:
            # Treat bounded events as wakeups. Read complete snapshots even when
            # nested question IDs/text or event kinds were clipped by max_field.
            return self.reconnect()
        self.cursor = page["next"]
        return []

    def prompt(self, text: str, key: str) -> dict[str, Any]:
        return self.request("POST", self.path + "/prompt_async", {"text": text},
                            {"Idempotency-Key": key})

    def answer(self, question_id: str, submission: dict[str, Any]) -> dict[str, Any]:
        # Keep the complete body, including its idempotency key, for retry.
        path = self.path + "/questions/" + quote(question_id, safe="") + "/answers"
        return self.request("POST", path, submission, None)["question"]

    def get(self, question_id: str) -> dict[str, Any]:
        return self.request("GET", self.path + "/questions/" + quote(question_id, safe=""),
                            None, None)["question"]

    def status(self) -> dict[str, Any]:
        return self.request("GET", self.path + "/status", None, None)


DEMO_SCRIPT = ('tool_call: {"name":"ask_user","arguments":{"questions":'
               '[{"question":"Which output format?","options":["JSON","Markdown"]}]}}\n'
               '---\ntext: Waiting for your answer.\n---\ntext: Answer received.\n')


@contextmanager
def demo_server(root: Path, *, buffer_limit: int = 100, max_field: int = 12000,
                script: str = DEMO_SCRIPT) -> Iterator[RequestJSON]:
    """Test-only server setup. Real host operations above use only REST."""
    from unittest.mock import patch

    from fastapi.testclient import TestClient
    from xiaoyu import Config, Permissions
    from xiaoyu.serve import ServeConfig, create_app

    script_path = root / "model.txt"
    script_path.write_text(script, encoding="utf-8")
    config = Config(base_url="http://invalid.local/v1", model="demo", workspace=root,
                    enable_skills=False, enable_plugins=False, enable_mcp=False,
                    enable_hooks=False, enable_agents=False, enable_explore=False,
                    enable_web_search=False, enable_browser=False, enable_peers=False,
                    enable_chenshu=False, load_project_instructions=False)
    # The scripted registry is exclusive: no credential discovery or model I/O.
    with patch.dict(os.environ, {"XIAOYU_SCRIPTED_SCRIPTS": str(script_path)}), \
         patch("xiaoyu.serve.Config.from_env", return_value=config), \
         patch("xiaoyu.serve.Permissions.load", return_value=Permissions(root)):
        cfg = ServeConfig(root=root, state_dir=root / "state", token="demo-token",
                          mcp=False, max_sessions=1, buffer_limit=buffer_limit,
                          max_field=max_field)
        with TestClient(create_app(cfg), base_url="http://127.0.0.1:8420") as client:
            def request(method: str, path: str, body: dict[str, Any] | None = None,
                        headers: dict[str, str] | None = None) -> dict[str, Any]:
                response = client.request(method, path, json=body, headers={
                    "Authorization": "Bearer demo-token", **(headers or {})})
                response.raise_for_status()
                return response.json() if response.content else {}
            yield request


def wait_until(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Demo did not reach the expected state")
        time.sleep(0.01)


def demo() -> None:
    for foreground in (0, 60):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with demo_server(root) as request:
                created = request("POST", "/session", {
                    "questions": {"foreground_timeout_seconds": foreground}}, None)
                host = QuestionHost(request, created["session_id"])
                host.reconnect()
                host.prompt("Ask which output format I prefer.", "demo-prompt")
                wait_until(lambda: bool(host.poll(0) or host.questions))
                question = next(iter(host.questions.values()))
                if not foreground:
                    wait_until(lambda: not host.status()["busy"])
                print("Question:", json.dumps(question, ensure_ascii=False))
                submission = {"answers": [{"item_id": question["items"][0]["item_id"],
                                           "selected": ["JSON"]}],
                              "idempotency_key": "demo-explicit-answer"}
                # Synthetic choice only in --demo; real mode reads a user-authored file.
                receipt = host.answer(question["question_id"], submission)
                assert receipt["state"] in {"queued", "answered"}
                if not foreground:
                    assert host.status()["turns"] == 1 and not host.status()["busy"]
                    assert host.get(question["question_id"])["state"] == "queued"
                    host.prompt("Continue with my submitted answer.", "demo-continue")
                wait_until(lambda: not host.status()["busy"])
                assert host.get(question["question_id"])["state"] == "answered"
                retry = host.answer(question["question_id"], submission)
                assert retry["answer_id"] == receipt["answer_id"]
                host.reconnect()
                assert host.reconnect() == []
                print("Accepted once; repeated snapshot ignored; foreground:", foreground)
    print("HTTP questions demo: OK (offline)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:8420")
    commands = parser.add_subparsers(dest="command")
    create = commands.add_parser("create")
    create.add_argument("--foreground-timeout", type=float, default=0)
    for name in ("prompt", "watch", "get", "answer"):
        command = commands.add_parser(name)
        command.add_argument("session_id")
        if name in {"get", "answer"}:
            command.add_argument("question_id")
        if name == "prompt":
            command.add_argument("text")
            command.add_argument("--key", required=True)
        if name == "answer":
            command.add_argument("file", type=Path, help="JSON containing answers and idempotency_key")
        if name == "watch":
            command.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.demo:
        demo()
        return
    if args.command is None:
        parser.error("choose a command or --demo")
    request = http_transport(args.url, os.environ.get("XIAOYU_SERVE_TOKEN", ""))
    if args.command == "create":
        result = request("POST", "/session", {
            "questions": {"foreground_timeout_seconds": args.foreground_timeout}}, None)
    else:
        host = QuestionHost(request, args.session_id)
        if args.command == "prompt":
            result = host.prompt(args.text, args.key)
        elif args.command == "answer":
            submission = json.loads(args.file.read_text(encoding="utf-8", errors="replace"))
            result = host.answer(args.question_id, submission)
        elif args.command == "get":
            result = host.get(args.question_id)
        else:
            changes = host.reconnect()
            try:
                while True:
                    for question in changes:
                        print(json.dumps(question, ensure_ascii=False), flush=True)
                    if args.once:
                        return
                    changes = host.poll()
            except KeyboardInterrupt:
                return
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
