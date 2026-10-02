"""harbor 适配器：把本地构建的小羽 wheel 装进任务容器，以 headless 方式跑任务指令。

一次 trial 的生命周期：

1. ``install()``：上传 wheel 与 ``install.sh``，以 root 在 ``/installed-agent/venv`` 里装好；
2. ``run()``：把指令写进容器文件，``xiaoyu -p --yolo --unattended --no-sandbox
   --output-format stream-json < 指令文件``，事件流 tee 到 ``/logs/agent/xiaoyu.jsonl``；
   会话日志经 ``XDG_CONFIG_HOME`` 也落在 ``/logs/agent`` 下，超时被杀也能拿到；
3. ``populate_context_post_run()``：从收尾对象取用量（没有收尾对象就从会话日志的
   ``request`` 事件累加），按单价表折算费用，并把事件流转成 ATIF 轨迹。

模型写成 ``provider/model``：``gateway/deepseek-flash``（OpenAI 兼容网关，读宿主的
``XIAOYU_BASE_URL`` / ``XIAOYU_API_KEY``）或 ``anthropic/claude-sonnet-5-5`` 这类直连。
密钥只在 ``run()`` 时从宿主环境读，不进生成的 harbor 配置文件。
"""

from __future__ import annotations

import json
import os
import shlex
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import urlparse

from harbor.agents.capabilities import AgentCapabilities
from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext, ModelUsage
from harbor.models.trajectories import (
    Agent,
    FinalMetrics,
    Metrics,
    Observation,
    ObservationResult,
    Step,
    ToolCall,
    Trajectory,
)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

CONTAINER_ROOT = "/installed-agent"
CONTAINER_INSTALL_SCRIPT = f"{CONTAINER_ROOT}/install.sh"
CONTAINER_INSTRUCTION = f"{CONTAINER_ROOT}/instruction.txt"
CONTAINER_XIAOYU = f"{CONTAINER_ROOT}/venv/bin/xiaoyu"
AGENT_LOG_DIR = "/logs/agent"
STREAM_LOG = "xiaoyu.jsonl"
STDERR_LOG = "xiaoyu.stderr.txt"
#  XDG_CONFIG_HOME 指到这里：会话日志（每次模型调用一条 request 事件）随 /logs/agent
#  一起被 harbor 下载，AgentTimeoutError 把进程杀掉时照样有用量可算
CONFIG_SUBDIR = "config"

#  每家 provider 在宿主上要有哪些变量（元组内任一命中即可），运行时按同名注入容器
PROVIDER_SECRETS: dict[str, list[tuple[str, ...]]] = {
    "gateway": [("XIAOYU_BASE_URL",), ("XIAOYU_API_KEY", "LITELLM_API_KEY")],
    "deepseek": [("DEEPSEEK_API_KEY",)],
    "moonshot": [("MOONSHOT_API_KEY",)],
    "qwen": [("QWEN_API_KEY", "DASHSCOPE_API_KEY")],
    "zhipu": [("ZHIPU_API_KEY",)],
    "anthropic": [("ANTHROPIC_API_KEY",)],
    "gemini": [("GEMINI_API_KEY", "GOOGLE_API_KEY")],
    "openai": [("OPENAI_API_KEY",)],
    "xai": [("XAI_API_KEY",)],
    #  Bedrock：区域是激活信号；凭证走 AWS 变量（下方 BEDROCK_PASSTHROUGH），有啥带啥
    "bedrock": [("XIAOYU_BEDROCK_REGION",)],
}
BEDROCK_PASSTHROUGH = (
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
)
#  任务声明 network_mode=allowlist 时，模型端点得显式放行；gateway 的主机名从 BASE_URL 取
PROVIDER_HOSTS: dict[str, list[str]] = {
    "deepseek": ["api.deepseek.com"],
    "moonshot": ["api.moonshot.cn", "api.moonshot.ai"],
    "qwen": ["dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"],
    "zhipu": ["open.bigmodel.cn"],
    "anthropic": ["api.anthropic.com"],
    "gemini": ["generativelanguage.googleapis.com"],
    "openai": ["api.openai.com"],
    "xai": ["api.x.ai"],
    "bedrock": ["*.amazonaws.com"],
}

