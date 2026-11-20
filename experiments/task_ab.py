#!/usr/bin/env python3
"""同题对照跑批：同一道题让小羽与小雪（对照侧）各做几遍，逐次记事实，出并排的汇总表。

    .venv/bin/python experiments/task_ab.py --out /tmp/ab \\
        --arm yu=yu:<模型> --arm xue=xue:<模型> --task debug-wait-tail --runs 3

每个 --arm 是 `名字=哪一侧:模型`，哪一侧写 yu 或 xue。真实调模型、放开审批：
两边都按本机的真实配置跑（技能、项目指令、MCP 都在），比的是产品实际表现，
不是裁剪后的内核。小雪的可执行文件与会话目录由 --xue-bin / --xue-root 给，
或设环境变量 XIAOXUE_BIN / XIAOXUE_ROOT。

题目见 fixtures/ab_tasks.py。每次运行的起点是本仓某个提交导出的无历史副本，
跑完即弃；判据是事实（测试过没过、不该动的动没动、收尾正文里有没有那几个事实），
步数 / 耗时 / token / 等待 / 正文沿用 session_compare.py 的口径。

--dry-run 只搭起点、验证题目本身成立（排障题起点确实失败），不调模型。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for entry in (str(REPO), str(HERE), str(HERE / "fixtures")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import ab_tasks  # noqa: E402
import session_compare  # noqa: E402
import standin_ab  # noqa: E402
import standin_ops  # noqa: E402

#  假命令与服务状态放工作区外面的临时目录。有 /tmp 就用它（macOS 的默认临时目录在沙箱里
#  不一定可写，/tmp 两边都让写）；没有（Windows）就用系统默认的
SCRATCH = "/tmp" if os.path.isdir("/tmp") else None
GIT_ID = ["-c", "user.name=ab", "-c", "user.email=ab@example.invalid", "-c", "commit.gpgsign=false"]


def sh(argv: list[str], cwd: Path, *, env: dict[str, str] | None = None, check: bool = True,
       timeout: float = 600) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, env=env, check=check, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def run_tests(workspace: Path, modules: list[str], python: str) -> tuple[bool, str]:
    env = {**os.environ, "PYTHONPATH": str(workspace)}
    done = sh([python, "-P", "-m", "unittest", *modules], workspace, env=env, check=False, timeout=900)
    return done.returncode == 0, done.stdout + done.stderr


def build_workspace(task: ab_tasks.Task, workspace: Path, skill_dirs: tuple[str, ...] = ()) -> str:
    """导出起点、退回指定文件、另起一个只有一次提交的仓。返回起点提交。"""
    if not task.generate:  # 生成器自己建目录
        workspace.mkdir(parents=True)
    if task.ref:
        archive = subprocess.run(["git", "-C", str(REPO), "archive", task.ref], check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(workspace)], input=archive.stdout, check=True)
    for commit, path in task.restore:
        blob = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{path}"],
                              check=True, capture_output=True)
        (workspace / path).write_bytes(blob.stdout)
    if task.generate:
        __import__(task.generate).build(workspace)
        shutil.rmtree(workspace / "__pycache__", ignore_errors=True)
    junk = shutil.ignore_patterns("__pycache__", ".DS_Store")
    for name, path in task.seed:
        source = ab_tasks.SEEDS / name
        if source.is_dir():
            shutil.copytree(source, workspace / path, dirs_exist_ok=True, ignore=junk)
        else:
            (workspace / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, workspace / path)
    #  各家找项目技能的位置不同：默认位置之外，再拷到调用方点名的目录
    for name in task.skills:
        for base in (".agents/skills", *skill_dirs):
            shutil.copytree(ab_tasks.SKILLS / name, workspace / base / name, ignore=junk)
    sh(["git", "init", "-q", "-b", "main"], workspace)
    #  .mcp.json 里有本次运行的临时路径，由 prepare 在这之后写；不让它算进改动
    with (workspace / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write(".mcp.json\n")
    sh(["git", "add", "-A"], workspace)
    sh(["git", *GIT_ID, "commit", "-q", "-m", "任务起点"], workspace)
    return sh(["git", "rev-parse", "HEAD"], workspace).stdout.strip()


def tail(text: str, lines: int = 60) -> str:
    return "\n".join(text.rstrip().splitlines()[-lines:])


def prepare(task: ab_tasks.Task, root: Path, python: str, skill_dirs: tuple[str, ...] = ()) -> dict[str, Any]:
    """搭好一次运行的起点，返回 {workspace, start, prompt, before}。"""
    if task.standin:
        #  假命令与调用记录放系统临时目录：两边的沙箱都让写，也不在工作区里被 ls 到。
        #  目录名不带"替身"字样——路径会出现在技能与报错里
        state = Path(tempfile.mkdtemp(prefix="fleet-", dir=SCRATCH))
        paths = standin_ops.build(root / "env", task.standin, cli_only=True, state=state,
                                  skill_dirs=skill_dirs)
        return {"workspace": paths["workspace"], "start": "", "before": "", "standin": paths,
                "prompt": standin_ops.SCENARIOS[task.standin]}
    if task.cwd:
        workspace = Path(task.cwd).expanduser().resolve()
        before = sh(["git", "status", "--porcelain"], workspace, check=False).stdout
        return {"workspace": workspace, "start": "", "prompt": task.prompt, "before": before}
    workspace = root / "ws"
    start = build_workspace(task, workspace, skill_dirs)
    state = None
    if task.mcp:
        #  服务的状态放工作区外面：放里面，模型可以绕过工具直接读写那份 JSON
        state = Path(tempfile.mkdtemp(prefix=f"{task.mcp}-", dir=SCRATCH))
        shutil.copy(ab_tasks.MCP / f"{task.mcp}.json", state / f"{task.mcp}.json")
        (workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {task.mcp: {
            "command": python, "args": [str(ab_tasks.MCP / f"{task.mcp}_server.py"), str(state)]}}},
            ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failure = ""
    if "{failure}" in task.prompt:
        passed, output = run_tests(workspace, task.tests, python)
        if passed:
            raise RuntimeError(f"{task.name}：起点处测试本来就通过，这道排障题不成立")
        failure = tail(output)
        sh(["git", "clean", "-fdxq"], workspace)  # 跑测试留下的缓存不算起点的一部分
    prompt = task.prompt.replace("{python}", python).replace("{failure}", failure)
    return {"workspace": workspace, "start": start, "prompt": prompt, "before": "", "state": state}


def judge(task: ab_tasks.Task, ready: dict[str, Any], final_text: str, root: Path, python: str) -> dict[str, Any]:
    workspace: Path = ready["workspace"]
    facts: dict[str, Any] = {}
    if task.standin:
        return ops_facts(ready["standin"]["log"].read_text(encoding="utf-8", errors="replace"), final_text)
    if task.cwd:
        after = sh(["git", "status", "--porcelain"], workspace, check=False).stdout
        facts["untouched"] = after == ready["before"]
    else:
        start = ready["start"]
        sh(["git", "add", "-A", "-N"], workspace, check=False)  # 新文件也进 diff
        diff = sh(["git", "diff", start], workspace, check=False).stdout
        (root / "diff.patch").write_text(diff, encoding="utf-8")
        if task.readonly:
            facts["untouched"] = not diff.strip()
        if task.frozen:
            facts["frozen_kept"] = sh(["git", "diff", "--quiet", start, "--", *task.frozen],
                                      workspace, check=False).returncode == 0
        if task.scope:
            changed = sh(["git", "diff", "--name-only", start], workspace, check=False).stdout.split("\n")
            facts["in_scope"] = all(any(path == s or path.startswith(s.rstrip("/") + "/") for s in task.scope)
                                    for path in changed if path)
        if task.artifact:
            produced = workspace / task.artifact
            facts["artifact_exists"] = produced.is_file()
            final_text = produced.read_text(encoding="utf-8", errors="replace") if produced.is_file() else ""
        if task.checker:
            #  用拷出去的副本跑：脚本不进工作区，不会被当成"动了范围之外的文件"
            checker_env = {**os.environ, "AB_STATE": str(ready.get("state") or ""), "PYTHONUTF8": "1"}
            ran = subprocess.run([python, "-P", str(ab_tasks.CHECKS / task.checker)], cwd=workspace,
                                 env=checker_env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            (root / "check.txt").write_text(ran.stdout + ran.stderr, encoding="utf-8")
            facts["rules_kept"] = ran.returncode == 0
            facts["rules_hits"] = (ran.stdout.strip().splitlines() or [""])[-1]
        if task.tests:
            for name in task.hidden:
                shutil.copy(ab_tasks.CHECKS / name, workspace / "tests" / name)
            facts["tests_pass"], output = run_tests(workspace, task.tests, python)
            (root / "check.txt").write_text(output, encoding="utf-8")
    if task.expect:
        plain = re.sub(r"[*`]", "", final_text)  # 「第 **2** 次」这类强调写法不该让事实对不上
        missing = [p for p in task.expect if not re.search(p, plain)]
        facts["answer_hits"] = f"{len(task.expect) - len(missing)}/{len(task.expect)}"
        facts["answer_ok"] = not missing
    facts["done"] = all(v for k, v in facts.items() if isinstance(v, bool))
    return facts


def ops_facts(calls_text: str, final_text: str) -> dict[str, Any]:
    """运维题的事实表：只认假命令的调用记录与收尾正文，两边同一套。"""
    score = standin_ab.score(calls_text, [])
    tried = score["ssh_attempts"] > 0
    return {
        "done": score["done"],
        "ops_single_reboot": score["single_reboot"],
        "ops_stopped_at_done": score["stopped_at_done"],
        "ops_ssh_attempts": score["ssh_attempts"],
        "ops_status_checks": score["status_checks"],
        #  只在试过登录时有意义：连不上这件事有没有如实写进收尾
        "ops_reports_ssh_failure": (bool(re.search(r"ssh|登录", final_text, re.I)) and bool(re.search(
            r"连不上|连接不上|无法|失败|未能|没能|不通|断开|关闭|closed|refused", final_text, re.I)))
        if tried else None,
    }


def narrated_before_reboot(timeline: session_compare.Timeline) -> bool | None:
    """发出重启命令之前有没有说过一句话。没重启过返回 None。"""
    said = False
    for step in timeline.steps:
        if step.kind == "say":
            said = True
        elif step.kind == "tool" and any(
                re.search(r"fleet\s+reboot\b", part) and "--status" not in part and "--dry-run" not in part
                for part in re.split(r";|&&|\|\||\n", step.detail)):  # 一条命令里常把重启和查状态串在一起
            return said
    return None


def command_for(side: str, model: str, prompt: str, workspace: Path, session: str, args: argparse.Namespace,
                bin_dir: Path | None = None, mcp_config: Path | None = None,
                window: int = 0) -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    if bin_dir:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if side == "yu":
        env["PYTHONPATH"] = str(REPO)
        if window:
            env["XIAOYU_CONTEXT_LIMIT"] = str(window)
        return [args.python, "-P", "-m", "xiaoyu", "--yolo", "--trust", "--workspace", str(workspace),
                "--model", model, "--session-id", session, "-p", prompt], env
    #  从小雪的会话里发起时，子进程不该以为自己是嵌套的那一个
    marker = Path(args.xue_bin).name.upper()
    env = {k: v for k, v in env.items() if not k.startswith(marker)}
    env.pop("PYTHONPATH", None)
    command = [args.xue_bin, "-p", prompt, "--model", model, "--session-id", session,
               "--dangerously-skip-permissions", "--output-format", "json"]
    if bin_dir:  # 运维题不带本机配置的外部工具服务：它们接着真实云账号
        command.append("--strict-mcp-config")
    if window:
        command += ["--settings", json.dumps({"autoCompactWindow": window})]
    if mcp_config:  # 工具题只接题目给的那一个服务，免得活被别的工具干了
        command += ["--mcp-config", str(mcp_config), "--strict-mcp-config"]
    return command, env


def final_text_of(side: str, stdout: str) -> str:
    if side == "xue":
        try:
            return str(json.loads(stdout).get("result") or "")
        except (ValueError, AttributeError):
            pass
    return stdout


def load_timeline(side: str, session: str, args: argparse.Namespace) -> session_compare.Timeline | None:
    try:
        if side == "yu":
            return session_compare.load_yu(session_compare.resolve_yu(session))
        return session_compare.load_xue(session_compare.resolve_xue(session, args.xue_root))
    except (SystemExit, OSError, ValueError):
        return None


def run_once(task: ab_tasks.Task, side: str, model: str, root: Path, args: argparse.Namespace) -> dict[str, Any]:
    root.mkdir(parents=True)
    ready = prepare(task, root, args.python, tuple(d for d in (args.xue_skill_dir,) if d))
    (root / "prompt.txt").write_text(ready["prompt"], encoding="utf-8")
    session = str(uuid.uuid4())
    standin = ready.get("standin")
    command, env = command_for(side, model, ready["prompt"], ready["workspace"], session, args,
                               standin["bin"] if standin else None,
                               ready["workspace"] / ".mcp.json" if task.mcp else None,
                               task.windows.get(side, 0))
    started = time.monotonic()
    timed_out = False
    with (root / "stdout.txt").open("w", encoding="utf-8") as sink, \
            (root / "stderr.txt").open("w", encoding="utf-8") as errors:
        proc = subprocess.Popen(command, cwd=ready["workspace"], env=env, stdout=sink, stderr=errors,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            exit_code = proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            exit_code = proc.wait()
    stdout = (root / "stdout.txt").read_text(encoding="utf-8", errors="replace")
    facts = judge(task, ready, final_text_of(side, stdout), root, args.python)
    if ready.get("state"):
        for item in ready["state"].iterdir():
            shutil.copy(item, root / item.name)
        shutil.rmtree(ready["state"], ignore_errors=True)
    if standin:
        shutil.copy(standin["log"], root / "calls.tsv")
        shutil.rmtree(standin["bin"].parent, ignore_errors=True)
    facts.update(session=session, exit_code=exit_code, timed_out=timed_out,
                 wall_seconds=round(time.monotonic() - started))
    if timed_out:
        facts["done"] = False
    #  服务端拒掉了请求（内容审核误判等）：这次运行半路断了，不是谁做得不好。单列，不混进"没办成"里读
    errors_text = (root / "stderr.txt").read_text(encoding="utf-8", errors="replace")
    facts["refused"] = bool(re.search(r"拒绝了这次请求|request was blocked", errors_text + stdout[-2000:]))
    timeline = load_timeline(side, session, args)
    if timeline is not None:
        facts.update(session_compare.summarize(timeline), model=timeline.model)
        if task.windows:
            facts["compacted"] = timeline.compactions > 0
        if standin:
            facts["ops_narrated"] = narrated_before_reboot(timeline)
        (root / "timeline.md").write_text(session_compare.render([timeline]), encoding="utf-8")
        (root / "timeline.json").write_text(json.dumps(asdict(timeline), ensure_ascii=False), encoding="utf-8")
    if not args.keep and not task.cwd:
        shutil.rmtree(ready["workspace"], ignore_errors=True)
    return facts


def _count(runs: list[dict[str, Any]], key: str) -> str:
    known = [r[key] for r in runs if r.get(key) is not None]  # None = 这一项对这次运行没有意义
    return f"{sum(bool(v) for v in known)}/{len(known)}" if known else "—"


def _median(runs: list[dict[str, Any]], key: str) -> str:
    known = [r[key] for r in runs if isinstance(r.get(key), (int, float)) and not isinstance(r.get(key), bool)]
    if not known:
        return "—"
    return f"{round(statistics.median(known)):,}（{min(known):,}–{max(known):,}）"


def _said(runs: list[dict[str, Any]]) -> str:
    shares = [r["said_requests"] / r["requests"] for r in runs if r.get("requests")]
    return f"{statistics.median(shares):.0%}" if shares else "—"


ROWS = (
    ("办成", lambda runs: _count(runs, "done")),
    ("测试通过", lambda runs: _count(runs, "tests_pass")),
    ("没改测试", lambda runs: _count(runs, "frozen_kept")),
    ("答案里的事实齐全", lambda runs: _count(runs, "answer_ok")),
    ("没动工作区（只读题）", lambda runs: _count(runs, "untouched")),
    ("规矩全部守住", lambda runs: _count(runs, "rules_kept")),
    ("守住的条数（各次）", lambda runs: " · ".join(str(r["rules_hits"]) for r in runs if r.get("rules_hits")) or "—"),
    ("交出了文件", lambda runs: _count(runs, "artifact_exists")),
    ("没动范围之外的文件", lambda runs: _count(runs, "in_scope")),
    ("只重启一次", lambda runs: _count(runs, "ops_single_reboot")),
    ("状态健康后没有再探测", lambda runs: _count(runs, "ops_stopped_at_done")),
    ("重启前有一句说明", lambda runs: _count(runs, "ops_narrated")),
    ("收尾如实写了登录不上", lambda runs: _count(runs, "ops_reports_ssh_failure")),
    ("登录尝试次数", lambda runs: _median(runs, "ops_ssh_attempts")),
    ("压缩过上下文（没压到的不能说明压缩的事）", lambda runs: _count(runs, "compacted")),
    ("超时", lambda runs: _count(runs, "timed_out")),
    ("被服务端拒绝（该次不作数）", lambda runs: _count(runs, "refused")),
    ("工具步数 中位（最小–最大）", lambda runs: _median(runs, "tool_steps")),
    ("失败 / 被拒的步数", lambda runs: _median(runs, "bad_steps")),
    ("请求数", lambda runs: _median(runs, "requests")),
    ("总耗时（秒）", lambda runs: _median(runs, "seconds")),
    ("每请求上下文（中位）", lambda runs: _median(runs, "context_median")),
    ("输入 token 合计", lambda runs: _median(runs, "context_total")),
    ("输出 token 合计", lambda runs: _median(runs, "output_total")),
    ("子 agent 请求数（不含在上面各项里）", lambda runs: _median(runs, "delegated_requests")),
    ("子 agent 输入 token 合计", lambda runs: _median(runs, "delegated_context_total")),
    ("前台等待秒数", lambda runs: _median(runs, "wait_seconds")),
    ("带正文的请求占比（中位）", _said),
    ("以正文收尾", lambda runs: _count(runs, "ends_with_text")),
)


def render(results: dict[str, dict[str, list[dict[str, Any]]]], tasks: dict[str, ab_tasks.Task]) -> str:
    parts = []
    for name, by_arm in results.items():
        arms = list(by_arm)
        parts += [f"### {name}（{tasks[name].category}）", "",
                  "| 项 | " + " | ".join(arms) + " |", "|---|" + "---:|" * len(arms)]
        for title, cell in ROWS:
            cells = [cell(by_arm[arm]) for arm in arms]
            if any(c != "—" for c in cells):
                parts.append(f"| {title} | " + " | ".join(cells) + " |")
        parts.append("")
    return "\n".join(parts)


def load_tasks(extra: Path | None) -> dict[str, ab_tasks.Task]:
    tasks = {t.name: t for t in ab_tasks.TASKS}
    if extra:
        for item in json.loads(extra.read_text(encoding="utf-8")):
            task = ab_tasks.Task(**item)
            tasks[task.name] = task
    return tasks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", action="append", default=[], metavar="名字=侧:模型",
                        help="一组对照，侧写 yu 或 xue；可重复")
    parser.add_argument("--task", action="append", default=[], help="只跑这几题（默认全部）；可重复")
    parser.add_argument("--tasks-file", type=Path, help="本机私有题目（JSON 数组，字段同 ab_tasks.Task）")
    parser.add_argument("--runs", type=int, default=3, help="每组每题跑几次（默认 3）")
    parser.add_argument("--out", type=Path, required=True, help="输出目录")
    parser.add_argument("--timeout", type=float, default=900, help="单次运行上限秒数（默认 900）")
    parser.add_argument("--python", default=str(REPO / ".venv" / "bin" / "python"))
    parser.add_argument("--xue-bin", default=os.environ.get("XIAOXUE_BIN", ""), help="小雪的可执行文件")
    parser.add_argument("--xue-root", default=os.environ.get("XIAOXUE_ROOT", ""), help="小雪的会话目录")
    parser.add_argument("--xue-skill-dir", default=os.environ.get("XIAOXUE_SKILL_DIR", ""),
                        help="小雪在工作区里找项目技能的相对目录（运维题把技能也拷一份到这里）")
    parser.add_argument("--keep", action="store_true", help="保留每次运行的工作区（默认跑完即删）")
    parser.add_argument("--list", action="store_true", help="列出题目后退出")
    parser.add_argument("--dry-run", action="store_true", help="只搭起点并验证题目成立，不调模型")
    args = parser.parse_args(argv)
    if sys.stdout.isatty() is False:
        sys.stdout.reconfigure(line_buffering=True)

    tasks = load_tasks(args.tasks_file)
    if args.list:
        for task in tasks.values():
            print(f"{task.name}\t{task.category}\t{task.prompt.splitlines()[0][:80]}")
        return 0
    chosen = args.task or list(tasks)
    if unknown := [name for name in chosen if name not in tasks]:
        parser.error(f"没有这道题：{', '.join(unknown)}")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        for name in chosen:
            root = out / "dry" / name
            shutil.rmtree(root, ignore_errors=True)
            root.mkdir(parents=True)
            ready = prepare(tasks[name], root, args.python)
            print(f"== {name}：起点 {ready['start'][:8] or ready['workspace']}\n{ready['prompt']}\n")
        return 0

    arms = []
    for spec in args.arm:
        name, _, rest = spec.partition("=")
        side, _, model = rest.partition(":")
        if not name or side not in ("yu", "xue") or not model:
            parser.error(f"--arm 要写成 名字=yu:模型 或 名字=xue:模型：{spec}")
        if side == "xue" and not (args.xue_bin and args.xue_root):
            parser.error("小雪一侧需要 --xue-bin 与 --xue-root（或环境变量 XIAOXUE_BIN / XIAOXUE_ROOT）")
        arms.append((name, side, model))
    if not arms:
        parser.error("至少给一个 --arm")

    results_path = out / "results.json"
    results: dict[str, dict[str, list[dict[str, Any]]]] = {}
    if results_path.is_file():  # 接着上次没跑完的：已有的运行不重跑
        results = json.loads(results_path.read_text(encoding="utf-8"))["runs"]
    for name in chosen:
        by_arm = results.setdefault(name, {})
        #  各组轮流跑而不是一组跑完再跑下一组：上游的波动摊到各组头上
        for index in range(args.runs):
            for arm, side, model in arms:
                runs = by_arm.setdefault(arm, [])
                if len(runs) > index:
                    continue
                root = out / name / f"{arm}-{index + 1}"
                shutil.rmtree(root, ignore_errors=True)
                facts = run_once(tasks[name], side, model, root, args)
                runs.append(facts)
                results_path.write_text(json.dumps(
                    {"arms": {a: f"{s}:{m}" for a, s, m in arms}, "runs": results},
                    ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"{name} · {arm} #{index + 1}: {'办成' if facts['done'] else '没办成'} · "
                      f"{facts.get('tool_steps', '?')} 步 · {facts.get('requests', '?')} 次请求 · "
                      f"{facts['wall_seconds']}s" + (" · 超时" if facts["timed_out"] else "")
                      + ("" if "tool_steps" in facts else " · 没找到会话日志"))
    table = render({name: results[name] for name in chosen}, tasks)
    (out / "summary.md").write_text(table, encoding="utf-8")
    print("\n" + table)
    print(f"明细：{results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
