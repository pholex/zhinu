"""Real serve + scripted model, entirely in a temporary workspace, for UI checks."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def isolated_environment(root: Path) -> None:
    # The fixture process must not inherit real model keys or local integrations.
    keep = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    os.environ.clear()
    os.environ.update(keep)
    for name in ("home", "config", "projects"):
        (root / name).mkdir()
    os.environ.update({"HOME": str(root / "home"), "USERPROFILE": str(root / "home"),
                       "XDG_CONFIG_HOME": str(root / "config"), "APPDATA": str(root / "config"),
                       "XIAOYU_SCRIPTED_SCRIPTS": str(root / "script.txt"),
                       "XIAOYU_SUGGESTIONS": "0", "XIAOYU_ENABLE_ASKPASS": "0"})
    for name in ("SKILLS", "PLUGINS", "MCP", "HOOKS", "AGENTS", "EXPLORE", "WEB_SEARCH",
                 "X_SEARCH", "DEEP_RESEARCH", "BROWSER"):
        os.environ[f"XIAOYU_ENABLE_{name}"] = "0"
    os.chdir(root / "projects")


def fixture_app(root: Path, port: int, ui_origin: str, token: str = "", *, announce: bool = True):
    import httpx
    from xiaoyu.serve import ServeConfig, create_app

    cfg = ServeConfig(root=root / "projects", port=port, token=token, model="lab-scripted",
                      mode="default", approval="ask", approval_timeout=86400,
                      sandbox=True, mcp=False, cors_extensions=False,
                      cors_origins=(ui_origin,), state_dir=root / "state")
    app = create_app(cfg)
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=f"http://127.0.0.1:{port}", headers=headers) as client:
                async def request(method, path, **kwargs):
                    response = await client.request(method, path, **kwargs)
                    response.raise_for_status()
                    return response.json()

                async def wait(sid, predicate):
                    for _ in range(300):
                        state = await request("GET", f"/session/{sid}/status")
                        if predicate(state):
                            return state
                        await asyncio.sleep(.02)
                    raise RuntimeError(f"Fixture did not reach the expected state: {sid}")

                async def start(project, prompt, script, *, setup=None, **options):
                    folder = root / "projects" / project
                    folder.mkdir(exist_ok=True)
                    (root / "script.txt").write_text(script, encoding="utf-8")
                    info = await request("POST", "/session", json={"workspace": str(folder), **options})
                    sid = info["session_id"]
                    if setup is not None:
                        setup(app.state.sessions[sid].agent)
                    await request("POST", f"/session/{sid}/prompt_async", json={"text": prompt})
                    return sid

                def tool(name, arguments):
                    return "tool_call: " + json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False) + "\n"

                def install_weave(agent):
                    from xiaoyu.agents import AgentSpec, RunStore
                    from xiaoyu.providers import Registry
                    from xiaoyu.qixiang import make_qixiang_tool
                    from xiaoyu.douqiao import make_douqiao_tool
                    from xiaoyu.scripted import ScriptedClient, parse_scripts

                    answer = "本项检查已完成。" + "已核对任务、执行记录与交付文件，结果可追溯。" * 12
                    child = ScriptedClient(parse_scripts("---\n".join(
                        f"text: 第 {index + 1} 项：{answer}\n" for index in range(64))))
                    agent.config.qixiang_concurrency = 1
                    runs = RunStore(locate=lambda: Path(str(agent.session_log.path) + ".runs"))
                    specs = [AgentSpec(name="reviewer", description="交付检查", system_prompt="核对交付材料。",
                                       tools=("read_file",))]
                    # Direct commands enumerate the same mounted specs and archives
                    # used by the tool, including the fixture's settled seed batch.
                    agent._mounted_agent_specs = specs
                    agent.subagent_runs = runs
                    agent.toolbox.register(make_qixiang_tool(
                        specs, agent.config, Registry.for_client(child),
                        agent.usage, agent.sink, agent.approver, agent.permissions, runs,
                        stop_requested=agent.interrupt_requested))
                    # A separate queue keeps comparative work independent of the
                    # batch fixture. Two candidates plus a judge form one round.
                    contest_answers = [
                        "方案一：逐项核对交付记录，先完成最小范围再扩展。" + answer,
                        "方案二：先列验收标准，再按标准检查执行记录与交付文件。" + answer,
                        "两项方案均保留检查记录；方案二先明确标准，更便于复核。\n胜者：#2\n理由：验收步骤更明确。\n败者亮点：方案一的最小范围便于先行试用。",
                    ]
                    contest_script = "---\n".join(
                        "text: " + json.dumps(value, ensure_ascii=False) + "\n"
                        for _ in range(20) for value in contest_answers)
                    contest_client = ScriptedClient(parse_scripts(contest_script))
                    agent.toolbox.register(make_douqiao_tool(
                        specs, agent.config, Registry.for_client(contest_client),
                        agent.usage, agent.sink, agent.approver, agent.permissions, runs,
                        stop_requested=agent.interrupt_requested))
                    if shutil.which("git"):
                        from xiaoyu.chenshu import ChenshuRuntime, make_chenshu_tools

                        workspace = agent.config.workspace
                        (workspace / "README.md").write_text("# 协作看板脚本示例\n", encoding="utf-8")
                        git_env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
                        for args in (["init", "-q"], ["add", "README.md"],
                                     ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                      "commit", "-qm", "fixture"]):
                            subprocess.run(["git", *args], cwd=workspace, env=git_env, check=True,
                                           capture_output=True)
                        member_script = (
                            tool("chenshu_finding", {"kind": "improve", "title": "明确交付检查顺序",
                                                     "body": "先核对任务范围，再检查交付记录与待处理项。"})
                            + "---\n" + tool("chenshu_mission", {"mission_id": "M1",
                                "status": "completed", "note": "已核对示例范围；后续实现尚未开始。"})
                            + "---\ntext: 已完成只读范围调研。建议后续实现从交付检查清单开始，先保留每一步的来源和状态。\n")
                        runtime = ChenshuRuntime(agent.config, Registry.for_client(ScriptedClient(parse_scripts(member_script))),
                                                 agent.usage, agent.sink, agent.permissions, notify=agent.notify,
                                                 hooks=lambda: agent.hook_engine, stop_requested=agent.interrupt_requested)
                        agent.chenshu = runtime
                        for item in make_chenshu_tools(runtime):
                            agent.toolbox.register(item)

                fleet_script = ""
                if shutil.which("git"):
                    fleet_script = (tool("chenshu_init", {}) + "---\n"
                        + tool("chenshu_plan", {"missions": [
                            {"title": "核对交付检查范围", "kind": "survey"},
                            {"title": "补充交付检查清单", "kind": "build", "scope": ["checklist/"], "deps": ["M1"]}]})
                        + "---\n" + tool("chenshu_spawn", {"name": "scope-reader", "kind": "worker", "mission_id": "M1"})
                        + "---\n")

                done = await start("release-notes", "整理本次工作台实验的交付记录。",
                                   tool("write_file", {"path": "delivery.md", "content": "# 工作台验证\n\n已完成会话与成果检查。\n"})
                                   + "---\n" + tool("qixiang", {"spec": "reviewer", "prompt_template": "检查{{item}}",
                                                                             "items": ["交付记录", "验收范围"]})
                                   + "---\n" + fleet_script
                                   + "text: 已生成 delivery.md，记录工作台的首轮验证范围。\nusage: {\"prompt_tokens\":1200,\"completion_tokens\":180}\n",
                                   setup=install_weave)
                state = await wait(done, lambda s: bool(s["pending_approvals"]))
                await request("POST", f"/session/{done}/permissions", json={
                    "request_id": state["pending_approvals"][0]["request_id"], "decision": "allow"})
                if fleet_script:
                    state = await wait(done, lambda s: any(item["tool"] == "chenshu_init" for item in s["pending_approvals"]))
                    approval_request = next(item for item in state["pending_approvals"] if item["tool"] == "chenshu_init")
                    await request("POST", f"/session/{done}/permissions", json={
                        "request_id": approval_request["request_id"], "decision": "allow"})
                await wait(done, lambda s: not s["busy"])
                if fleet_script:
                    runtime = app.state.sessions[done].agent.chenshu
                    for thread in list(runtime._threads.values()):
                        await asyncio.to_thread(thread.join, 5)
                        if thread.is_alive():
                            raise RuntimeError("Fixture fleet did not settle")

                from xiaoyu.session_log import SessionLog
                source = SessionLog.create("lab-scripted", str(root / "projects/release-notes"),
                                           session_id="desktop-example")
                source.append({"role": "user", "content": "整理一份可以在羽案里接着处理的终端任务。"})
                source.append({"role": "assistant", "content": "已整理任务背景；下一步请检查交付范围。"})
                source.close()

                question = await start("research-notes", "先确认报告格式，再继续整理调研材料。",
                                       tool("ask_user", {"questions": [{"question": "调研报告需要哪种交付格式？", "options": ["Markdown 文档", "CSV 表格"]}]})
                                       + "---\ntext: 等你确认交付格式后继续。\n---\ntext: 已按你提交的答案继续整理。\n", questions={})
                await wait(question, lambda s: not s["busy"])

                failed = await start("research-notes", "检查材料来源是否可访问。", "error: fixture source unavailable\n")
                await wait(failed, lambda s: not s["busy"])

                approval = await start("release-notes", "把验证摘要保存到 approval-note.md。",
                                       tool("write_file", {"path": "approval-note.md", "content": "此文件仅在审批通过后生成。"})
                                       + "---\ntext: 这次调用已处理，详情请查看执行记录。\n")
                await wait(approval, lambda s: bool(s["pending_approvals"]))
                # Each newly created session reads this script into its own model queue.
                (root / "script.txt").write_text(
                    tool("write_file", {"path": "task-result.md", "content": "# 任务成果\n\n脚本模型验证已完成。\n"})
                    + "---\ntext: 文件处理完成。下面确认后续输出偏好。\n"
                    + "---\n" + tool("ask_user", {"questions": [{"question": "下一步输出哪种格式？", "options": ["Markdown", "纯文本"]}]})
                    + "---\ntext: 已接纳答案，验证完成。\n"
                    + "---\ntext: 已完成继续任务。\n"
                    + "---\ntext: 已接纳你的补充要求。\n", encoding="utf-8")
                if announce:
                    print("脚本模型验证已就绪：2 个项目、4 个会话；不调用真实模型。", flush=True)
                    print(f"打开 {ui_origin}/?fixture=1 并连接 http://127.0.0.1:{port}", flush=True)
                try:
                    yield
                finally:
                    for sid, session in list(app.state.sessions.items()):
                        if session.busy:
                            await request("POST", f"/session/{sid}/abort", json={})
                            await wait(sid, lambda s: not s["busy"])

    app.router.lifespan_context = lifespan
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18420)
    parser.add_argument("--ui-origin", default="http://127.0.0.1:8421")
    parser.add_argument("--token", default="", help="Optional fixture-only token for authentication checks")
    args = parser.parse_args()
    import uvicorn

    with tempfile.TemporaryDirectory(prefix="xiaoyu-workbench-fixture-") as tmp:
        root = Path(tmp).resolve()
        isolated_environment(root)
        app = fixture_app(root, args.port, args.ui_origin, args.token)
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
