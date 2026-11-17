"""读 runs/ 下的 harbor 结果（JobResult / TrialResult JSON），出 list / show / task / compare / rm。"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from harbor.models.job.result import JobResult
from harbor.models.trial.result import TrialResult

RUNS_DIR = Path(__file__).resolve().parent / "runs"
STATUSES = ("pass", "partial", "fail", "timeout", "error", "no-reward")


@dataclass
class LoadedJob:
    summary: JobResult
    trials: list[TrialResult]
    job_dir: Path

    @property
    def name(self) -> str:
        return self.job_dir.name


def load_job(job_dir: Path) -> LoadedJob:
    result = job_dir / "result.json"
    if not result.is_file():
        raise FileNotFoundError(f"不是 harbor 结果目录（缺 result.json）：{job_dir}")
    summary = JobResult.model_validate_json(result.read_text(encoding="utf-8"))
    trials: list[TrialResult] = []
    for child in sorted(job_dir.iterdir()):
        trial_result = child / "result.json"
        if child.is_dir() and trial_result.is_file():
            trials.append(TrialResult.model_validate_json(trial_result.read_text(encoding="utf-8")))
    return LoadedJob(summary=summary, trials=trials, job_dir=job_dir)


# ---------------------------------------------------------------- 单 trial 指标


def task_name(trial: TrialResult) -> str:
    """注册表任务名带数据集前缀（terminal-bench/build-pov-ray），表里只显示短名。"""
    return trial.task_id.get_name().rsplit("/", 1)[-1]


def trial_reward(trial: TrialResult) -> float | None:
    rewards = trial.verifier_result.rewards if trial.verifier_result else None
    if not rewards:
        return None
    value = rewards.get("reward", next(iter(rewards.values())))
    return float(value)


def trial_error(trial: TrialResult) -> tuple[str, str] | None:
    if trial.exception_info is None:
        return None
    return trial.exception_info.exception_type, trial.exception_info.exception_message


def trial_status(trial: TrialResult) -> str:
    """pass / partial / fail / timeout / error / no-reward。

    reward 优先于异常：agent 超时或退出码非零时 harbor 照样跑 verifier，
    判分过了就算过——分数是硬指标，异常只是过程记录。
    """
    reward = trial_reward(trial)
    if reward is not None and reward > 0:
        return "pass" if reward >= 1.0 else "partial"
    error = trial_error(trial)
    if error is not None:
        return "timeout" if "timeout" in error[0].lower() else "error"
    return "no-reward" if reward is None else "fail"


def _seconds(start, end) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def trial_duration(trial: TrialResult) -> float | None:
    """整个 trial（建容器 + 装 agent + 跑 + 判分）。"""
    return _seconds(trial.started_at, trial.finished_at)


def agent_duration(trial: TrialResult) -> float | None:
    """只算 agent 跑任务那一段。"""
    timing = trial.agent_execution
    if timing is None:
        return None
    return _seconds(timing.started_at, timing.finished_at)


@dataclass(frozen=True)
class Tokens:
    input: int | None
    output: int | None
    cost: float | None


def trial_tokens(trial: TrialResult) -> Tokens:
    n_in, _cache, n_out, cost = trial.compute_token_cost_totals()
    return Tokens(n_in, n_out, cost)


def trial_turns(trial: TrialResult, job_dir: Path) -> int | None:
    """模型调用次数：优先 agent 写进 metadata 的 turns，其次数 ATIF 轨迹里的 agent step。"""
    context = trial.agent_result
    if context and context.metadata and context.metadata.get("turns") is not None:
        return int(context.metadata["turns"])
    trajectory = job_dir / trial.trial_name / "agent" / "trajectory.json"
    if trajectory.is_file():
        try:
            data = json.loads(trajectory.read_text(encoding="utf-8"))
        except ValueError:
            return None
        steps = data.get("steps") if isinstance(data, dict) else None
        if isinstance(steps, list):
            return sum(1 for s in steps if isinstance(s, dict) and s.get("source") == "agent")
    return None


# ---------------------------------------------------------------- job 聚合


def status_counts(trials: list[TrialResult]) -> dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for trial in trials:
        counts[trial_status(trial)] += 1
    return counts


def _sum(values) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def job_tokens(job: LoadedJob) -> Tokens:
    tokens = [trial_tokens(t) for t in job.trials]
    return Tokens(
        input=int(_sum(t.input for t in tokens) or 0),
        output=int(_sum(t.output for t in tokens) or 0),
        cost=_sum(t.cost for t in tokens),
    )


def job_turns(job: LoadedJob) -> int:
    return sum(trial_turns(t, job.job_dir) or 0 for t in job.trials)


def job_model(job: LoadedJob) -> str:
    for trial in job.trials:
        info = trial.agent_info
        if info and info.model_info and info.model_info.name:
            return info.model_info.name
    return "?"


def job_version(job: LoadedJob) -> str:
    for trial in job.trials:
        if trial.agent_info and trial.agent_info.version:
            return trial.agent_info.version
    return "?"


# ---------------------------------------------------------------- 格式化


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "-"
    if sec < 60:
        return f"{sec:.0f}s"
    if sec < 3600:
        return f"{sec / 60:.1f}m"
    return f"{sec / 3600:.1f}h"


def fmt_tokens(n: int | None) -> str:
    if not n:
        return "-"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def fmt_cost(usd: float | None) -> str:
    if usd is None:
        return "-"
    return f"${usd:.2f}" if usd >= 0.01 else f"${usd:.4f}"


def fmt_rate(counts: dict[str, int], total: int) -> str:
    return f"{100 * counts['pass'] / total:.1f}%" if total else "-"


def breakdown(counts: dict[str, int]) -> str:
    return f"{counts['pass']}/{counts['fail']}/{counts['error']}/{counts['timeout']}"


def list_jobs() -> list[LoadedJob]:
    if not RUNS_DIR.is_dir():
        return []
    jobs: list[LoadedJob] = []
    for child in sorted(RUNS_DIR.iterdir()):
        if child.is_dir() and (child / "result.json").is_file():
            jobs.append(load_job(child))
    return jobs


# ---------------------------------------------------------------- 子命令


def cmd_list(args: argparse.Namespace) -> int:
    jobs = list_jobs()
    if not jobs:
        print(f"{RUNS_DIR} 下没有跑完的 job")
        return 0
    header = (
        f"{'job_name':<36} {'version':<8} {'model':<22} {'rate':>6} {'agent':>7} {'compute':>8} "
        f"{'in':>7} {'out':>7} {'turns':>6} {'cost':>8} {'pass/fail/err/tout':>18}"
    )
    print(header)
    print("-" * len(header))
    for job in jobs:
        counts = status_counts(job.trials)
        tokens = job_tokens(job)
        name = job.name if job.summary.finished_at else f"{job.name} (running)"
        print(
            f"{name[:36]:<36} {job_version(job):<8} {job_model(job)[:22]:<22} "
            f"{fmt_rate(counts, len(job.trials)):>6} "
            f"{fmt_duration(_sum(agent_duration(t) for t in job.trials)):>7} "
            f"{fmt_duration(_sum(trial_duration(t) for t in job.trials)):>8} "
            f"{fmt_tokens(tokens.input):>7} {fmt_tokens(tokens.output):>7} "
            f"{fmt_tokens(job_turns(job)):>6} {fmt_cost(tokens.cost):>8} "
            f"{breakdown(counts):>18}"
        )
    print()
    print("agent=各 trial agent 执行时长之和；compute=各 trial 全程（建容器+装+跑+判分）之和；")
    print("两者都把并发展开，与宿主并发数无关。cost 按单价表估算，没单价的模型记 '-'。")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    job = load_job(RUNS_DIR / args.job_name)
    counts = status_counts(job.trials)
    total = len(job.trials)
    tokens = job_tokens(job)
    print(f"Job:       {job.name}")
    print(f"Agent:     xiaoyu {job_version(job)}  model={job_model(job)}")
    print(f"Started:   {job.summary.started_at}")
    print(f"Trials:    {total}  " + "  ".join(f"{k}={v}" for k, v in counts.items()))
    print(f"Pass rate: {fmt_rate(counts, total)}")
    print(f"Agent:     {fmt_duration(_sum(agent_duration(t) for t in job.trials))}   "
          f"Compute: {fmt_duration(_sum(trial_duration(t) for t in job.trials))}")
    print(f"Tokens:    in={fmt_tokens(tokens.input)}  out={fmt_tokens(tokens.output)}  "
          f"turns={fmt_tokens(job_turns(job))}  cost={fmt_cost(tokens.cost)}")
    print()
    header = (
        f"{'task':<40} {'status':<9} {'reward':>6} {'agent':>7} {'in':>7} {'out':>7} "
        f"{'turns':>5} {'cost':>8}  error"
    )
    print(header)
    print("-" * len(header))
    for trial in sorted(job.trials, key=task_name):
        status = trial_status(trial)
        if args.status and status != args.status:
            continue
        reward = trial_reward(trial)
        error = trial_error(trial)
        err = ""
        if error is not None:
            first = (error[1] or "").splitlines()[0] if error[1] else ""
            err = f"{error[0]}: {first}" if first else error[0]
            if len(err) > 60:
                err = err[:57] + "..."
        tk = trial_tokens(trial)
        turns = trial_turns(trial, job.job_dir)
        print(
            f"{task_name(trial)[:40]:<40} {status:<9} "
            f"{(f'{reward:.2f}' if reward is not None else '-'):>6} "
            f"{fmt_duration(agent_duration(trial)):>7} "
            f"{fmt_tokens(tk.input):>7} {fmt_tokens(tk.output):>7} "
            f"{(str(turns) if turns is not None else '-'):>5} {fmt_cost(tk.cost):>8}  {err}"
        )
    return 0


def _tail(path: Path, n: int) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


def cmd_task(args: argparse.Namespace) -> int:
    job_dir = RUNS_DIR / args.job_name
    job = load_job(job_dir)
    wanted = args.task_name.rsplit("/", 1)[-1]
    matches = [t for t in job.trials if task_name(t) == wanted]
    if not matches:
        names = sorted({task_name(t) for t in job.trials})
        print(f"{args.job_name} 里没有任务 '{args.task_name}'。有：{', '.join(names[:12])}", file=sys.stderr)
        return 1
    for trial in matches:
        trial_dir = job_dir / trial.trial_name
        tk = trial_tokens(trial)
        print(f"=== {trial.trial_name} ===")
        print(f"Status:   {trial_status(trial)}   reward={trial_reward(trial)}")
        print(f"Agent:    {fmt_duration(agent_duration(trial))}   trial={fmt_duration(trial_duration(trial))}")
        print(f"Tokens:   in={fmt_tokens(tk.input)} out={fmt_tokens(tk.output)} "
              f"turns={trial_turns(trial, job_dir)} cost={fmt_cost(tk.cost)}")
        if trial.agent_result and trial.agent_result.metadata:
            meta = trial.agent_result.metadata
            keys = ("usage_source", "cost_source", "unpriced_models", "xiaoyu_error", "background_tasks_terminated")
            shown = {k: meta[k] for k in keys if k in meta}
            if shown:
                print(f"Meta:     {json.dumps(shown, ensure_ascii=False)}")
        error = trial_error(trial)
        if error is not None:
            print(f"Error:    {error[0]}")
            for line in (error[1] or "").splitlines()[:8]:
                print(f"          {line}")
        verifier_dir = trial_dir / "verifier"
        if verifier_dir.is_dir():
            for log in sorted(verifier_dir.glob("*.txt"))[:2]:
                lines = _tail(log, 15)
                if lines:
                    print(f"--- verifier/{log.name}（末 15 行）")
                    for line in lines:
                        print(f"    {line}")
        agent_log = trial_dir / "agent" / "xiaoyu.jsonl"
        if agent_log.is_file():
            print(f"Agent log: {agent_log} ({agent_log.stat().st_size:,} bytes)")
            if args.tail:
                print(f"--- 末 {args.tail} 行")
                for line in _tail(agent_log, args.tail):
                    print(line[:400])
        stderr_log = trial_dir / "agent" / "xiaoyu.stderr.txt"
        if stderr_log.is_file() and stderr_log.stat().st_size:
            print("--- stderr（末 10 行）")
            for line in _tail(stderr_log, 10):
                print(f"    {line}")
        print(f"Artifacts: {trial_dir}")
        print()
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    job_a = load_job(RUNS_DIR / args.job_a)
    job_b = load_job(RUNS_DIR / args.job_b)
    by_a = {task_name(t): t for t in job_a.trials}
    by_b = {task_name(t): t for t in job_b.trials}
    common = sorted(set(by_a) & set(by_b))
    ca, cb = status_counts(job_a.trials), status_counts(job_b.trials)
    na, nb = len(job_a.trials), len(job_b.trials)
    ta, tb = job_tokens(job_a), job_tokens(job_b)

    print(f"A: {job_a.name}  (xiaoyu {job_version(job_a)}, {job_model(job_a)})")
    print(f"B: {job_b.name}  (xiaoyu {job_version(job_b)}, {job_model(job_b)})")
    print()
    print(f"{'metric':<14} {'A':>10} {'B':>10}")
    print("-" * 36)
    rows = [
        ("trials", str(na), str(nb)),
        ("pass", str(ca["pass"]), str(cb["pass"])),
        ("fail", str(ca["fail"]), str(cb["fail"])),
        ("timeout", str(ca["timeout"]), str(cb["timeout"])),
        ("error", str(ca["error"]), str(cb["error"])),
        ("pass rate", fmt_rate(ca, na), fmt_rate(cb, nb)),
        ("tokens in", fmt_tokens(ta.input), fmt_tokens(tb.input)),
        ("tokens out", fmt_tokens(ta.output), fmt_tokens(tb.output)),
        ("turns", fmt_tokens(job_turns(job_a)), fmt_tokens(job_turns(job_b))),
        ("cost", fmt_cost(ta.cost), fmt_cost(tb.cost)),
        ("agent time", fmt_duration(_sum(agent_duration(t) for t in job_a.trials)),
         fmt_duration(_sum(agent_duration(t) for t in job_b.trials))),
        ("compute", fmt_duration(_sum(trial_duration(t) for t in job_a.trials)),
         fmt_duration(_sum(trial_duration(t) for t in job_b.trials))),
    ]
    for label, a, b in rows:
        print(f"{label:<14} {a:>10} {b:>10}")

    only_a = sorted(set(by_a) - set(by_b))
    only_b = sorted(set(by_b) - set(by_a))
    if only_a or only_b:
        print()
        if only_a:
            print(f"只在 A ({len(only_a)}): {', '.join(only_a)}")
        if only_b:
            print(f"只在 B ({len(only_b)}): {', '.join(only_b)}")

    both_pass = [n for n in common if trial_status(by_a[n]) == "pass" and trial_status(by_b[n]) == "pass"]
    a_only = [n for n in common if trial_status(by_a[n]) == "pass" and trial_status(by_b[n]) != "pass"]
    b_only = [n for n in common if trial_status(by_a[n]) != "pass" and trial_status(by_b[n]) == "pass"]
    neither = [n for n in common if trial_status(by_a[n]) != "pass" and trial_status(by_b[n]) != "pass"]
    print()
    print(f"共同任务 {len(common)}：都过 {len(both_pass)}，只 A 过 {len(a_only)}，只 B 过 {len(b_only)}，都没过 {len(neither)}")
    if args.verbose:
        for label, names, other in (("只 A 过", a_only, by_b), ("只 B 过", b_only, by_a)):
            if names:
                print(f"\n{label}：")
                for name in names:
                    print(f"  {name:<40} 对方={trial_status(other[name])}")
    return 0


def cmd_rm(args: argparse.Namespace) -> int:
    runs = RUNS_DIR.resolve()
    targets: list[Path] = []
    for name in args.job_names:
        target = (RUNS_DIR / name).resolve()
        if runs not in target.parents:
            print(f"拒绝删除 runs/ 之外的路径：{name}", file=sys.stderr)
            return 2
        if not target.is_dir():
            print(f"不是 run 目录：{target}", file=sys.stderr)
            return 1
        targets.append(target)
    for target in targets:
        size_kb = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) // 1024
        print(f"  {target.name}  ({size_kb:,} KB)")
    if not args.yes:
        answer = input(f"删除这 {len(targets)} 个 run？[y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("已取消")
            return 1
    for target in targets:
        shutil.rmtree(target)
        print(f"已删除 {target.name}")
    return 0
