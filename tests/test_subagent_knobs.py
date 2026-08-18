"""subagent 四旋钮的测试。不打网络。

capability_mode 的格语义 / isolation=worktree 的建删与 fail-open /
resume_from 的 fail-closed / MCP 继承的 server 级筛选，各卡一处。
worktree 相关用例需要 git，机器上没有就跳过。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import agents as agents_mod
from xiaoyu import worktree as worktree_mod
from xiaoyu.agent import Usage
from xiaoyu.agents import (
    AgentSpec,
    SubagentRun,
    intersect_capabilities,
    load_agent_specs,
    make_subagent_tool,
    normalize_capability,
)
from xiaoyu.config import Config
from xiaoyu.mcp import McpView, RemoteTool
from xiaoyu.providers import Registry
from xiaoyu.render import PlainSink
from xiaoyu.tools import Toolbox

from .test_agent_paths import AgentTestCase, FakeClient, call_fragment, chunk

HAS_GIT = shutil.which("git") is not None


def write_spec(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


def text_turn(text: str) -> list:
    return [chunk(content=text)]


def tool_turn(call_id: str, name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps(args))])]


# ---------- 能力档位：纯函数 ----------


class CapabilityLatticeTest(unittest.TestCase):
    def test_aliases_normalize(self):
        for raw in ("read-only", "readonly", "READ_ONLY", "readOnly", " Read-Only "):
            self.assertEqual(normalize_capability(raw), "read-only")
        self.assertEqual(normalize_capability("readWrite"), "read-write")
        self.assertEqual(normalize_capability("ALL"), "all")
        self.assertIsNone(normalize_capability("sandbox"))
        self.assertIsNone(normalize_capability(""))

    def test_intersection_lattice(self):
        #  格语义：all 单位元、read-only 吸收元、不可比降到 read-only
        self.assertIsNone(intersect_capabilities(None, None))
        self.assertEqual(intersect_capabilities("execute", None), "execute")
        self.assertEqual(intersect_capabilities("all", "read-write"), "read-write")
        self.assertEqual(intersect_capabilities("read-only", "all"), "read-only")
        self.assertEqual(intersect_capabilities("execute", "execute"), "execute")
        self.assertEqual(intersect_capabilities("read-write", "execute"), "read-only")
        self.assertEqual(intersect_capabilities("execute", "read-write"), "read-only")


# ---------- spec 解析 ----------


class SpecParsingTest(AgentTestCase):
    def _load(self):
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            return load_agent_specs(self.root)

    def test_capability_mode_without_tools(self):
        write_spec(
            self.root / ".xiaoyu" / "agents", "reader",
            'description = "d"\nsystem_prompt = "s"\ncapability_mode = "read-only"\n',
        )
        specs, problems = self._load()
        self.assertEqual(problems, [])
        self.assertEqual(specs[0].tools, ("read_file", "grep", "list_files"))
        self.assertTrue(specs[0].readonly)

    def test_capability_mode_intersects_tools(self):
        write_spec(
            self.root / ".xiaoyu" / "agents", "mixed",
            'description = "d"\nsystem_prompt = "s"\n'
            'tools = ["read_file", "write_file", "bash"]\ncapability_mode = "read-write"\n',
        )
        specs, problems = self._load()
        self.assertEqual(problems, [])
        #  bash 被 read-write 档扣掉
        self.assertEqual(specs[0].tools, ("read_file", "write_file"))

    def test_bad_knob_values_are_problems(self):
        base = self.root / ".xiaoyu" / "agents"
        write_spec(
            base, "bad_mode",
            'description = "d"\nsystem_prompt = "s"\ncapability_mode = "sandbox"\n',
        )
        write_spec(
            base, "bad_iso",
            'description = "d"\nsystem_prompt = "s"\ntools = ["grep"]\nisolation = "vm"\n',
        )
        write_spec(
            base, "empty_cut",
            'description = "d"\nsystem_prompt = "s"\n'
            'tools = ["bash"]\ncapability_mode = "read-write"\n',
        )
        write_spec(
            base, "neither",
            'description = "d"\nsystem_prompt = "s"\n',
        )
        specs, problems = self._load()
        self.assertEqual(specs, [])
        self.assertEqual(len(problems), 4)

    def test_mcp_forms(self):
        base = self.root / ".xiaoyu" / "agents"
        write_spec(
            base, "mcp_all",
            'description = "d"\nsystem_prompt = "s"\ntools = ["grep"]\nmcp = "all"\n',
        )
        write_spec(
            base, "mcp_named",
            'description = "d"\nsystem_prompt = "s"\ntools = ["grep"]\nmcp = ["github"]\n',
        )
        write_spec(
            base, "mcp_except",
            'description = "d"\nsystem_prompt = "s"\ntools = ["grep"]\n'
            'mcp_except = ["internal"]\n',
        )
        write_spec(
            base, "mcp_conflict",
            'description = "d"\nsystem_prompt = "s"\ntools = ["grep"]\n'
            'mcp = ["a"]\nmcp_except = ["b"]\n',
        )
        specs, problems = self._load()
        by_name = {spec.name: spec for spec in specs}
        self.assertEqual(by_name["mcp_all"].mcp_mode, "all")
        self.assertEqual(by_name["mcp_named"].mcp_servers, ("github",))
        self.assertEqual(by_name["mcp_except"].mcp_mode, "except")
        self.assertNotIn("mcp_conflict", by_name)
        self.assertEqual(len(problems), 1)
        #  继承了 MCP 的只读 spec 不再是"免确认只读"
        self.assertFalse(by_name["mcp_all"].readonly)

    def test_isolation_worktree_parsed(self):
        write_spec(
            self.root / ".xiaoyu" / "agents", "iso",
            'description = "d"\nsystem_prompt = "s"\ntools = ["grep"]\n'
            'isolation = "worktree"\n',
        )
        specs, problems = self._load()
        self.assertEqual(problems, [])
        self.assertEqual(specs[0].isolation, "worktree")


# ---------- 运行期旋钮（直接调 handler，子 agent 吃假 client 脚本） ----------


class KnobTestCase(AgentTestCase):
    """公共装配：make_subagent_tool 直连（不经父 agent 的模型循环）。"""

    def make_tool(self, spec: AgentSpec, script: list, runs=None, mcp_manager=None):
        self.sub_client = FakeClient(script)
        return make_subagent_tool(
            spec,
            self.config,
            Registry.for_client(self.sub_client),
            Usage(),
            PlainSink(indent="", verbose=False),
            lambda name, args: True,
            None,
            runs=runs,
            mcp_manager=mcp_manager,
        )


class CapabilityRuntimeTest(KnobTestCase):
    SPEC = AgentSpec(
        name="coder", description="d", system_prompt="工作区 {workspace}",
        tools=("read_file", "write_file", "bash"),
    )

    def test_invalid_mode_param_rejected(self):
        tool = self.make_tool(self.SPEC, [])
        result = tool.handler(task="x", capability_mode="sandbox")
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("capability_mode", result)

    def test_mode_param_narrows_tools(self):
        """收紧到 read-only 后 write_file 不在子工具箱里，写不进文件。"""
        script = [
            tool_turn("w1", "write_file", {"path": "out.txt", "content": "x"}),
            text_turn("写不了，收工"),
        ]
        tool = self.make_tool(self.SPEC, script)
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="写个文件", capability_mode="read-only")
        self.assertIn("写不了", result)
        self.assertFalse((self.root / "out.txt").exists())

    def test_sentinel_mode_param_ignored(self):
        tool = self.make_tool(self.SPEC, [text_turn("好")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="x", capability_mode="null", isolation="undefined")
        self.assertNotIn("ERROR", result)


class ResumeTest(KnobTestCase):
    SPEC = AgentSpec(
        name="doc_reader", description="d", system_prompt="工作区 {workspace}",
        tools=("read_file", "grep", "list_files"),
    )

    def _first_run(self, runs) -> str:
        tool = self.make_tool(self.SPEC, [text_turn("第一次结论")], runs=runs)
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="第一次任务")
        match = re.search(r"resume_from: ([0-9a-f]{8})", result)
        self.assertIsNotNone(match, f"结论里没有 resume 句柄：{result}")
        return match.group(1)

    def test_round_trip_inherits_transcript(self):
        runs: dict = {}
        rid = self._first_run(runs)
        self.assertIn("第一次任务", json.dumps(runs[rid].messages, ensure_ascii=False))
        tool = self.make_tool(self.SPEC, [text_turn("续上了")], runs=runs)
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="接着来", resume_from=rid)
        self.assertIn("续上了", result)
        #  新存档里新旧两个任务都在（transcript 续用而不是重开）
        new_id = re.search(r"resume_from: ([0-9a-f]{8})", result).group(1)
        dump = json.dumps(runs[new_id].messages, ensure_ascii=False)
        self.assertIn("第一次任务", dump)
        self.assertIn("接着来", dump)

    def test_unknown_id_fails_closed(self):
        tool = self.make_tool(self.SPEC, [], runs={})
        result = tool.handler(task="x", resume_from="deadbeef")
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("resume_from", result)

    def test_spec_mismatch_fails_closed(self):
        runs = {
            "cafe0123": SubagentRun(
                id="cafe0123", spec_name="别人家", model="m",
                messages=[{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
            )
        }
        tool = self.make_tool(self.SPEC, [], runs=runs)
        result = tool.handler(task="x", resume_from="cafe0123")
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("别人家", result)

    def test_oversized_transcript_fails_closed(self):
        self.config.context_limit = 100
        runs = {
            "cafe0123": SubagentRun(
                id="cafe0123", spec_name="doc_reader", model="m",
                messages=[
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": "字" * 4000},
                ],
            )
        }
        tool = self.make_tool(self.SPEC, [], runs=runs)
        result = tool.handler(task="x", resume_from="cafe0123")
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("80%", result)

    def test_rolling_eviction(self):
        runs: dict = {}
        with mock.patch.object(agents_mod, "MAX_RUNS", 2):
            for index in range(3):
                tool = self.make_tool(self.SPEC, [text_turn(f"第{index}次")], runs=runs)
                with contextlib.redirect_stdout(io.StringIO()):
                    tool.handler(task=f"任务{index}")
        self.assertEqual(len(runs), 2)


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class WorktreeIsolationTest(KnobTestCase):
    #  spec 不设默认隔离：test_dirty 用调用参数触发（顺带验 alias 容错），
    #  SPEC_ISOLATED 验 spec 级默认
    SPEC = AgentSpec(
        name="builder", description="d", system_prompt="工作区 {workspace}",
        tools=("read_file", "write_file"),
    )
    SPEC_ISOLATED = AgentSpec(
        name="builder", description="d", system_prompt="工作区 {workspace}",
        tools=("read_file", "write_file"), isolation="worktree",
    )

    def setUp(self):
        super().setUp()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-qm", "init"],
        ):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        #  worktree 基目录必须落在临时区，不碰真机配置
        patcher = mock.patch.object(worktree_mod, "user_config_dir", lambda: self.root / "cfg")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dirty_worktree_kept_and_reported(self):
        """调用参数触发隔离（spec 没设默认），顺带验大小写 alias。"""
        script = [
            tool_turn("w1", "write_file", {"path": "new.txt", "content": "hi"}),
            text_turn("写好了"),
        ]
        tool = self.make_tool(self.SPEC, script)
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="写个文件", isolation="Worktree")
        self.assertIn("改动在独立 worktree", result)
        kept = Path(re.search(r"worktree：(\S+)（", result).group(1))
        self.assertTrue((kept / "new.txt").is_file())
        #  主工作区一根毛都没动
        self.assertFalse((self.root / "new.txt").exists())

    def test_clean_worktree_removed(self):
        """spec 级默认隔离：没改动跑完就删。"""
        tool = self.make_tool(self.SPEC_ISOLATED, [text_turn("只看不改")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="看看")
        self.assertNotIn("改动在独立 worktree", result)
        worktrees_dir = self.root / "cfg" / "worktrees"
        leftovers = list(worktrees_dir.rglob("new.txt")) if worktrees_dir.is_dir() else []
        self.assertEqual(leftovers, [])
        #  目录本身也删了（只剩空的 slug 目录无所谓）
        if worktrees_dir.is_dir():
            self.assertEqual([p for p in worktrees_dir.rglob("*") if p.is_file()], [])

    def test_resume_reuses_kept_worktree(self):
        script = [
            tool_turn("w1", "write_file", {"path": "new.txt", "content": "hi"}),
            text_turn("写好了"),
        ]
        runs: dict = {}
        tool = self.make_tool(self.SPEC_ISOLATED, script, runs=runs)
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="写个文件")
        rid = re.search(r"resume_from: ([0-9a-f]{8})", result).group(1)
        kept = runs[rid].worktree
        self.assertIsNotNone(kept)
        tool = self.make_tool(self.SPEC_ISOLATED, [text_turn("还在老地方")], runs=runs)
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="继续", resume_from=rid)
        #  复用同一个 worktree：新存档还指着它
        new_id = re.search(r"resume_from: ([0-9a-f]{8})", result).group(1)
        self.assertEqual(runs[new_id].worktree, kept)

    def test_non_git_workspace_fails_open(self):
        """不在 git 仓里：告警、退回主工作区，委托照常完成。"""
        import tempfile

        with tempfile.TemporaryDirectory() as plain:
            plain_root = Path(plain).resolve()
            config = Config(
                base_url="http://unused", model="main-model",
                summary_model="cheap-model", explore_model="cheap-model",
                workspace=plain_root, auto_approve=True,
                enable_skills=False, enable_agents=False,
                enable_hooks=False, enable_plugins=False,
            )
            self.config = config
            self.root = plain_root
            tool = self.make_tool(self.SPEC_ISOLATED, [text_turn("照常干完")])
            with contextlib.redirect_stdout(io.StringIO()):
                result = tool.handler(task="干活")
            self.assertIn("照常干完", result)
            self.assertIn("worktree 隔离失败", result)


# ---------- MCP 继承 ----------


def remote(server: str, tool: str) -> RemoteTool:
    return RemoteTool(
        name=f"mcp__{server}__{tool}",
        description=f"[MCP·{server}] {tool}",
        parameters={"type": "object", "properties": {}},
        handler=lambda **kwargs: f"{server}/{tool} ok",
        check_fn=lambda: True,
        server=server,
    )


class FakeManager:
    def __init__(self, remotes):
        self._remotes = remotes

    def ready_tools(self):
        return list(self._remotes)

    def loading(self):
        return False

    def take_media(self):
        return []


class McpInheritanceTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.manager = FakeManager([remote("github", "issues"), remote("internal", "secrets")])

    def test_view_filters_by_server(self):
        named = McpView(self.manager, "named", ("github",))
        self.assertEqual([r.server for r in named.ready_tools()], ["github"])
        excluded = McpView(self.manager, "except", ("internal",))
        self.assertEqual([r.server for r in excluded.ready_tools()], ["github"])
        everything = McpView(self.manager, "all")
        self.assertEqual(len(everything.ready_tools()), 2)
        #  引用不存在的名字不报错，只是筛没了
        ghost = McpView(self.manager, "named", ("nonexistent",))
        self.assertEqual(ghost.ready_tools(), [])

    def test_restricted_toolbox_mounts_meta_tools_with_view(self):
        view = McpView(self.manager, "named", ("github",))
        box = Toolbox(self.config, only=["read_file"], mcp_view=view)
        names = box.names()
        self.assertIn("search_tool", names)
        self.assertIn("use_tool", names)
        #  use_tool 照常要审批（继承不放大权限）
        self.assertTrue(box.get("use_tool").requires_approval)
        #  经视图能调到被允许的 server
        self.assertIn("ok", box._use_tool("mcp__github__issues", {}))
        #  被筛掉的 server 调不到
        self.assertIn("ERROR", box._use_tool("mcp__internal__secrets", {}))

    def test_full_toolbox_honors_explicit_view(self):
        #  嵌入宿主的注入面：完整工具箱（only=None）显式传 view 时用它，
        #  替代（而非合并）配置发现——不落 mcp.json 也能挂宿主声明的 server
        view = McpView(self.manager, "all")
        box = Toolbox(self.config, mcp_view=view)
        self.assertIs(box._mcp, view)
        names = box.names()
        self.assertIn("search_tool", names)
        self.assertIn("bash", names)  # 完整工具箱：内置工具照常在
        #  经视图能调到 server 工具（宿主注入的清单真实可用）
        self.assertIn("ok", box._use_tool("mcp__github__issues", {}))

    def test_restricted_toolbox_without_view_has_no_mcp(self):
        box = Toolbox(self.config, only=["read_file"])
        self.assertNotIn("search_tool", box.names())
        self.assertNotIn("use_tool", box.names())


if __name__ == "__main__":
    unittest.main()