DEFAULT_PREAMBLE = (
    "You are running unattended inside a Docker container as one trial of an "
    "automated benchmark. Nobody will answer questions, confirm anything, or read "
    "progress reports: do not ask, do not wait, and do not stop at a plan. Use your "
    "tools to finish the task completely, verify the result yourself, and only then "
    "reply with a short summary of what was done."
)


def split_model(model_name: str | None) -> tuple[str, str]:
    """``provider/model`` → (provider, model)；没有斜杠视为网关模型。"""
    if not model_name:
        raise ValueError("model_name 不能为空，写成 provider/model，如 gateway/deepseek-flash")
    if "/" not in model_name:
        return "gateway", model_name
    provider, model = model_name.split("/", 1)
    return provider, model


def _first_present(names: tuple[str, ...]) -> tuple[str, str] | None:
    for name in names:
        value = os.environ.get(name)
        if value:
            return name, value
    return None


def missing_secrets(provider: str) -> list[str]:
    """宿主上缺哪些变量（每组给出全部候选名，用 | 连接）。未知 provider 视为不缺。"""
    missing: list[str] = []
    for group in PROVIDER_SECRETS.get(provider, []):
        if _first_present(group) is None:
            missing.append(" | ".join(group))
    return missing


def secret_env(provider: str) -> dict[str, str]:
    """从宿主环境取这家 provider 的密钥，键名用小羽认的那个。"""
    env: dict[str, str] = {}
    for group in PROVIDER_SECRETS.get(provider, []):
        found = _first_present(group)
        if found is None:
            continue
        name, value = found
        #  网关 key 的两个别名统一成 XIAOYU_API_KEY，其它按原名
        env[group[0] if provider == "gateway" else name] = value
    if provider == "bedrock":
        for name in BEDROCK_PASSTHROUGH:
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


def provider_hosts(provider: str) -> list[str]:
    if provider == "gateway":
        host = urlparse(os.environ.get("XIAOYU_BASE_URL", "")).hostname
        return [host] if host else []
    return list(PROVIDER_HOSTS.get(provider, []))


# ---------------------------------------------------------------- 单价表


