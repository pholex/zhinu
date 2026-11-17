"""Catalog contracts use temporary terminal journals, never personal histories."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_serve import ServeCase
from xiaoyu import session_log
from xiaoyu.serve_catalog import Catalog


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "需要安全目录句柄")
class CatalogTests(ServeCase):
    mcp = False
    token = "catalog-test-access"

    def begin(self, script="text: continued\n", **extra):
        self.start(script, **extra)
        self.assertTrue(session_log.sessions_dir().resolve().is_relative_to(Path(self.tmp)))

    def fixture(self, name="terminal", workspace=None, *, close=True, extra=None):
        log = session_log.SessionLog.create("source-private-model", str(workspace or self.root), session_id=name)
        log.event("system_prompt", text="source-private-configuration")
        log.append({"role": "user", "content": "terminal question"})
        log.append({"role": "assistant", "content": "terminal reply"})
        for record in extra or []:
            log._write(record)
        if close:
            log.close()
        else:
            self.addCleanup(log.release)
        return log

    def listing(self):
        response = self.client.get("/catalog/sessions", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def preview(self, item, **params):
        return self.client.get(f"/catalog/sessions/{item['catalog_id']}/history", params=params, headers=self.headers())

    def branch(self, item, key="branch-once", **extra):
        body = {"fork_from": {"catalog_id": item["catalog_id"], "revision": item["revision"]}, **extra}
        return self.client.post("/session", json=body, headers={**self.headers(), "Idempotency-Key": key})

    def test_scope_auth_read_only_preview_and_pagination(self):
        self.begin()
        source = self.fixture()
        original = source.path.read_bytes()
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        self.fixture("outside", outside)
        nested = self.root / "nested"
        nested.mkdir()
        self.fixture("nested", nested)
        self.assertEqual(self.client.get("/catalog/sessions").status_code, 401)
        items = self.listing()["sessions"]
        self.assertEqual(len(items), 2)
        item = next(item for item in items if item["workspace"] == str(self.root))
        self.assertTrue(item["can_fork"])
        self.assertFalse(item["locked"])
        self.assertNotIn("path", item)
        first = self.preview(item, limit=1).json()
        second = self.preview(item, cursor=first["next_cursor"], limit=1).json()
        self.assertEqual([first["messages"][0]["content"], second["messages"][0]["content"]],
                         ["terminal question", "terminal reply"])
        self.assertEqual(first["revision"], item["revision"])
        self.assertEqual(source.path.read_bytes(), original)
        self.assertEqual(self.client.get("/catalog/sessions/unknown/history", headers=self.headers()).status_code, 404)

    def test_everywhere_lists_and_previews_other_workspaces_but_never_writes_for_them(self):
        self.begin(catalog_everywhere=True)
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        foreign = self.fixture("foreign", outside)
        original = foreign.path.read_bytes()
        self.fixture("local")
        gone = Path(self.tmp) / "gone"
        gone.mkdir()
        self.fixture("gone", gone)
        gone.rmdir()
        items = {item["workspace"]: item for item in self.listing()["sessions"]}
        self.assertEqual(set(items), {str(self.root), str(outside)})  # 目录已不存在的不列
        self.assertTrue(items[str(self.root)]["in_scope"])
        self.assertTrue(items[str(self.root)]["can_continue"])
        item = items[str(outside)]
        self.assertFalse(item["in_scope"])
        self.assertFalse(item["locked"])
        self.assertFalse(item["can_continue"])
        self.assertFalse(item["can_fork"])
        self.assertIn("另一个工作目录", item["continue_reason"])
        self.assertNotIn("path", item)
        preview = self.preview(item)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual([m["content"] for m in preview.json()["messages"]], ["terminal question", "terminal reply"])
        self.assertEqual(self.adopt(item).status_code, 409)
        self.assertEqual(self.branch(item).status_code, 409)
        self.assertEqual(self.client.get("/session", headers=self.headers()).json()["sessions"], [])
        self.assertEqual(foreign.path.read_bytes(), original)

    def test_listing_reuses_parsed_facts_until_the_journal_changes_and_probes_the_lock_each_time(self):
        self.begin()
        source = self.fixture()
        catalog = self.client.app.state.catalog
        reads = []
        parse = catalog._read
        with patch.object(catalog, "_read", lambda fd, size: reads.append(size) or parse(fd, size)):
            first = self.listing()["sessions"][0]
            self.assertTrue(first["can_continue"])
            self.assertEqual(self.listing()["sessions"][0], first)
            self.assertEqual(len(reads), 1)
            # 内容没变、只是被终端重新打开：不重读，但占用状态要跟上
            holder = session_log.SessionLog(source.path)
            self.addCleanup(holder.release)
            held = self.listing()["sessions"][0]
            self.assertEqual(len(reads), 1)
            self.assertTrue(held["locked"])
            self.assertFalse(held["can_continue"])
            holder.append({"role": "user", "content": "one more"})
            changed = self.listing()["sessions"][0]
            self.assertEqual(len(reads), 2)
            self.assertNotEqual(changed["revision"], first["revision"])
            self.assertFalse(changed["can_fork"])

    def test_fork_is_new_log_preserves_source_and_does_not_inherit_config(self):
        self.begin()
        source = self.fixture()
        original = source.path.read_bytes()
        item = self.listing()["sessions"][0]
        response = self.branch(item)
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        sid = created["session_id"]
        self.assertEqual(created["fork_origin"]["catalog_id"], item["catalog_id"])
        self.assertNotEqual(created["model"], "source-private-model")
        target = self.client.app.state.sessions[sid]
        self.assertNotEqual(target.agent.session_log.path, source.path)
        self.assertNotIn("source-private-configuration", target.agent.session_log.path.read_text())
        messages = self.client.get(f"/session/{sid}/history", headers=self.headers()).json()["messages"]
        self.assertEqual([m["content"] for m in messages], ["terminal question", "terminal reply"])
        self.client.post(f"/session/{sid}/prompt", json={"text": "continue here"}, headers=self.headers())
        self.assertEqual(source.path.read_bytes(), original)
        again = self.branch(item).json()
        self.assertEqual(again["session_id"], sid)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.client.get("/session", headers=self.headers()).json()["sessions"]), 1)

    def test_creation_retry_survives_restart_even_if_source_changes(self):
        state = Path(self.tmp) / "serve-state"
        self.begin(state_dir=state)
        source = self.fixture()
        item = self.listing()["sessions"][0]
        sid = self.branch(item).json()["session_id"]
        self.client.__exit__(None, None, None)
        with source.path.open("a") as handle:
            handle.write(json.dumps({"event": "exit", "reason": "normal"}) + "\n")
        self.start("text: unused\n", state_dir=state)
        again = self.branch(item)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["session_id"], sid)
        self.assertEqual(again.json()["fork_origin"]["revision"], item["revision"])

    def test_active_source_forks_only_from_a_settled_turn_boundary(self):
        self.begin()
        resting = self.fixture("resting", close=False)
        original = resting.path.read_bytes()
        asking = self.fixture("asking", close=False, extra=[{"role": "user", "content": "still being answered"}])
        calling = self.fixture("calling", close=False, extra=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]},
        ])
        items = {item["catalog_id"]: item for item in self.listing()["sessions"]}
        by_path = {path: items[self.client.app.state.catalog._id(path.relative_to(session_log.sessions_dir().resolve()))]
                   for path in (resting.path.resolve(), asking.path.resolve(), calling.path.resolve())}
        for item in by_path.values():
            self.assertTrue(item["locked"])
            self.assertFalse(item["can_continue"])
            self.assertEqual(self.preview(item).status_code, 200)
        item = by_path[resting.path.resolve()]
        self.assertTrue(item["can_fork"])
        response = self.branch(item)
        self.assertEqual(response.status_code, 200, response.text)
        messages = self.client.get(f"/session/{response.json()['session_id']}/history", headers=self.headers()).json()["messages"]
        self.assertEqual([m["content"] for m in messages], ["terminal question", "terminal reply"])
        self.assertEqual(resting.path.read_bytes(), original)
        for path in (asking.path.resolve(), calling.path.resolve()):
            self.assertFalse(by_path[path]["can_fork"])
            self.assertIn("回合边界", by_path[path]["fork_reason"])
            self.assertEqual(self.branch(by_path[path], key=by_path[path]["catalog_id"]).status_code, 409)

    def adopt(self, item, key="", **extra):
        body = {"adopt_from": {"catalog_id": item["catalog_id"], "revision": item["revision"]}, **extra}
        return self.client.post("/session", json=body,
                                headers={**self.headers(), **({"Idempotency-Key": key} if key else {})})

    def test_continue_in_place_takes_the_lock_and_appends_to_the_same_journal(self):
        self.begin()
        source = self.fixture()
        original = source.path.read_bytes()
        item = self.listing()["sessions"][0]
        self.assertTrue(item["can_continue"])
        self.assertEqual(item["session_id"], "")
        response = self.adopt(item, key="continue-once")
        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()
        sid = created["session_id"]
        self.assertEqual(created["fork_origin"]["kind"], "cli_adopted")
        self.assertEqual(created["title"], item["title"])
        self.assertNotEqual(created["model"], "source-private-model")
        target = self.client.app.state.sessions[sid]
        self.assertEqual(target.agent.session_log.path.resolve(), source.path.resolve())
        self.assertTrue(created["writable"], created["write_blocked_reason"])
        messages = self.client.get(f"/session/{sid}/history", headers=self.headers()).json()["messages"]
        self.assertEqual([m["content"] for m in messages], ["terminal question", "terminal reply"])
        # 同一个幂等键重放：还是这场会话，不再抢一次锁
        again = self.adopt(item, key="continue-once")
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["session_id"], sid)
        # 目录把它认成「已在本服务继续」，不是「被终端占用」
        held = self.listing()["sessions"][0]
        self.assertEqual(held["session_id"], sid)
        self.assertFalse(held["locked"])
        self.assertFalse(held["can_continue"])
        self.assertFalse(held["can_fork"])
        self.assertEqual(self.adopt(held).status_code, 409)
        # 终端那边此刻打不开它
        with self.assertRaises(session_log.SessionLockedError):
            session_log.SessionLog(source.path)
        response = self.client.post(f"/session/{sid}/prompt", json={"text": "continue here"}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        grown = source.path.read_bytes()
        self.assertTrue(grown.startswith(original))
        records = [json.loads(line) for line in grown[len(original):].decode().splitlines()]
        self.assertEqual(records[0]["event"], "reopened")
        self.assertIn("continue here", [r.get("content") for r in records if r.get("role") == "user"])
        self.assertIn("continued", [r.get("content") for r in records if r.get("role") == "assistant"])
        # 历史没有被再抄一遍
        self.assertEqual(sum(1 for line in grown.decode().splitlines() if "terminal question" in line), 1)
        # 关掉会话 = 交还：留下正常结束标记，终端能接着打开，目录里重新可继续
        self.assertEqual(self.client.delete(f"/session/{sid}", headers=self.headers()).status_code, 200)
        returned = self.listing()["sessions"][0]
        self.assertEqual(returned["session_id"], "")
        self.assertTrue(returned["can_continue"], returned["continue_reason"])
        reopened, restored = session_log.open_named("terminal", "m", str(self.root))
        self.addCleanup(reopened.release)
        self.assertEqual([m["content"] for m in restored if m["role"] == "user"][:2],
                         ["terminal question", "continue here"])

    def test_continue_conflicts_while_a_terminal_holds_the_journal_then_retries(self):
        self.begin()
        source = self.fixture(close=False)
        item = self.listing()["sessions"][0]
        self.assertTrue(item["locked"])
        self.assertIn("终端", item["continue_reason"])
        response = self.adopt(item)
        self.assertEqual(response.status_code, 423, response.text)
        self.assertEqual(response.json()["detail"]["code"], "writer_conflict")
        self.assertEqual(self.client.get("/session", headers=self.headers()).json()["sessions"], [])
        source.close()
        # 旧预览已过期：必须按新修订重试
        self.assertEqual(self.adopt(item).status_code, 409)
        fresh = self.listing()["sessions"][0]
        self.assertFalse(fresh["locked"])
        self.assertEqual(self.adopt(fresh).status_code, 200)

    def test_continue_loses_a_race_for_the_lock_without_leaving_a_session(self):
        self.begin()
        source = self.fixture()
        original = source.path.read_bytes()
        item = self.listing()["sessions"][0]
        catalog = self.client.app.state.catalog
        probe = catalog.adopt
        holder = []

        def raced(reference):
            result = probe(reference)
            holder.append(session_log.SessionLog(source.path))  # 探锁之后、真正拿锁之前被终端抢先
            return result

        with patch.object(catalog, "adopt", raced):
            response = self.adopt(item)
        self.addCleanup(holder[0].release)
        self.assertEqual(response.status_code, 423, response.text)
        self.assertEqual(response.json()["detail"]["code"], "writer_conflict")
        self.assertEqual(self.client.get("/session", headers=self.headers()).json()["sessions"], [])
        self.assertEqual(source.path.read_bytes(), original)

    def test_continue_refuses_unsafe_sources_and_foreign_options(self):
        self.begin()
        crashed = self.fixture("crashed", close=False)
        crashed.release()
        pending = self.fixture("pending", extra=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]},
        ])
        before = {path: path.read_bytes() for path in (crashed.path, pending.path)}
        for item in self.listing()["sessions"]:
            self.assertFalse(item["can_continue"])
            self.assertEqual(self.adopt(item).status_code, 409)
        for path, body in before.items():
            self.assertEqual(path.read_bytes(), body)
        good = self.fixture("good")
        item = next(i for i in self.listing()["sessions"] if i["can_continue"])
        reference = {"catalog_id": item["catalog_id"], "revision": item["revision"]}
        for body in ({"adopt_from": {"catalog_id": item["catalog_id"]}},
                     {"adopt_from": reference, "fork_from": reference},
                     {"adopt_from": reference, "agent": "any"},
                     {"adopt_from": reference, "workspace": str(Path(self.tmp))}):
            response = self.client.post("/session", json=body, headers=self.headers())
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get("/session", headers=self.headers()).json()["sessions"], [])
        # 被拒的请求不能留着锁
        session_log.SessionLog(good.path).release()

    def test_continued_session_survives_restart_and_delete_keeps_the_terminal_journal(self):
        state = Path(self.tmp) / "serve-state"
        self.begin(state_dir=state)
        source = self.fixture()
        item = self.listing()["sessions"][0]
        sid = self.adopt(item).json()["session_id"]
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state)
        restored = self.client.app.state.sessions[sid]
        self.assertEqual(restored.agent.session_log.path.resolve(), source.path.resolve())
        self.assertEqual(restored.fork_origin["kind"], "cli_adopted")
        self.assertEqual(self.listing()["sessions"][0]["session_id"], sid)
        result = self.client.app.state.delete_idle_sessions([sid])
        self.assertEqual(result, {"deleted": [sid], "incomplete": False})
        self.assertTrue(source.path.is_file())
        self.assertIn("terminal question", source.path.read_text())
        self.assertTrue(self.listing()["sessions"][0]["can_continue"])

    def test_changed_revision_and_missing_revision_are_rejected(self):
        self.begin()
        source = self.fixture()
        item = self.listing()["sessions"][0]
        with source.path.open("a") as handle:
            handle.write(json.dumps({"event": "exit", "reason": "normal"}) + "\n")
        self.assertEqual(self.branch(item).status_code, 409)
        response = self.client.post("/session", json={"fork_from": {"catalog_id": item["catalog_id"]}}, headers=self.headers())
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/session", headers=self.headers()).json()["sessions"], [])

    def test_crash_tail_and_unpaired_tool_call_do_not_claim_safe_fork(self):
        self.begin()
        crashed = self.fixture("crashed", close=False)
        crashed.release()
        broken = self.fixture("broken")
        with broken.path.open("ab") as handle:
            handle.write(b'{"role":')
        pending = self.fixture("pending", extra=[
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]},
        ])
        items = self.listing()["sessions"]
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertFalse(item["can_fork"])
            self.assertEqual(self.branch(item, key=item["catalog_id"]).status_code, 409)
        self.assertTrue(pending.path.exists())

    def test_symlink_log_directory_lock_and_workspace_escapes_are_rejected(self):
        self.begin()
        source = self.fixture()
        item = self.listing()["sessions"][0]
        outside = Path(self.tmp) / "outside"
        outside.mkdir()
        alias = session_log.sessions_dir() / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        (outside / "stolen.jsonl").write_bytes(source.path.read_bytes())
        link = source.path.parent / "link.jsonl"
        link.symlink_to(source.path)
        self.assertEqual(len(self.listing()["sessions"]), 1)
        source.path.unlink()
        source.path.symlink_to(outside / "stolen.jsonl")
        self.assertEqual(self.preview(item).status_code, 404)
        source.path.unlink()
        source.path.write_bytes((outside / "stolen.jsonl").read_bytes())
        lock = session_log.lock_path(source.path)
        lock.unlink()
        lock.symlink_to(outside / "protected")
        self.assertEqual(self.branch(item).status_code, 409)
        self.assertFalse((outside / "protected").exists())

    def test_malformed_tool_structures_are_readable_but_never_forkable(self):
        self.begin()
        for index, calls in enumerate(({"id": "x"}, ["invalid"], [{"id": []}], 42)):
            self.fixture(f"bad-{index}", extra=[{"role": "assistant", "content": "visible reply", "tool_calls": calls}])
        items = self.listing()["sessions"]
        self.assertEqual(len(items), 4)
        for item in items:
            self.assertFalse(item["can_fork"])
            self.assertEqual(self.preview(item).json()["messages"][-1]["content"], "visible reply")
            self.assertEqual(self.branch(item, key=item["catalog_id"]).status_code, 409)

    def test_unreadable_directory_does_not_hide_readable_sources(self):
        self.begin()
        source = self.fixture()
        bad = session_log.sessions_dir() / "unreadable"
        bad.mkdir()
        original = Path.iterdir

        def guarded(path):
            if path == bad:
                raise PermissionError("blocked")
            return original(path)

        with patch.object(Path, "iterdir", guarded):
            result = self.listing()
        self.assertEqual(len(result["sessions"]), 1)
        self.assertIn("不完整", result["warning"])
        self.assertTrue(source.path.exists())

    def test_strict_lock_errors_do_not_fall_back_to_unlocked_copy(self):
        self.begin()
        self.fixture()
        item = self.listing()["sessions"][0]
        with patch("xiaoyu.serve_catalog.session_log._try_lock", side_effect=OSError("unsupported")):
            response = self.branch(item)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/session", headers=self.headers()).json()["sessions"], [])
