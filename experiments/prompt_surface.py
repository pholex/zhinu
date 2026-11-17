#!/usr/bin/env python3
"""隔离捕获两个 checkout 的模型可见面并比较成本；benchmark 显式调用真实模型。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))


def render():
    # PYTHONPATH 由父进程指向待测 checkout，复用其生产 CLI 和隔离回放夹具。
    from tests.test_e2e_snapshot import SnapshotCase, Scenario
    from tests.snapshot_support import normalize_system_prompt, normalize_tool_schemas
    from xiaoyu.tokens import estimate_text, estimate_tools

    surfaces = {}
    for mode in ("default", "auto", "plan"):
        case = SnapshotCase("runTest")
        case.setUp()
        try:
            run = case.replay_run(Scenario("text-turn", "打个招呼", args=("--mode", mode)))
            first = run["captures"][0]
            system = normalize_system_prompt(first["system"], case.tmp)
            tools = json.loads(normalize_tool_schemas(first["tools"], case.tmp))
            surfaces[mode] = {
                "system": system, "tools": tools,
                "system_tokens_estimate": estimate_text(system),
                "tool_tokens_estimate": estimate_tools(tools), "tool_count": len(tools),
            }
        finally:
            case.doCleanups()
    return {"schema": 1, "surfaces": surfaces}


def compare(before, after):
    if set(before["surfaces"]) != set(after["surfaces"]):
        raise ValueError("两份报告的配置面不一致，不能直接比较")
    rows = {}
    for name, old in before["surfaces"].items():
        new = after["surfaces"][name]
        old_tools = {t["function"]["name"]: t for t in old["tools"]}
        new_tools = {t["function"]["name"]: t for t in new["tools"]}
        rows[name] = {
            "system_changed": old["system"] != new["system"],
            "added_tools": sorted(new_tools.keys() - old_tools.keys()),
            "removed_tools": sorted(old_tools.keys() - new_tools.keys()),
            "changed_tools": sorted(k for k in new_tools.keys() & old_tools.keys() if new_tools[k] != old_tools[k]),
            **{f"{key}_delta": new[key] - old[key]
               for key in ("system_tokens_estimate", "tool_tokens_estimate", "tool_count")},
        }
    return {"schema": 1, "surfaces": rows, "measurement": "local_estimate_not_billed_usage"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    capture = sub.add_parser("capture", help="离线捕获 default/auto/plan")
    capture.add_argument("--repo", type=Path, required=True)
    capture.add_argument("--output", type=Path, required=True)
    diff = sub.add_parser("diff")
    diff.add_argument("before", type=Path)
    diff.add_argument("after", type=Path)
    bench = sub.add_parser("benchmark", help="真实模型：同一组行为任务，每个 checkout 单独运行")
    bench.add_argument("--repo", type=Path, required=True)
    bench.add_argument("--output-dir", type=Path, required=True)
    bench.add_argument("--model", required=True)
    bench.add_argument("--repeat", type=int, default=3)
    sub.add_parser("_render")
    args = parser.parse_args()
    if args.command == "_render":
        report = render()
    elif args.command == "diff":
        report = compare(read(args.before), read(args.after))
    else:
        repo = args.repo.resolve()
        if not (repo / "xiaoyu" / "__init__.py").is_file():
            parser.error("--repo 必须是源码 checkout")
        env = {**os.environ, "PYTHONPATH": str(repo), "PYTHONNOUSERSITE": "1"}
        if args.command == "capture":
            completed = subprocess.run([sys.executable, "-P", str(Path(__file__).resolve()), "_render"],
                                       cwd=repo, env=env, capture_output=True, check=True)
            report = json.loads(completed.stdout.decode("utf-8", errors="replace"))
            report["revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode("utf-8", errors="replace").strip()
            report["dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repo))
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0
        if not 1 <= args.repeat <= 10:
            parser.error("--repeat 必须为 1–10")
        args.output_dir.mkdir(parents=True, exist_ok=False)
        command = [sys.executable, "-P", "-m", "xiaoyu.evals.runner", "--model", args.model,
                   "--repeat", str(args.repeat)]
        for case in ("fix_and_test", "targeted_edit", "readonly_answer", "multi_file_rename"):
            command += ["--case", case]
        manifest = {"revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo).decode("utf-8", errors="replace").strip(),
                    "dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=repo)),
                    "model": args.model, "repeat": args.repeat, "command": command}
        (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return subprocess.run(command, cwd=args.output_dir.resolve(), env=env).returncode
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
