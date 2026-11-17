#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["harbor==0.23.0", "PyYAML>=6.0"]
# ///
"""小羽的 harbor 横评入口（terminal-bench 系数据集）。

子命令：
    run        构建/上传 wheel，生成 harbor 配置，跑一个 job
    list       列出 runs/ 下所有 job 的汇总表
    show       一个 job 的逐任务结果
    task       一个 job 里某个任务的细节（判分输出、日志尾部）
    compare    两个 job 逐任务对比
    rm         删掉若干 job

跑法见 README.md。
"""

from __future__ import annotations

import argparse

from reporter import STATUSES, cmd_compare, cmd_list, cmd_rm, cmd_show, cmd_task
from runner import DEFAULT_CONCURRENCY, DEFAULT_DATASET, cmd_run, parse_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="跑一个 benchmark job")
    run.add_argument("--wheel", help="已构建的 xiaoyu wheel；省略则在仓库根 uv build 一个")
    run.add_argument("--extras", help="装包附带的 extras，如 bedrock（走 Bedrock 时要）")
    run.add_argument("--dataset", default=DEFAULT_DATASET, help=f"默认 {DEFAULT_DATASET}")
    run.add_argument(
        "--model",
        default=None,
        help="provider/model；默认 gateway/<宿主 XIAOYU_MODEL>（没配就 deepseek-flash）",
    )
    run.add_argument("--tasks", type=parse_csv, default=[], help="逗号分隔的任务名子集")
    run.add_argument("--n-tasks", type=int, default=None, help="只跑前 N 个任务（在 --tasks 之后生效）")
    run.add_argument("--trials", type=int, default=1, help="每个任务跑几次")
    run.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    run.add_argument("--timeout-multiplier", type=float, default=1.0, help="任务超时倍率")
    run.add_argument("--max-retries", type=int, default=0, help="harbor 对异常 trial 的重试次数")
    run.add_argument("--budget-tokens", type=int, default=None, help="小羽的 --budget-tokens（软预算）")
    run.add_argument("--effort", default=None, help="小羽的 --effort（推理深度）")
    run.add_argument(
        "--env",
        action="append",
        metavar="KEY=VALUE",
        help="追加/覆盖注入容器的小羽环境变量，可重复（模板见 config_template.yaml）",
    )
    run.add_argument(
        "--allow-host",
        action="append",
        metavar="HOST",
        help="任务用 allowlist 网络策略时额外放行的主机，可重复（模型端点已按 provider 自动加）",
    )
    run.add_argument("--prices", help="额外的单价表 JSON（覆盖内置与本机表）")
    run.add_argument("--env-file", help=".env 路径；默认依次找当前目录、本目录、仓库根")
    run.add_argument("--job-name")
    run.add_argument("--dry-run", action="store_true", help="只打印生成的 harbor 配置")

    sub.add_parser("list", help="列出所有 job 的汇总")

    show = sub.add_parser("show", help="一个 job 的逐任务结果")
    show.add_argument("job_name")
    show.add_argument("--status", choices=STATUSES)

    task = sub.add_parser("task", help="一个任务的细节")
    task.add_argument("job_name")
    task.add_argument("task_name")
    task.add_argument("--tail", type=int, default=0, help="打印 agent 事件流末 N 行")

    compare = sub.add_parser("compare", help="两个 job 逐任务对比")
    compare.add_argument("job_a")
    compare.add_argument("job_b")
    compare.add_argument("-v", "--verbose", action="store_true")

    rm = sub.add_parser("rm", help="删掉 job")
    rm.add_argument("job_names", nargs="+")
    rm.add_argument("-y", "--yes", action="store_true", help="不询问")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "run":
        if args.model is None:
            #  默认模型要在 .env 读进来之后才知道，cmd_run 里再定
            args.model = ""
        return cmd_run(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "show":
        return cmd_show(args)
    if args.cmd == "task":
        return cmd_task(args)
    if args.cmd == "compare":
        return cmd_compare(args)
    if args.cmd == "rm":
        return cmd_rm(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
