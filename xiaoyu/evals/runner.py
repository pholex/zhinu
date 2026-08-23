"""eval 执行器。

跑法：
    .venv/bin/xiaoyu-eval                      # 跑全部 case
    .venv/bin/xiaoyu-eval --case targeted_edit # 只跑一个
    .venv/bin/xiaoyu-eval --model deepseek-v4-pro --repeat 3

结果同时打到终端并存到当前目录 xiaoyu-eval-results/*.json，方便比较"改 prompt / 换模型前后"的差异。

测量纪律（比"跑出一个数"更重要的三条）：
- **基础设施故障不计入通过率**：鉴权/额度/限流/网络（errors.classify 的
  auth/quota/rate_limit/transient）说明的是端点状态，不是模型能力——把它算成
  FAIL 会让一次网络抖动看起来像"这个模型不会写代码"。这类运行标记为不可测
  （measurable=false），单独计数，退出码 3（区别于真失败的 1）。
- **排名差异要过抖动带才算数**：LLM 输出是随机变量。单次运行（--repeat 1）
  的模型间差异一律标注"未证实"；重复运行时，两个模型的通过数差不超过双方
  抖动 case（同一 case 时过时不过）之和的，标注"抖动带内，不构成区分"。
  没有这条，横向对比表就是在把噪声当结论卖。
- **结果自带来源指纹**（provenance）：版本、端点、超时、重复次数、平台。
  不带配置的数字连和它自己的下一次 run 都不可比。
"""

from __future__ import annotations

import argparse
import contextlib
import os
import io
import json
import platform
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from xiaoyu import __version__, errors, ui
from xiaoyu.agent import Agent
from xiaoyu.config import Config, MissingConfig, load_dotenv
from xiaoyu.tools import Toolbox

from . import cases as case_module
from . import models as model_registry
from .harness import Case, Context, snapshot

#  结果落在当前工作目录（pip/pipx 安装后包目录不可写，不能往包内写）。
RESULTS_DIR = Path.cwd() / "xiaoyu-eval-results"

#  横向扫模型时不能让某个卡死的模型拖垮整轮；抽成常量让 provenance 引用同一个值。
REQUEST_TIMEOUT = 180.0

#  这些分类说明的是端点/账号状态，不是模型能力——算进通过率会把一次网络抖动
#  记成"这个模型不会干活"。context_overflow 与 fatal 刻意不在内：窗口用爆和
#  跑挂都是能力相关的真实结局。
INFRA_KINDS = frozenset({"auth", "quota", "rate_limit", "transient"})


