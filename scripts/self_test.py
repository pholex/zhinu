#!/usr/bin/env python3
"""第一人称自测（tests_ai/self_test.md）跑一遍并存记录——给 nightly 用，发版不再等它。

    python scripts/self_test.py                     # 跑一遍，记录存到 <用户配置目录>/self-test/
    python scripts/self_test.py --model deepseek-flash --timeout 1800
    python scripts/self_test.py --install-launchd   # macOS：每天 03:30 自动跑（卸载 --uninstall-launchd）

记录文件 = xiaoyu `--output-format json` 的收尾对象，再补：`platform`（uname -sm）、
`recorded_at`（本地时间 ISO）、`source`（跑的是哪份源码：版本 / 分支 / 提交）。
同一目录下 `latest.json` 永远指向最近一次；scripts/release_notes.py 默认读它生成
「本版验证」一节，scripts/release.py 的预检只把它当警告（太旧 / 通过率低）——
它不是发版的门，是发版说明里的一行事实。

真调模型，密钥按 xiaoyu 自己的规则找（当前目录 .env、用户级 .env、环境变量）；
从仓库外（launchd）起时用 --env-file 指向仓库的 .env。自测在空临时工作区里跑，
用户配置目录用临时的（已有 deny 规则不能抢先拦硬红线测试）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHECKLIST = REPO / "tests_ai" / "self_test.md"
PASS_RATE = 0.8
KEEP_RECORDS = 30
LAUNCHD_LABEL = "com.pholex.xiaoyu-self-test"

SCHEMA = {
    "type": "object",
    "properties": {
        "total": {"type": "integer"},
        "passed": {"type": "integer"},
        "skipped": {"type": "integer"},
        "rate": {"type": "number"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail", "skip"]},
                    "evidence": {"type": "string"},
                },
                "required": ["id", "status", "evidence"],
            },
        },
    },
    "required": ["total", "passed", "skipped", "rate", "items"],
}


def _release_notes():
    spec = importlib.util.spec_from_file_location("release_notes", REPO / "scripts" / "release_notes.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def source_info(repo: Path = REPO) -> dict[str, str]:
    """跑的是哪份源码：__version__ + 分支 + 提交（都尽力而为）。"""
    info: dict[str, str] = {}
    try:
        text = (repo / "xiaoyu" / "__init__.py").read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        if match:
            info["version"] = match.group(1)
    except OSError:
        pass
    for key, args in (("branch", ["rev-parse", "--abbrev-ref", "HEAD"]), ("commit", ["rev-parse", "HEAD"])):
        try:
            proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=10)
            if proc.returncode == 0 and proc.stdout.strip():
                info[key] = proc.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return info


def parse_result(stdout: str) -> dict | None:
    """收尾对象是 stdout 最后一行非空 JSON；拿不到返回 None。"""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("output"), dict) else None


def run_self_test(xiaoyu: list[str], *, model: str | None, timeout: float, env_file: Path | None,
                  checklist: Path = CHECKLIST) -> tuple[dict | None, str]:
    """在空临时工作区里跑清单；返回 (收尾对象或 None, 给人看的失败说明)。"""
    prompt = checklist.read_text(encoding="utf-8")
    env = dict(os.environ)
    env["XIAOYU_UPDATE_CHECK"] = "0"
    if env_file:
        env["XIAOYU_ENV_FILE"] = str(env_file)
    with tempfile.TemporaryDirectory(prefix="xiaoyu-self-test-ws-") as ws, \
            tempfile.TemporaryDirectory(prefix="xiaoyu-self-test-cfg-") as cfg:
        env["XDG_CONFIG_HOME"] = cfg
        env["APPDATA"] = cfg
        argv = [*xiaoyu, "-p", prompt, "--yolo", "--output-format", "json",
                "--output-schema", json.dumps(SCHEMA)]
        if model:
            argv += ["--model", model]
        try:
            proc = subprocess.run(argv, cwd=ws, env=env, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return None, f"超过 {int(timeout)} 秒没跑完"
        except OSError as exc:
            return None, f"起不来：{exc}"
    data = parse_result(proc.stdout)
    if data is None:
        tail = (proc.stderr or proc.stdout).strip()[-400:]
        return None, f"退出码 {proc.returncode}，没拿到收尾对象：{tail}"
    return data, ""


def finish_record(data: dict, *, source: dict[str, str], now: dt.datetime | None = None) -> dict:
    record = dict(data)
    record["platform"] = f"{platform.system()} {platform.machine()}"
    record["recorded_at"] = (now or dt.datetime.now()).replace(microsecond=0).isoformat()
    record["source"] = source
    return record


def save_record(record: dict, out_dir: Path, *, keep: int = KEEP_RECORDS) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = record["recorded_at"].replace(":", "").replace("-", "")
    path = out_dir / f"{stamp}.json"
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    (out_dir / "latest.json").write_text(text, encoding="utf-8")
    dated = sorted(p for p in out_dir.glob("*.json") if p.name != "latest.json")
    for old in dated[:-keep] if len(dated) > keep else []:
        old.unlink(missing_ok=True)
    return path


def rate_of(record: dict) -> float:
    output = record.get("output") or {}
    counted = int(output.get("total", 0)) - int(output.get("skipped", 0))
    return (int(output.get("passed", 0)) / counted) if counted > 0 else 0.0


# ---- launchd（macOS）---------------------------------------------------------


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def render_plist(*, python: str, script: Path, out_dir: Path, hour: int, minute: int,
                 env_file: Path | None, model: str | None) -> dict:
    args = [python, str(script), "--out-dir", str(out_dir)]
    if env_file:
        args += ["--env-file", str(env_file)]
    if model:
        args += ["--model", model]
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": args,
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(out_dir / "launchd.log"),
        "StandardErrorPath": str(out_dir / "launchd.log"),
        "EnvironmentVariables": {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"},
        "RunAtLoad": False,
    }


def install_launchd(plist: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump(plist, fh)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(path)], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"launchctl bootstrap 失败：{proc.stderr.strip()}")


def uninstall_launchd(path: Path) -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], capture_output=True)
    path.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--xiaoyu", help="xiaoyu 可执行文件（默认 PATH 上的 xiaoyu，没有就 python -m xiaoyu）")
    parser.add_argument("--model", help="自测用的模型（默认出厂默认模型）")
    parser.add_argument("--timeout", type=float, default=1800.0, help="整场自测时限（秒）")
    parser.add_argument("--env-file", help="密钥所在的 .env（从仓库外起时给仓库的 .env）")
    parser.add_argument("--out-dir", help="记录目录（默认用户配置目录下 self-test/）")
    parser.add_argument("--install-launchd", action="store_true", help="macOS：装成每天定时跑")
    parser.add_argument("--uninstall-launchd", action="store_true")
    parser.add_argument("--at", default="03:30", help="--install-launchd 的时刻 HH:MM（默认 03:30）")
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir) if args.out_dir else _release_notes().self_test_dir()
    env_file = Path(args.env_file).resolve() if args.env_file else None

    if args.uninstall_launchd:
        uninstall_launchd(launchd_plist_path())
        print(f"已卸载 {LAUNCHD_LABEL}")
        return 0
    if args.install_launchd:
        if sys.platform != "darwin":
            raise SystemExit("--install-launchd 只在 macOS 上有用")
        hour, minute = (int(x) for x in args.at.split(":", 1))
        if env_file is None and (REPO / ".env").is_file():
            env_file = (REPO / ".env").resolve()
        plist = render_plist(python=sys.executable, script=Path(__file__).resolve(), out_dir=out_dir,
                             hour=hour, minute=minute, env_file=env_file, model=args.model)
        install_launchd(plist, launchd_plist_path())
        print(f"已装 {LAUNCHD_LABEL}：每天 {hour:02d}:{minute:02d} 跑，记录在 {out_dir}，日志 {out_dir / 'launchd.log'}")
        print(f"  手动触发：launchctl kickstart gui/{os.getuid()}/{LAUNCHD_LABEL}")
        return 0

    if args.xiaoyu:
        #  给的是 .py 就用本解释器跑它（测试里拿假 xiaoyu 走整条链路）
        xiaoyu = [sys.executable, args.xiaoyu] if args.xiaoyu.endswith(".py") else [args.xiaoyu]
    elif shutil.which("xiaoyu"):
        xiaoyu = [shutil.which("xiaoyu") or "xiaoyu"]
    else:
        xiaoyu = [sys.executable, "-m", "xiaoyu"]
    started = dt.datetime.now()
    print(f"[self_test] {started:%Y-%m-%d %H:%M} 开始：{' '.join(xiaoyu)}{' --model ' + args.model if args.model else ''}")
    data, problem = run_self_test(xiaoyu, model=args.model, timeout=args.timeout, env_file=env_file)
    if data is None:
        print(f"[self_test] 没跑出结果：{problem}", file=sys.stderr)
        return 2
    record = finish_record(data, source=source_info())
    path = save_record(record, out_dir)
    rate = rate_of(record)
    output = record["output"]
    print(f"[self_test] {output.get('passed')}/{output.get('total')} 通过（{output.get('skipped', 0)} 跳过，"
          f"通过率 {rate:.0%}，用时 {int((dt.datetime.now() - started).total_seconds())} 秒）→ {path}")
    for item in output.get("items") or []:
        if item.get("status") in ("fail", "skip"):
            print(f"  {item.get('status')}: {item.get('id')} — {item.get('evidence', '')}")
    return 0 if rate >= PASS_RATE else 1


if __name__ == "__main__":
    sys.exit(main())
