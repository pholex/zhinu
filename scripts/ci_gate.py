#!/usr/bin/env python3
"""release.yml 的门：同一提交的 CI 已经跑过就不再跑一遍，只等结果。

    python scripts/ci_gate.py --repo pholex/zhinu --sha <40 位> --workflow ci.yml

读 GitHub API（经 `gh api`，Actions 里自带 GH_TOKEN）里该 workflow 在这个提交上的运行：

- 有一次 success        → 打印 `result=verified`，退出 0（release.yml 跳过对应测试 job）
- 全部跑完、没有 success → 打印 `result=failed`，退出 1（gate 红，发布链整个停下，版本号烧掉）
- 还在跑                → 轮询等（默认最多 30 分钟）；超时按 missing 处理
- 一次运行都没有        → 等一小会（push 之后 run 的创建有延迟），仍没有就 `result=missing`，
                          退出 0（release.yml 自己跑一遍——只改文档的发版就走这条）

为什么等而不是直接跳过：发版是 `git push --atomic origin main vX.Y.Z` 一次推走，ci.yml 由
main 的 push 触发、release.yml 由 tag 触发，两个同时起跑；release 这边等 ci 的结论，比
三平台矩阵再跑一遍省 10 分钟。

结果写到 $GITHUB_OUTPUT（有就写）和 stdout。判定（decide）与取数（fetch_runs）分开，判定可测。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

VERIFIED, FAILED, PENDING, MISSING = "verified", "failed", "pending", "missing"
_BAD = {"failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"}


def decide(runs: list[dict]) -> tuple[str, dict | None]:
    """一组运行 → (结论, 依据的那次运行)。success 优先；其次还在跑的；其次失败的。"""
    completed = [r for r in runs if r.get("status") == "completed"]
    for run in completed:
        if run.get("conclusion") == "success":
            return VERIFIED, run
    active = [r for r in runs if r.get("status") in ("queued", "in_progress", "waiting", "requested", "pending")]
    if active:
        return PENDING, active[0]
    bad = [r for r in completed if r.get("conclusion") in _BAD]
    if bad:
        return FAILED, bad[0]
    if completed:
        #  completed 但 conclusion 是 skipped / neutral 之类：没有可信的绿，自己跑
        return MISSING, completed[0]
    return MISSING, None


def fetch_runs(repo: str, workflow: str, sha: str) -> list[dict]:
    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/workflows/{workflow}/runs?head_sha={sha}&per_page=20",
         "--jq", ".workflow_runs"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[-300:] or f"gh 退出码 {proc.returncode}")
    runs = json.loads(proc.stdout or "[]")
    return runs if isinstance(runs, list) else []


def wait(repo: str, workflow: str, sha: str, *, wait_seconds: float, interval: float,
         missing_grace: float, sleep=time.sleep, now=time.monotonic, fetch=fetch_runs) -> tuple[str, dict | None]:
    start = now()
    while True:
        try:
            runs = fetch(repo, workflow, sha)
        except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
            print(f"::warning::查 {workflow} 运行记录失败（{exc}），按没有记录处理", file=sys.stderr)
            runs = []
        result, run = decide(runs)
        elapsed = now() - start
        if result in (VERIFIED, FAILED):
            return result, run
        if result == MISSING and elapsed >= missing_grace:
            return MISSING, run
        if result == PENDING and elapsed >= wait_seconds:
            print(f"::warning::等 {workflow} 超过 {int(wait_seconds)} 秒仍未结束，改为自己跑", file=sys.stderr)
            return MISSING, run
        print(f"{workflow}@{sha[:7]}：{result}（已等 {int(elapsed)} 秒）", flush=True)
        sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--sha", required=True, help="提交 SHA（release.yml 里是 $GITHUB_SHA）")
    parser.add_argument("--workflow", required=True, help="ci.yml / sdk.yml")
    parser.add_argument("--wait", type=float, default=1800.0, help="最多等多少秒（默认 30 分钟）")
    parser.add_argument("--interval", type=float, default=30.0, help="轮询间隔（秒）")
    parser.add_argument("--missing-grace", type=float, default=120.0,
                        help="一次运行都没有时再等多少秒才判 missing（push 后 run 的创建有延迟）")
    args = parser.parse_args(argv)

    result, run = wait(args.repo, args.workflow, args.sha, wait_seconds=args.wait,
                       interval=args.interval, missing_grace=args.missing_grace)
    url = (run or {}).get("html_url", "")
    print(f"result={result}" + (f" {url}" if url else ""))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"result={result}\n")
    if result == FAILED:
        print(f"::error::{args.workflow} 在 {args.sha[:7]} 上是红的：{url}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
