#!/usr/bin/env python3
"""本地一键门禁：按 CI（.github/workflows/ci.yml）同样的顺序把该跑的都跑一遍。

    python scripts/verify.py                 # 全部步骤
    python scripts/verify.py --list          # 只打印本平台的计划，不跑
    python scripts/verify.py --only unittest --only secrets
    python scripts/verify.py --quiet         # 单元测试不带 -v

与 CI 同序，CI 变了这里同步：ci.yml 的 test job（单元测试）与 build job（构建 →
twine check → wheel 冒烟 → pip-audit → 密钥特征扫描）各一步。密钥正则直接是
ci.yml 里那一条（tests/test_verify_script.py 对账，别在这里改出第二套）。

每步结果只有四种：PASS / FAIL / SKIP / WARN。工具缺失是 SKIP 并告诉你怎么装；
平台不适用也是 SKIP（显式打出来，而不是静默少跑一步）；pip-audit 失败是 WARN，
与 CI 的 continue-on-error 一致——上游刚披露的 CVE 不该把无关改动拦红，但要看得见。
有任何 FAIL 以非零退出。

只用标准库；构建产物放临时目录，不碰仓库的 dist/（本机 dist/ 里常年有旧版本的
wheel，wheel 冒烟要求目录里恰好一个 wheel）。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent

#  与 ci.yml「敏感信息扫描」那一步的 grep -E 正则逐字相同（测试对账）。
#  只扫通用密钥特征；内部专有词表不进仓库，本机 pre-commit 另有一份。
SECRET_PATTERN = r"sk-[a-zA-Z0-9]{16}|sbp_[a-z0-9]{8}|AKIA[A-Z0-9]{8}|BEGIN [A-Z ]*PRIVATE KEY"
_SECRET_RE = re.compile(SECRET_PATTERN, re.IGNORECASE)

#  步骤级超时（秒），照 ci.yml 的 timeout-minutes：挂住时限时失败，好过干等
UNITTEST_TIMEOUT = 15 * 60
WHEEL_SMOKE_TIMEOUT = 10 * 60
_DEFAULT_TIMEOUT = 10 * 60

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"


@dataclass
class Result:
    status: str
    detail: str = ""


@dataclass
class Context:
    """步骤之间传递的状态：构建产物目录等。"""

    python: str = sys.executable
    repo: Path = REPO
    verbose_tests: bool = True
    outdir: Path | None = None  # build 步骤产出的 wheel/sdist 所在目录
    results: dict[str, Result] = field(default_factory=dict)


@dataclass(frozen=True)
class Step:
    name: str
    title: str
    ci_ref: str  # 对应 ci.yml 里的哪一步
    run: Callable[[Context], Result]
    #  平台不适用时给出理由（返回 None = 适用）
    platform_skip: Callable[[], str | None] = lambda: None


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _run(ctx: Context, cmd: list[str], *, timeout: float, env: dict[str, str] | None = None,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    """子进程输出直接落终端（单元测试要能看到卡在哪个用例），只拿退出码。"""
    full_env = dict(os.environ)
    #  -P 不把 cwd 放进 sys.path，所以显式给 PYTHONPATH；PYTHONUNBUFFERED 让 -v 的
    #  用例名在卡住那一刻已经打出来（与 ci.yml 的理由相同）
    full_env["PYTHONPATH"] = str(ctx.repo)
    full_env["PYTHONUNBUFFERED"] = "1"
    if env:
        full_env.update(env)
    print(f"   $ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=str(cwd or ctx.repo), env=full_env, timeout=timeout,
                          stdin=subprocess.DEVNULL)


def _timed_out(exc: subprocess.TimeoutExpired) -> Result:
    return Result(FAIL, f"超时（{exc.timeout:.0f}s）：{exc.cmd}")


# ---- 步骤 -----------------------------------------------------------------


def step_bwrap(ctx: Context) -> Result:
    if shutil.which("bwrap"):
        return Result(PASS, "bwrap 在 PATH 里，沙箱真内核用例会真跑")
    return Result(WARN, "没有 bwrap：沙箱真内核用例会经探针跳过（apt-get install bubblewrap）")


def step_unittest(ctx: Context) -> Result:
    cmd = [ctx.python, "-P", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    if ctx.verbose_tests:
        cmd.append("-v")
    try:
        proc = _run(ctx, cmd, timeout=UNITTEST_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc)
    return Result(PASS) if proc.returncode == 0 else Result(FAIL, f"退出码 {proc.returncode}")


def step_build(ctx: Context) -> Result:
    if not _module_available("build"):
        return Result(SKIP, f"缺 build 模块：{ctx.python} -m pip install build")
    outdir = Path(tempfile.mkdtemp(prefix="xiaoyu-verify-dist-"))
    try:
        proc = _run(ctx, [ctx.python, "-m", "build", "--outdir", str(outdir)], timeout=_DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc)
    if proc.returncode != 0:
        return Result(FAIL, f"退出码 {proc.returncode}")
    ctx.outdir = outdir
    files = sorted(p.name for p in outdir.iterdir())
    return Result(PASS, f"产物在 {outdir}：{', '.join(files)}")


def _need_build(ctx: Context) -> Result | None:
    if ctx.outdir is None:
        return Result(SKIP, "没有构建产物（build 步骤没跑或没过）")
    return None


def step_twine(ctx: Context) -> Result:
    if (blocked := _need_build(ctx)) is not None:
        return blocked
    if not _module_available("twine"):
        return Result(SKIP, f"缺 twine：{ctx.python} -m pip install twine")
    assert ctx.outdir is not None
    artifacts = [str(p) for p in sorted(ctx.outdir.iterdir())]
    try:
        proc = _run(ctx, [ctx.python, "-m", "twine", "check", *artifacts], timeout=_DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc)
    return Result(PASS) if proc.returncode == 0 else Result(FAIL, f"退出码 {proc.returncode}")


def step_wheel_smoke(ctx: Context) -> Result:
    if (blocked := _need_build(ctx)) is not None:
        return blocked
    assert ctx.outdir is not None
    try:
        proc = _run(ctx, [ctx.python, str(ctx.repo / "tests" / "wheel_smoke.py"), str(ctx.outdir)],
                    timeout=WHEEL_SMOKE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return _timed_out(exc)
    return Result(PASS) if proc.returncode == 0 else Result(FAIL, f"退出码 {proc.returncode}（要能装依赖的网络）")


def step_pip_audit(ctx: Context) -> Result:
    """CI 审计的是装好的 wheel 的依赖闭包；本地 venv 是同一份锁定依赖的可编辑安装，
    直接审计当前解释器环境，省掉再建一个 venv 装一遍的网络开销。"""
    if _module_available("pip_audit"):
        cmd = [ctx.python, "-m", "pip_audit", "--strict", "--desc", "on"]
    elif shutil.which("pip-audit"):
        cmd = ["pip-audit", "--strict", "--desc", "on"]
    else:
        return Result(SKIP, f"缺 pip-audit：{ctx.python} -m pip install pip-audit（要联网查漏洞库）")
    try:
        proc = _run(ctx, cmd, timeout=_DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        return Result(WARN, f"超时（{exc.timeout:.0f}s）")
    if proc.returncode == 0:
        return Result(PASS)
    return Result(WARN, f"退出码 {proc.returncode}——与 CI 一样不拦，处理方式是升依赖那一行")


def scan_secrets(root: Path) -> list[str]:
    """逐文件按行匹配密钥特征，返回「相对路径:行号: 片段」清单（空 = 干净）。"""
    hits: list[str] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if _SECRET_RE.search(line):
                hits.append(f"{path.relative_to(root).as_posix()}:{lineno}: {line.strip()[:120]}")
    return hits


def step_secrets(ctx: Context) -> Result:
    """CI 扫的是 sdist 解包后的内容。没有 sdist 时退而扫 git 跟踪的文件——范围只会更大。"""
    if ctx.outdir is not None and (sdists := sorted(ctx.outdir.glob("*.tar.gz"))):
        with tempfile.TemporaryDirectory(prefix="xiaoyu-verify-audit-") as tmp:
            with tarfile.open(sdists[0]) as tar:
                #  3.12 起有 data 过滤器（3.11.4 回移），更老的小版本退回无过滤：sdist 是自己刚构建的
                tar.extractall(tmp, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
            hits = scan_secrets(Path(tmp))
        scope = f"sdist {sdists[0].name}"
    else:
        if not shutil.which("git"):
            return Result(SKIP, "没有 sdist 也没有 git，无从确定扫描范围")
        proc = subprocess.run(["git", "-C", str(ctx.repo), "ls-files", "-z"], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            return Result(SKIP, "git ls-files 失败")
        hits = []
        for rel in proc.stdout.split("\0"):
            path = ctx.repo / rel
            if not rel or not path.is_file():
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if _SECRET_RE.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()[:120]}")
        scope = "git 跟踪的文件（没有 sdist）"
    if hits:
        return Result(FAIL, f"{scope} 含密钥特征：\n" + "\n".join(f"      {h}" for h in hits[:20]))
    return Result(PASS, f"扫描范围：{scope}")


STEPS: tuple[Step, ...] = (
    Step("bwrap", "Linux：bubblewrap 在不在", "test job「Linux：装 bubblewrap」", step_bwrap,
         platform_skip=lambda: None if sys.platform.startswith("linux") else "只在 Linux 上有意义"),
    Step("unittest", "单元测试（全量，不打网络）", "test job「单元测试」", step_unittest),
    Step("build", "构建 wheel + sdist", "build job「python -m build」", step_build),
    Step("twine", "元数据检查", "build job「twine check」", step_twine),
    Step("wheel_smoke", "装好的 wheel 黑盒冒烟", "build job「wheel 黑盒冒烟」", step_wheel_smoke),
    Step("pip_audit", "依赖漏洞审计（失败只警告）", "build job「pip-audit」", step_pip_audit),
    Step("secrets", "密钥特征扫描", "build job「敏感信息扫描」", step_secrets),
)
STEP_NAMES = tuple(step.name for step in STEPS)


def plan(only: list[str] | None = None) -> list[tuple[Step, str | None]]:
    """本平台的执行计划：(步骤, 平台不适用的理由或 None)。"""
    chosen = [s for s in STEPS if not only or s.name in only]
    return [(step, step.platform_skip()) for step in chosen]


def run_steps(ctx: Context, only: list[str] | None = None) -> int:
    for step, skip_reason in plan(only):
        print(f"\n== {step.name}：{step.title}（CI：{step.ci_ref}）", flush=True)
        started = time.monotonic()
        if skip_reason:
            result = Result(SKIP, skip_reason)
        else:
            try:
                result = step.run(ctx)
            except Exception as exc:  # 步骤自身的 bug 也要计入 FAIL，不能让门禁静默过
                result = Result(FAIL, f"步骤异常：{exc.__class__.__name__}: {exc}")
        ctx.results[step.name] = result
        elapsed = time.monotonic() - started
        print(f"   [{result.status}] {result.detail or ''}（{elapsed:.1f}s）".rstrip(), flush=True)

    print("\n== 汇总")
    width = max(len(name) for name in ctx.results) if ctx.results else 8
    for name, result in ctx.results.items():
        first = result.detail.splitlines()[0] if result.detail else ""
        print(f"   {name:<{width}}  {result.status:<4}  {first}")
    failed = [name for name, r in ctx.results.items() if r.status == FAIL]
    if failed:
        print(f"\n门禁未过：{', '.join(failed)}")
        return 1
    print("\n门禁通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="只打印本平台的计划")
    parser.add_argument("--only", action="append", choices=STEP_NAMES, metavar="STEP",
                        help=f"只跑这些步骤（可重复）：{', '.join(STEP_NAMES)}")
    parser.add_argument("--quiet", action="store_true", help="单元测试不带 -v")
    args = parser.parse_args(argv)

    if args.list:
        print(f"平台 {sys.platform}，解释器 {sys.executable}")
        for step, skip_reason in plan(args.only):
            mark = f"SKIP（{skip_reason}）" if skip_reason else "将运行"
            print(f"  {step.name:<12} {step.title:<28} ← {step.ci_ref}  [{mark}]")
        return 0

    ctx = Context(verbose_tests=not args.quiet)
    code = run_steps(ctx, args.only)
    if ctx.outdir is not None and code == 0:
        shutil.rmtree(ctx.outdir, ignore_errors=True)
    elif ctx.outdir is not None:
        print(f"构建产物保留在 {ctx.outdir} 供排查")
    return code


if __name__ == "__main__":
    sys.exit(main())