def load_price_table(extra: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """合并单价表：仓库自带 prices.json → 本机 xiaoyu/evals/models.local.json → --prices 指定文件。

    后者覆盖前者。每条 ``{"name", "in", "out"}``，单位 USD / token。
    """
    sources: list[Path] = [
        HERE / "prices.json",
        REPO_ROOT / "xiaoyu" / "evals" / "models.local.json",
    ]
    if extra:
        sources.append(Path(extra).expanduser())
    table: dict[str, dict[str, Any]] = {}
    for path in sources:
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in data.get("models") or []:
            name = entry.get("name")
            if not name or "in" not in entry or "out" not in entry:
                continue
            table[str(name)] = {
                "in": float(entry["in"]),
                "out": float(entry["out"]),
                "source": path.name,
            }
    return table


def _bare(name: str) -> str:
    return name.rsplit("/", 1)[-1]


def lookup_price(table: dict[str, dict[str, Any]], model: str) -> dict[str, Any] | None:
    """先全名，再去掉 provider 前缀；仍没有就按裸名找同名条目。"""
    if model in table:
        return table[model]
    bare = _bare(model)
    if bare in table:
        return table[bare]
    for name, entry in table.items():
        if _bare(name) == bare:
            return entry
    return None


def price_usage(
    table: dict[str, dict[str, Any]], by_model: dict[str, dict[str, int]]
) -> tuple[float | None, dict[str, float | None], list[str]]:
    """(总费用, 每模型费用, 没有单价的模型)。全部模型都没单价时总费用为 None。"""
    per_model: dict[str, float | None] = {}
    unpriced: list[str] = []
    total = 0.0
    priced_any = False
    for model, usage in by_model.items():
        entry = lookup_price(table, model)
        if entry is None:
            per_model[model] = None
            unpriced.append(model)
            continue
        cost = usage.get("prompt_tokens", 0) * entry["in"] + usage.get("completion_tokens", 0) * entry["out"]
        per_model[model] = cost
        total += cost
        priced_any = True
    return (total if priced_any else None), per_model, unpriced


# ---------------------------------------------------------------- 日志解析


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """逐行解析 NDJSON，坏行（半截、非 JSON 的 stderr 混入）直接跳过。"""
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def find_result(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if event.get("kind") == "result":
            return event
    return None


def find_session_log(logs_dir: Path) -> Path | None:
    """``/logs/agent/config/xiaoyu/sessions/<工作区>/*.jsonl``，取最新的一份。"""
    root = logs_dir / CONFIG_SUBDIR / "xiaoyu" / "sessions"
    if not root.is_dir():
        return None
    candidates = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def successful_requests(session_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """会话日志里成功落账的模型调用（有 prompt/completion 字段的 request 事件）。"""
    out: list[dict[str, Any]] = []
    for record in session_records:
        if record.get("event") != "request" or record.get("error"):
            continue
        if record.get("prompt_tokens") is None and record.get("completion_tokens") is None:
            continue
        out.append(record)
    return out


def usage_from_requests(requests: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, dict[str, int]] = {}
    for record in requests:
        model = f"{record.get('provider', '?')}/{record.get('model', '?')}"
        entry = by_model.setdefault(model, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0})
        entry["calls"] += 1
        entry["prompt_tokens"] += int(record.get("prompt_tokens") or 0)
        entry["completion_tokens"] += int(record.get("completion_tokens") or 0)
    return {
        "turns": sum(e["calls"] for e in by_model.values()),
        "prompt_tokens": sum(e["prompt_tokens"] for e in by_model.values()),
        "completion_tokens": sum(e["completion_tokens"] for e in by_model.values()),
        "by_model": by_model,
    }


class _StepDraft:
    def __init__(self, model: str | None) -> None:
        self.model = model
        self.text: list[str] = []
        self.tool_calls: list[ToolCall] = []
        self.results: list[ObservationResult] = []

    def empty(self) -> bool:
        return not self.text and not self.tool_calls


def build_trajectory(
    events: list[dict[str, Any]],
    *,
    instruction: str,
    requests: list[dict[str, Any]],
    model_name: str,
    version: str,
    session_id: str,
    usage: dict[str, Any] | None,
    total_cost: float | None,
) -> Trajectory:
    """stream-json 事件流 → ATIF：一次 request.started 开一个 agent step，
    text.delta 拼成正文，tool.pending/completed 配成调用与观测；每步的 token
    数按顺序对上会话日志里成功的 request 事件（重试失败的那次不占位）。
    """
    steps: list[Step] = [Step(step_id=1, source="user", message=instruction)]
    draft: _StepDraft | None = None
    req_index = 0
    call_counter = 0

    def flush() -> None:
        nonlocal draft, req_index
        if draft is None or draft.empty():
            draft = None
            return
        metrics = None
        if req_index < len(requests):
            record = requests[req_index]
            metrics = Metrics(
                prompt_tokens=int(record.get("prompt_tokens") or 0),
                completion_tokens=int(record.get("completion_tokens") or 0),
                extra={k: record[k] for k in ("total_ms", "first_chunk_ms", "finish") if k in record} or None,
            )
        req_index += 1
        steps.append(
            Step(
                step_id=len(steps) + 1,
                source="agent",
                model_name=draft.model or model_name,
                message="".join(draft.text),
                tool_calls=draft.tool_calls or None,
                observation=Observation(results=draft.results) if draft.results else None,
                metrics=metrics,
                llm_call_count=1,
            )
        )
        draft = None

    for event in events:
        kind = event.get("kind")
        if kind == "request.started":
            if draft is not None and not draft.empty():
                flush()
            if draft is None:
                draft = _StepDraft(event.get("model"))
            continue
        if kind == "result":
            break
        if draft is None:
            draft = _StepDraft(None)
        if kind == "text.delta":
            draft.text.append(str(event.get("text", "")))
        elif kind == "tool.pending":
            call_counter += 1
            args = event.get("args")
            draft.tool_calls.append(
                ToolCall(
                    tool_call_id=f"call_{call_counter}",
                    function_name=str(event.get("name", "?")),
                    arguments=args if isinstance(args, dict) else {"value": args},
                )
            )
        elif kind in ("tool.completed", "tool.denied"):
            index = len(draft.results)
            source = draft.tool_calls[index].tool_call_id if index < len(draft.tool_calls) else None
            if kind == "tool.completed":
                content = str(event.get("output", ""))
                extra = {"ok": event.get("ok"), "seconds": event.get("seconds")}
            else:
                content = f"[denied by {event.get('by', '?')}]"
                extra = {"denied": True}
            draft.results.append(ObservationResult(source_call_id=source, content=content, extra=extra))
    flush()

    final = FinalMetrics(
        total_prompt_tokens=(usage or {}).get("prompt_tokens"),
        total_completion_tokens=(usage or {}).get("completion_tokens"),
        total_cost_usd=total_cost,
        total_steps=len(steps),
    )
    return Trajectory(
        schema_version="ATIF-v1.7",
        session_id=session_id,
        agent=Agent(name="xiaoyu", version=version, model_name=model_name),
        steps=steps,
        final_metrics=final,
        notes="Converted from xiaoyu --output-format stream-json (events) + session log (per-request usage)",
    )


# ---------------------------------------------------------------- 适配器


class XiaoyuWheelAgent(BaseInstalledAgent):
    """把本地 wheel 装进任务容器跑小羽。

    kwargs（经 harbor 配置的 ``agents[].kwargs`` 传入）：

    - ``wheel``：宿主上的 wheel 路径（必填）；
    - ``extras``：装包时附带的 extras，如 ``"[bedrock]"``；
    - ``env``：注入容器的非密钥环境变量（来自 config_template.yaml 的 ``env``）；
    - ``budget_tokens`` / ``effort``：对应 ``--budget-tokens`` / ``--effort``；
    - ``preamble``：追加到 system prompt 的无人值守说明（默认见 DEFAULT_PREAMBLE）；
    - ``prices_file``：额外的单价表；``python_spec``：容器里 uv 选解释器的版本约束。
    """

    capabilities = AgentCapabilities(atif=True)

    def __init__(
        self,
        *args: Any,
        wheel: str,
        extras: str = "",
        env: dict[str, str] | None = None,
        budget_tokens: int | None = None,
        effort: str | None = None,
        preamble: str | None = None,
        prices_file: str | None = None,
        python_spec: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.wheel = Path(wheel).expanduser().resolve()
        self.extras = extras or ""
        self.base_env = {str(k): str(v) for k, v in (env or {}).items()}
        self.budget_tokens = budget_tokens
        self.effort = effort
        self.preamble = preamble if preamble is not None else DEFAULT_PREAMBLE
        self.prices_file = prices_file
        self.python_spec = python_spec
        self._instruction = ""

    @staticmethod
    def name() -> str:
        return "xiaoyu"

    def get_version_command(self) -> str | None:
        return f"{CONTAINER_XIAOYU} --version"

    def parse_version(self, stdout: str) -> str:
        #  输出形如 "xiaoyu 0.60.0"
        parts = stdout.strip().split()
        return parts[-1] if parts else stdout.strip()

    # ---- install

    async def install(self, environment: BaseEnvironment) -> None:
        if not self.wheel.is_file():
            raise FileNotFoundError(f"wheel 不存在：{self.wheel}")
        container_wheel = f"{CONTAINER_ROOT}/{self.wheel.name}"
        await environment.upload_file(self.wheel, container_wheel)
        await environment.upload_file(HERE / "install.sh", CONTAINER_INSTALL_SCRIPT)
        #  curl 用来取 uv；装不上（镜像没有包管理器 / 不能联网）也不致命，
        #  install.sh 还有系统 python3 的退路
        try:
            await self.ensure_system_dependencies(environment, ("curl",))
        except Exception as exc:  # noqa: BLE001 - 退路在 install.sh 里
            self.logger.warning("装 curl 失败，交给 install.sh 的退路：%s", exc)
        env = {"XIAOYU_HARBOR_PYTHON": self.python_spec} if self.python_spec else None
        await self.exec_as_root(
            environment,
            command=(
                f"sh {CONTAINER_INSTALL_SCRIPT} {shlex.quote(container_wheel)} "
                f"{shlex.quote(self.extras)}"
            ),
            env=env,
            timeout_sec=900,
        )

    # ---- run

    def _run_env(self) -> dict[str, str]:
        provider, model = split_model(self.model_name)
        env = dict(self.base_env)
        env.update(secret_env(provider))
        env["XIAOYU_MODEL"] = model
        #  只注册这一家：裸模型名不会被别的直连预设抢走，摘要/explore 子 agent 也用同一个模型
        env["XIAOYU_PROVIDERS"] = provider
        env.setdefault("XIAOYU_SUMMARY_MODEL", model)
        env.setdefault("XIAOYU_EXPLORE_MODEL", model)
        env["XDG_CONFIG_HOME"] = f"{AGENT_LOG_DIR}/{CONFIG_SUBDIR}"
        return env

    def _cli_flags(self) -> list[str]:
        flags = ["-p", "--yolo", "--unattended", "--no-sandbox", "--output-format", "stream-json"]
        if self.budget_tokens:
            flags += ["--budget-tokens", str(int(self.budget_tokens))]
        if self.effort:
            flags += ["--effort", self.effort]
        if self.preamble:
            flags += ["--append-system-prompt", self.preamble]
        return flags

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment, context: AgentContext) -> None:
        self._instruction = instruction
        env = self._run_env()
        with TemporaryDirectory() as tmp:
            local = Path(tmp) / "instruction.txt"
            local.write_text(instruction, encoding="utf-8")
            await environment.upload_file(local, CONTAINER_INSTRUCTION)
        await self.exec_as_root(environment, command=f"chmod 644 {CONTAINER_INSTRUCTION}")

        command = (
            f"mkdir -p {AGENT_LOG_DIR} && "
            f"{CONTAINER_XIAOYU} {shlex.join(self._cli_flags())} "
            f"< {CONTAINER_INSTRUCTION} "
            f"2> {AGENT_LOG_DIR}/{STDERR_LOG} | tee {AGENT_LOG_DIR}/{STREAM_LOG}"
        )
        await self.exec_as_agent(environment, command=command, env=env)

    # ---- post-run

    def populate_context_post_run(self, context: AgentContext) -> None:
        events = read_jsonl(self.logs_dir / STREAM_LOG)
        result = find_result(events)
        session_path = find_session_log(self.logs_dir)
        requests = successful_requests(read_jsonl(session_path)) if session_path else []

        usage: dict[str, Any] | None = None
        usage_source = "none"
        if result and isinstance(result.get("usage"), dict):
            usage = result["usage"]
            usage_source = "result"
        elif requests:
            usage = usage_from_requests(requests)
            usage_source = "session_log"

        metadata: dict[str, Any] = {"usage_source": usage_source}
        if result is not None:
            if result.get("error"):
                metadata["xiaoyu_error"] = result["error"]
            if result.get("background_tasks_terminated"):
                metadata["background_tasks_terminated"] = len(result["background_tasks_terminated"])
        if session_path is not None:
            metadata["session_log"] = str(session_path.relative_to(self.logs_dir))

        total_cost: float | None = None
        if usage is not None:
            by_model = usage.get("by_model") or {}
            table = load_price_table(self.prices_file)
            total_cost, per_model, unpriced = price_usage(table, by_model)
            context.n_input_tokens = int(usage.get("prompt_tokens") or 0)
            context.n_output_tokens = int(usage.get("completion_tokens") or 0)
            #  小羽的用量账本不分列缓存命中，这里如实留空
            context.n_cache_tokens = None
            context.cost_usd = total_cost
            context.model_usage = {
                model: ModelUsage(
                    n_input_tokens=int(entry.get("prompt_tokens") or 0),
                    n_output_tokens=int(entry.get("completion_tokens") or 0),
                    cost_usd=per_model.get(model),
                )
                for model, entry in by_model.items()
            }
            metadata["turns"] = int(usage.get("turns") or 0)
            metadata["cost_source"] = "price_table" if total_cost is not None else "unpriced"
            if unpriced:
                metadata["unpriced_models"] = unpriced
        context.metadata = {**(context.metadata or {}), **metadata}

        if not events:
            return
        try:
            trajectory = build_trajectory(
                events,
                instruction=self._instruction,
                requests=requests,
                model_name=self.model_name or "?",
                version=self.version() or "unknown",
                session_id=self.session_id or str(uuid.uuid4()),
                usage=usage,
                total_cost=total_cost,
            )
            (self.logs_dir / "trajectory.json").write_text(
                json.dumps(trajectory.to_json_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001 - 轨迹是附加产物，失败不该掀翻 trial
            self.logger.warning("ATIF 轨迹生成失败：%s", exc)