def run_case(case: Case, model: str, base_url: str | None, verbose: bool) -> dict:
    """跑一个 case，返回结构化结果。"""
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix=f"xiaoyu-eval-{case.name}-") as tmp:
        workspace = Path(tmp).resolve()
        case.setup(workspace)
        #  触发准确率 case：把隔离技能铺进独立目录，经 XIAOYU_SKILLS_DIR 加载
        #  （只认这个目录，不混机器上真装的技能）。放在 workspace 之外，免得
        #  被 setup 的文件快照/文件类判据看见
        skills_dir: str | None = None
        if case.enable_skills and case.skills:
            sroot = Path(tmp).parent / f"{Path(tmp).name}-skills"
            for sname, body in case.skills.items():
                (sroot / sname).mkdir(parents=True, exist_ok=True)
                (sroot / sname / "SKILL.md").write_text(body, encoding="utf-8")
            skills_dir = str(sroot)
        before = snapshot(workspace)

        config = Config.from_env(
            workspace=workspace,
            model=model,
            base_url=base_url,
            #  eval 必须无人值守，否则确认提示会卡死。
            auto_approve=True,
            #  eval 环境要确定：不能受跑分机器上装了什么技能/插件影响。
            #  触发准确率 case 例外——它自带隔离技能（见上）
            enable_skills=case.enable_skills,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
            enable_mcp=False,
            #  联网搜索结果随时间变，eval 里必须关掉
            enable_web_search=False,
            max_iterations=case.max_iterations,
            request_timeout=REQUEST_TIMEOUT,
            #  eval 是无人值守 + 全放行的，必须挡住"往系统 Python 里 pip install"。
            #  真实发生过：某个模型跑 pytest 失败后 pip install 到了系统 site-packages。
            extra_env={
                "PIP_REQUIRE_VIRTUALENV": "true",
                "PIP_NO_INPUT": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        #  技能在 turn 开始（_refresh_skills）重扫，所以 env 要贯穿整个 send，
        #  用完在 finally 还原，绝不泄漏到下一个 case
        prev_skills_env = os.environ.get("XIAOYU_SKILLS_DIR")
        if skills_dir:
            os.environ["XIAOYU_SKILLS_DIR"] = skills_dir
        agent = Agent(config, Toolbox(config))

        buffer = io.StringIO()
        error: str | None = None
        infra_kind: str | None = None
        try:
            with contextlib.redirect_stdout(buffer):
                agent.send(case.prompt)
        except Exception as exc:  # noqa: BLE001 - 跑挂了也要记录成一次失败
            error = f"{type(exc).__name__}: {exc}"
            #  逃出 agent 内部重试的鉴权/额度/限流/网络故障是端点状态不是模型
            #  能力，标记为不可测——绝不产出一个"悄悄降级"的 FAIL。
            verdict = errors.classify(exc)
            if verdict.kind in INFRA_KINDS:
                infra_kind = verdict.kind
        finally:
            if skills_dir:
                if prev_skills_env is None:
                    os.environ.pop("XIAOYU_SKILLS_DIR", None)
                else:
                    os.environ["XIAOYU_SKILLS_DIR"] = prev_skills_env

        transcript = buffer.getvalue()
        if verbose:
            print(ui.secondary(transcript.rstrip()))

        ctx = Context(
            workspace=workspace,
            before=before,
            trace=agent.trace,
            transcript=transcript,
            error=error,
        )

        results = []
        for label, check in case.checks:
            if infra_kind:
                results.append(
                    {"check": label, "passed": False, "detail": f"基础设施故障（{infra_kind}），无法测量"}
                )
                continue
            if error:
                results.append({"check": label, "passed": False, "detail": "本次运行异常终止"})
                continue
            try:
                passed, detail = check(ctx)
            except Exception as exc:  # noqa: BLE001 - 检查本身炸了算失败
                passed, detail = False, f"检查异常：{type(exc).__name__}: {exc}"
            results.append({"check": label, "passed": bool(passed), "detail": detail})

        ok = all(item["passed"] for item in results) and not error
        #  失败的 case 必须留下现场，否则临时目录一删就没法诊断了。
        artifacts = None if ok else {"transcript": transcript, "files": ctx.after()}

    reported_usage = agent.usage.prompt_tokens > 0
    return {
        "case": case.name,
        "model": model,
        "description": case.description,
        "passed": ok,
        #  不可测 ≠ 失败：measurable=False 的运行不进通过率，单独计数呈现
        "measurable": infra_kind is None,
        "infra": infra_kind,
        "checks": results,
        "error": error,
        "tool_calls": [entry["tool"] for entry in agent.trace],
        "tool_errors": sum(1 for entry in agent.trace if not entry["ok"]),
        "model_calls": agent.usage.turns,
        "prompt_tokens": agent.usage.prompt_tokens,
        "completion_tokens": agent.usage.completion_tokens,
        #  走 Mantle / Responses API 的模型不回 usage。这时候 token 是 0，
        #  算出来的成本会是 $0 —— 那是假的，必须标成"未知"而不是"免费"。
        "usage_reported": reported_usage,
        "usage_by_model": {
            name: {"calls": calls, "in": prompt, "out": completion}
            for name, (calls, prompt, completion) in agent.usage.snapshot().items()
        },
        "cost": (
            model_registry.cost(model, agent.usage.prompt_tokens, agent.usage.completion_tokens)
            if reported_usage
            else None
        ),
        "seconds": round(time.monotonic() - started, 1),
        "artifacts": artifacts,
    }


def summarize_by_model(results: list[dict]) -> list[dict]:
    """把逐 case 结果按模型聚合，用于横向对比。

    排序：先按 case 通过数降序，再按成本升序 —— 先看能不能干活，再看多少钱。
    不可测的运行（基础设施故障）不进 runs/passed，落在 infra 计数里；
    flaky 记"同一 case 重复运行时过时不过"的 case 数——它是判断两个模型的
    通过数差是否只是抖动的尺子。
    """
    grouped: dict[str, dict] = {}
    #  (model, case) → [每次可测运行是否通过]，供 flaky 判定
    outcomes: dict[tuple[str, str], list[bool]] = {}
    for item in results:
        row = grouped.setdefault(
            item["model"],
            {
                "model": item["model"],
                "runs": 0,
                "passed": 0,
                "infra": 0,
                "flaky": 0,
                "checks_total": 0,
                "checks_passed": 0,
                "tool_errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost": 0.0,
                "has_cost": False,
                "seconds": 0.0,
                "errors": [],
            },
        )
        if not item.get("measurable", True):
            row["infra"] += 1
            row["seconds"] += item["seconds"]
            if item["error"]:
                row["errors"].append(f"{item['case']}: [未测] {item['error'][:80]}")
            continue
        row["runs"] += 1
        row["passed"] += 1 if item["passed"] else 0
        outcomes.setdefault((item["model"], item["case"]), []).append(bool(item["passed"]))
        row["checks_total"] += len(item["checks"])
        row["checks_passed"] += sum(1 for check in item["checks"] if check["passed"])
        row["tool_errors"] += item["tool_errors"]
        row["prompt_tokens"] += item["prompt_tokens"]
        row["completion_tokens"] += item["completion_tokens"]
        row["seconds"] += item["seconds"]
        if item["cost"] is not None:
            row["cost"] += item["cost"]
            row["has_cost"] = True
        if item["error"]:
            row["errors"].append(f"{item['case']}: {item['error'][:80]}")

    for (model, _case), passes in outcomes.items():
        if len(passes) > 1 and 0 < sum(passes) < len(passes):
            grouped[model]["flaky"] += 1

    rows = list(grouped.values())
    rows.sort(key=lambda row: (-row["passed"], row["cost"] if row["has_cost"] else 1e9))
    return rows


def discrimination_note(rows: list[dict], repeat: int) -> str | None:
    """横向对比的区分度判语：把"这张表能不能读出结论"说出来，而不是让读者
    把噪声当结论。返回 None = 没有要提醒的。

    三种情况（按优先级）：
    - 所有模型通过数相同 → 本轮 case 无区分度；
    - 单次运行且排名有差异 → 差异未证实（LLM 输出是随机变量）；
    - 重复运行且头两名的通过数差 ≤ 双方抖动 case 之和 → 差异在抖动带内。
    """
    if len(rows) < 2:
        return None
    if all(row["passed"] == rows[0]["passed"] and row["runs"] == rows[0]["runs"] for row in rows):
        return "本轮 case 对这批模型无区分度（通过数全部相同）——需要更难/更多的 case"
    if repeat < 2:
        return "单次运行：排名差异未证实（LLM 输出是随机变量，--repeat ≥3 才可比）"
    top, second = rows[0], rows[1]
    gap = top["passed"] - second["passed"]
    band = top["flaky"] + second["flaky"]
    if gap <= band:
        return (
            f"头两名通过数差 {gap} ≤ 双方抖动带 {band}"
            f"（{top['model']} 抖 {top['flaky']}、{second['model']} 抖 {second['flaky']}）"
            "——不构成区分，加大 --repeat 或补区分度更强的 case"
        )
    return None


def print_matrix(results: list[dict], repeat: int = 1) -> None:
    rows = summarize_by_model(results)
    if len(rows) < 2:
        return

    print("\n" + ui.heading("横向对比") + ui.secondary("（先看能不能干活，再看多少钱）"))
    header = (
        f"  {'模型':<26}{'case':>7}{'检查项':>9}{'抖动':>5}{'未测':>5}"
        f"{'工具错':>7}{'in tok':>9}{'out':>7}{'成本':>10}{'耗时':>8}"
    )
    print(ui.secondary(header))
    for row in rows:
        cost_text = model_registry.format_cost(row["cost"] if row["has_cost"] else None)
        line = (
            f"  {row['model']:<26}"
            f"{row['passed']}/{row['runs']:<5}"
            f"{row['checks_passed']}/{row['checks_total']:<7}"
            f"{row['flaky']:>5}"
            f"{row['infra']:>5}"
            f"{row['tool_errors']:>7}"
            f"{row['prompt_tokens']:>9}"
            f"{row['completion_tokens']:>7}"
            f"{cost_text:>10}"
            f"{row['seconds']:>7.0f}s"
        )
        paint = (
            ui.success
            if row["runs"] and row["passed"] == row["runs"]
            else ui.error
            if row["passed"] == 0
            else ui.warning
        )
        print(paint(line))
        for error in row["errors"][:2]:
            print(ui.secondary(f"      ⚠ {error}"))

    note = discrimination_note(rows, repeat)
    if note:
        print(ui.warning(f"  ⚖ {note}"))


def print_case_result(result: dict) -> None:
    if not result.get("measurable", True):
        mark = ui.warning("未测")
    else:
        mark = ui.success("PASS") if result["passed"] else ui.error("FAIL")
    print(
        f"\n{mark}  {ui.heading(result['case'])}  {ui.secondary(result['model'])}"
        f"  {ui.secondary(result['description'])}"
    )
    for item in result["checks"]:
        icon = ui.success("  ✓") if item["passed"] else ui.error("  ✗")
        print(f"{icon} {item['check']}  {ui.secondary('· ' + item['detail'])}")
    if result["error"]:
        print(ui.error(f"  运行异常：{result['error']}"))
    print(
        ui.secondary(
            f"  工具 {len(result['tool_calls'])} 次"
            f"（失败 {result['tool_errors']}）· 模型 {result['model_calls']} 次"
            f" · in {result['prompt_tokens']} / out {result['completion_tokens']} tok"
            f" · {model_registry.format_cost(result['cost'])}"
            f" · {result['seconds']}s · {' → '.join(result['tool_calls']) or '无工具调用'}"
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu-eval", description="小羽 eval 执行器")
    parser.add_argument("--case", action="append", help="只跑指定 case，可重复")
    parser.add_argument("--model", help="覆盖模型")
    parser.add_argument("--models", help="多个模型横向对比，逗号分隔")
    parser.add_argument(
        "--sweep", action="store_true", help="跑全部候选模型（清单见 --list-models）"
    )
    parser.add_argument("--base-url", dest="base_url", help="覆盖端点")
    parser.add_argument("--repeat", type=int, default=1, help="每个 case 重复几次（看稳定性）")
    parser.add_argument("--list", action="store_true", help="列出所有 case")
    parser.add_argument("--list-models", action="store_true", help="列出候选模型与单价")
    parser.add_argument("--verbose", action="store_true", help="打印 agent 的完整输出")
    parser.add_argument("--no-save", action="store_true", help="不写 results/*.json")
    args = parser.parse_args(argv)

    #  sweep 动辄跑几十分钟，输出重定向到文件时 Python 默认全缓冲，
    #  会导致"日志一直是空的、看不到进度"。改成行缓冲，实时可读。
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)

    load_dotenv()

    if args.list:
        for case in case_module.CASES:
            print(f"  {case.name:<20} {case.description}")
        return 0

    if args.list_models:
        print(ui.secondary(f"  {'模型':<26}{'家族':<10}{'输入单价倍数':>12}   备注"))
        for candidate in model_registry.CANDIDATES:
            multiple = model_registry.relative_price(candidate.name)
            factor = f"{multiple:.0f}×" if multiple else "—"
            print(f"  {candidate.name:<26}{candidate.family:<10}{factor:>12}   {candidate.note}")
        return 0

    selected = case_module.CASES
    if args.case:
        selected = []
        for name in args.case:
            case = case_module.by_name(name)
            if case is None:
                print(ui.error(f"没有这个 case：{name}"))
                return 2
            selected.append(case)

    if args.sweep:
        target_models = model_registry.CANDIDATE_NAMES
    elif args.models:
        target_models = [name.strip() for name in args.models.split(",") if name.strip()]
    else:
        target_models = [args.model or Config.from_env().model]

    runs = len(target_models) * len(selected) * args.repeat
    print(
        ui.heading(
            f"eval · {len(target_models)} 个模型 × {len(selected)} 个 case × {args.repeat} 次"
            f" = {runs} 次运行"
        )
    )
    if len(target_models) > 1:
        print(ui.secondary(f"  模型：{', '.join(target_models)}"))

    results: list[dict] = []
    try:
        for model in target_models:
            for case in selected:
                for _ in range(args.repeat):
                    result = run_case(case, model, args.base_url, args.verbose)
                    results.append(result)
                    print_case_result(result)
    except MissingConfig as exc:
        print(ui.error(str(exc)))
        return 2
    except KeyboardInterrupt:
        print(ui.warning("\n[已中断，已完成的部分仍会汇总]"))

    measurable = [item for item in results if item.get("measurable", True)]
    unmeasured = len(results) - len(measurable)
    passed = sum(1 for item in measurable if item["passed"])
    total_checks = sum(len(item["checks"]) for item in measurable)
    passed_checks = sum(1 for item in measurable for c in item["checks"] if c["passed"])
    infra_note = f" · 未测 {unmeasured}（基础设施故障，不计入通过率）" if unmeasured else ""
    print(
        "\n"
        + ui.heading(f"总计 {passed}/{len(measurable)} 次可测运行通过")
        + ui.secondary(f" · 检查项 {passed_checks}/{total_checks}{infra_note}")
    )
    print_matrix(results, repeat=args.repeat)

    if not args.no_save and results:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        label = "sweep" if len(target_models) > 1 else target_models[0].replace("/", "_")
        out = RESULTS_DIR / f"{stamp}-{label}.json"
        out.write_text(
            json.dumps(
                {
                    "timestamp": stamp,
                    "models": target_models,
                    "cases": [case.name for case in selected],
                    #  来源指纹：不带配置的数字连和它自己的下一次 run 都不可比
                    "provenance": {
                        "xiaoyu_version": __version__,
                        "python": platform.python_version(),
                        "platform": platform.platform(),
                        "base_url": args.base_url,
                        "repeat": args.repeat,
                        "request_timeout": REQUEST_TIMEOUT,
                    },
                    "discrimination_note": discrimination_note(
                        summarize_by_model(results), args.repeat
                    ),
                    "summary": summarize_by_model(results),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(ui.secondary(f"结果已写入 {out}"))

    #  退出码三分：0=可测的全过且没有未测；1=有真失败；3=没有真失败但有未测
    #  （基础设施故障）。"没测到"和"测了没过"必须在退出码上就可区分，
    #  CI 里前者该重跑、后者该修。
    if passed != len(measurable):
        return 1
    if unmeasured:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
