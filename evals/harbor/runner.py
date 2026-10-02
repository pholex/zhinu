"""`run` 子命令：读 .env、准备 wheel、生成 harbor 配置、拉起 `harbor run`。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from agent import missing_secrets, provider_hosts, split_model

HARBOR_DIR = Path(__file__).resolve().parent
REPO_ROOT = HARBOR_DIR.parent.parent
RUNS_DIR = HARBOR_DIR / "runs"
BUILD_DIR = HARBOR_DIR / ".build"
CONFIG_TEMPLATE_PATH = HARBOR_DIR / "config_template.yaml"

DEFAULT_DATASET = "terminal-bench/terminal-bench-2"
DEFAULT_CONCURRENCY = 4


def default_model() -> str:
    """没给 --model 就用宿主 .env 里的主模型，走网关。"""
    return f"gateway/{os.environ.get('XIAOYU_MODEL') or 'deepseek-flash'}"


# ---------------------------------------------------------------- .env


def dotenv_candidates(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    return [Path.cwd() / ".env", HARBOR_DIR / ".env", REPO_ROOT / ".env"]


def load_dotenv(explicit: str | None) -> Path | None:
    """只往 os.environ 里 setdefault，已有的真实环境变量优先。值不打印。"""
    for path in dotenv_candidates(explicit):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
                value = value[1:-1]
            elif " #" in value:
                value = value.split(" #", 1)[0].rstrip()
            if key and value:
                os.environ.setdefault(key, value)
        return path
    return None


# ---------------------------------------------------------------- wheel


def build_wheel() -> Path:
    """在仓库根用 uv build 打 wheel，放到 evals/harbor/.build/。"""
    if shutil.which("uv") is None:
        raise RuntimeError("找不到 uv；装好 uv 或用 --wheel 指定已构建的 wheel")
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    for stale in BUILD_DIR.glob("*.whl"):
        stale.unlink()
    print(f"构建 wheel：uv build --wheel（仓库 {REPO_ROOT}）")
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(BUILD_DIR), str(REPO_ROOT)],
        check=True,
    )
    wheels = sorted(BUILD_DIR.glob("*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        raise RuntimeError(f"uv build 没产出 wheel：{BUILD_DIR}")
    return wheels[-1]


def resolve_wheel(explicit: str | None) -> Path:
    if explicit:
        wheel = Path(explicit).expanduser().resolve()
        if not wheel.is_file():
            raise ValueError(f"--wheel 不存在：{wheel}")
        return wheel
    return build_wheel()


# ---------------------------------------------------------------- 配置


def load_template() -> dict[str, Any]:
    if not CONFIG_TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"缺模板：{CONFIG_TEMPLATE_PATH}")
    data = yaml.safe_load(CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("config_template.yaml 顶层必须是映射")
    return data


def parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_kv(items: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--env 要写成 KEY=VALUE：{item}")
        key, _, value = item.partition("=")
        out[key.strip()] = value
    return out


def default_job_name(model: str, dataset: str) -> str:
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-")
    safe_dataset = re.sub(r"[^A-Za-z0-9._-]+", "-", dataset.rsplit("/", 1)[-1]).strip("-")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"xiaoyu-{safe_dataset}-{safe_model}-{stamp}"


def validate_job_name(name: str) -> str:
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", name):
        raise ValueError("--job-name 只能用字母数字开头，之后字母数字 . _ -")
    return name


def dataset_config(dataset_ref: str, tasks: list[str], n_tasks: int | None) -> dict[str, Any]:
    name, sep, ref = dataset_ref.rpartition("@")
    dataset: dict[str, Any] = {"name": name if sep else dataset_ref}
    if sep:
        dataset["version"] = ref
    if tasks:
        dataset["task_names"] = tasks
    if n_tasks:
        dataset["n_tasks"] = n_tasks
    return dataset


def build_harbor_config(args: argparse.Namespace, wheel: Path) -> dict[str, Any]:
    provider, _ = split_model(args.model)
    missing = missing_secrets(provider)
    if missing:
        raise ValueError(
            f"provider '{provider}' 在宿主环境里缺变量：{', '.join(missing)}。"
            f"放进 .env（当前目录 / {HARBOR_DIR} / 仓库根）或用 --env-file 指定"
        )
    if args.trials < 1 or args.concurrency < 1:
        raise ValueError("--trials 与 --concurrency 至少为 1")
    if args.timeout_multiplier <= 0:
        raise ValueError("--timeout-multiplier 必须为正")

    template = load_template()
    env = {str(k): str(v) for k, v in (template.get("env") or {}).items()}
    env.update(parse_kv(args.env))

    agent_kwargs: dict[str, Any] = {
        "wheel": str(wheel),
        "env": env,
        "preamble": template.get("preamble", None),
    }
    if args.extras:
        agent_kwargs["extras"] = f"[{args.extras.strip('[]')}]"
    if args.budget_tokens:
        agent_kwargs["budget_tokens"] = args.budget_tokens
    if args.effort:
        agent_kwargs["effort"] = args.effort
    if args.prices:
        agent_kwargs["prices_file"] = str(Path(args.prices).expanduser().resolve())
    if template.get("python_spec"):
        agent_kwargs["python_spec"] = str(template["python_spec"])

    allowed_hosts = provider_hosts(provider) + list(args.allow_host or [])

    job_name = validate_job_name(args.job_name) if args.job_name else default_job_name(args.model, args.dataset)
    config: dict[str, Any] = {
        "job_name": job_name,
        "jobs_dir": str(RUNS_DIR),
        "n_attempts": args.trials,
        "n_concurrent_trials": args.concurrency,
        "environment": {"type": "docker", "force_build": False, "delete": True},
        "agents": [
            {
                "import_path": "agent:XiaoyuWheelAgent",
                "model_name": args.model,
                "kwargs": agent_kwargs,
                "extra_allowed_hosts": allowed_hosts,
            }
        ],
        "datasets": [dataset_config(args.dataset, args.tasks, args.n_tasks)],
    }
    if args.timeout_multiplier != 1.0:
        config["timeout_multiplier"] = args.timeout_multiplier
    if args.max_retries:
        config["retry"] = {"max_retries": args.max_retries}
    return config


def cmd_run(args: argparse.Namespace) -> int:
    loaded = load_dotenv(args.env_file)
    if not args.model:
        args.model = default_model()
    try:
        wheel = resolve_wheel(args.wheel)
        config = build_harbor_config(args, wheel)
    except Exception as exc:  # noqa: BLE001 - 面向用户的一句话
        print(f"error: {exc}", file=sys.stderr)
        return 2

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    job_dir = RUNS_DIR / config["job_name"]
    job_dir.mkdir(parents=True, exist_ok=True)
    config_path = job_dir / "_generated_config.json"
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Job:     {config['job_name']}")
    print(f"Model:   {args.model}")
    print(f"Wheel:   {wheel}")
    print(f".env:    {loaded or '（没找到，只用当前环境变量）'}")
    print(f"Config:  {config_path}")
    print(f"Runs:    {RUNS_DIR}")
    if args.dry_run:
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return 0

    #  用本脚本的解释器起 harbor（uv 给 PEP 723 脚本建的环境里 harbor 不一定在 PATH 上）
    command = [
        sys.executable, "-c", "from harbor.cli.main import app; app()",
        "run", "-c", str(config_path),
    ]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{HARBOR_DIR}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    #  多数任务是 public 网络策略，harbor 会逐 trial 警告"放行清单被忽略"；清单只对
    #  allowlist 任务有意义，这条警告没有信息量，压掉
    env.setdefault("PYTHONWARNINGS", "ignore::UserWarning:harbor.trial.network_policy")
    completed = subprocess.run(command, env=env, check=False)
    return completed.returncode
