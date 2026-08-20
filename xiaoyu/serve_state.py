"""serve 的控制面存储：Agent 对象（版本化）、会话清单（可恢复）、预算结算。

`serve.py` 是数据面——会话怎么跑、事件怎么流、审批怎么回。这里是它的薄控制面，
三件事都是"常驻多会话服务"才会有的需求，单机 TUI/CLI 用不到，所以单独成模块、
纯 Python、不依赖 fastapi，能脱离 HTTP 单测：

- **Agent 对象**：把 model / mode / system prompt 追加 / 审批档 / 沙箱 / 预算 /
  定价打包成一个**持久化、带版本**的配置对象。每次更新出一个新版本（旧版本
  原样留档），会话创建时钉住某个版本——之后再改 agent 不影响已在跑的会话，
  可回滚、可并排比。没有这层，serve 的全部会话共用启动参数那一份配置，
  一个服务只能服务一种用法。
- **会话清单**：进程重启后能把会话接回来所需的最小信息（工作区、agent 引用、
  会话日志路径、游标水位）。对话历史本身已在会话日志里（session_log），
  清单只记"去哪找、怎么拼"。
- **预算**：token 为主币种——xiaoyu 记账只到 provider/model 的 token，**没有
  价格表**，跨十几家 provider 维护一张表既不准也没人更新。美元预算是可选的，
  只在 agent 带了 `pricing` 时生效；用到未定价的模型按**超支**处理（fail
  closed）——"算不出来就当没超"是钱的事上最不该有的默认。

存储形态是一目录一 JSON 文件，不引数据库：这一层的量级是几十个 agent、几百个
会话清单，文件即真相、`cat` 即可审计，与 xiaoyu "明文可审"的底线一致。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#  Agent 对象可携带的配置键。只收这一撮：它们都是 serve 启动参数里"会话可覆盖"
#  的那一类；token/host/root 这种服务级参数不进 agent（那是运维边界，不是用法）。
AGENT_CONFIG_KEYS = (
    "model",
    "base_url",
    "mode",
    "approval",
    "append_system_prompt",
    "sandbox",
    "sandbox_network",
    "budget",
    "pricing",
)
MAX_NAME = 64


class StateError(ValueError):
    """请求不合法（调用方的错，HTTP 侧映射成 400）。"""


class NotFound(KeyError):
    """引用的对象不存在（HTTP 侧映射成 404）。"""


# ---------- 预算 ----------


@dataclass(frozen=True)
class Budget:
    """一个会话的硬上限。两个币种都可为空；都空等于没预算（用 None 表示更直白）。"""

    tokens: int | None = None
    usd: float | None = None

    @classmethod
    def parse(cls, raw: Any) -> "Budget | None":
        """请求体 → Budget。None / {} / 全空字段 → None（无预算）。"""
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise StateError("budget 必须是对象，形如 {\"tokens\": 200000} 或 {\"usd\": 5}")
        unknown = set(raw) - {"tokens", "usd"}
        if unknown:
            raise StateError(f"budget 只认 tokens / usd，多了 {sorted(unknown)}")
        tokens = raw.get("tokens")
        usd = raw.get("usd")
        if tokens is not None:
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0:
                raise StateError("budget.tokens 必须是正整数")
        if usd is not None:
            if isinstance(usd, bool) or not isinstance(usd, (int, float)) or usd <= 0:
                raise StateError("budget.usd 必须是正数")
            usd = float(usd)
        if tokens is None and usd is None:
            return None
        return cls(tokens=tokens, usd=usd)

    def to_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "usd": self.usd}


@dataclass(frozen=True)
class Spend:
    """一个会话到目前为止的累计用量（按 `Usage.snapshot()` 结算）。

    usd 为 None = 算不出来（没有 pricing，或有模型没定价）；unpriced 列出没
    定价的模型，预算判定与状态接口都要把它如实暴露出去。
    """

    tokens: int
    usd: float | None
    unpriced: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"tokens": self.tokens, "usd": self.usd, "unpriced": list(self.unpriced)}


def check_pricing(raw: Any) -> dict[str, dict[str, float]]:
    """校验并归一 pricing：{模型: {"input": 每百万 token 美元, "output": ...}}。

    键可以是 `provider/model` 全限定名，也可以是裸 model 名（兜底匹配，见
    `price_for`）。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise StateError('pricing 必须是对象：{"provider/model": {"input": 5, "output": 25}}')
    result: dict[str, dict[str, float]] = {}
    for model, entry in raw.items():
        if not isinstance(model, str) or not model:
            raise StateError("pricing 的键必须是非空模型名")
        if not isinstance(entry, dict) or set(entry) != {"input", "output"}:
            raise StateError(f"pricing[{model!r}] 必须形如 {{\"input\": 美元/百万, \"output\": 美元/百万}}")
        prices: dict[str, float] = {}
        for key in ("input", "output"):
            value = entry[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise StateError(f"pricing[{model!r}].{key} 必须是非负数")
            prices[key] = float(value)
        result[model] = prices
    return result


def price_for(pricing: dict[str, dict[str, float]], model: str) -> dict[str, float] | None:
    """先按全限定名精确匹配，再按裸模型名（去掉 provider 前缀）兜底。"""
    if model in pricing:
        return pricing[model]
    bare = model.rsplit("/", 1)[-1]
    return pricing.get(bare)


def spend_of(
    snapshot: dict[str, tuple[int, int, int]],
    pricing: dict[str, dict[str, float]],
    want_usd: bool,
) -> Spend:
    """`Usage.snapshot()` → Spend。want_usd=False 时不算钱（也不报未定价）。"""
    tokens = 0
    usd = 0.0
    unpriced: list[str] = []
    for model, (_calls, prompt, completion) in snapshot.items():
        tokens += prompt + completion
        if not want_usd:
            continue
        prices = price_for(pricing, model)
        if prices is None:
            unpriced.append(model)
            continue
        usd += prompt / 1_000_000 * prices["input"] + completion / 1_000_000 * prices["output"]
    if not want_usd:
        return Spend(tokens=tokens, usd=None)
    return Spend(tokens=tokens, usd=None if unpriced else round(usd, 6), unpriced=tuple(unpriced))


def budget_breach(budget: Budget | None, spend: Spend) -> str:
    """预算是否已耗尽：返回人读得懂的原因；空串 = 没超。

    美元预算下有未定价模型 → 视为超支。这是钱的 fail closed：宁可多停一次
    让运维补定价，不能让"算不出来"悄悄变成"不设限"。
    """
    if budget is None:
        return ""
    if budget.tokens is not None and spend.tokens >= budget.tokens:
        return f"token 用量 {spend.tokens} 已达预算 {budget.tokens}"
    if budget.usd is not None:
        if spend.unpriced:
            return (
                f"美元预算下用到了没有定价的模型 {', '.join(spend.unpriced)}"
                "（按超支处理；在 agent 的 pricing 里补上定价后再继续）"
            )
        if spend.usd is not None and spend.usd >= budget.usd:
            return f"花费 ${spend.usd:.4f} 已达预算 ${budget.usd:.4f}"
    return ""


# ---------- 持久化基座 ----------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """原子写：先写临时文件再 rename，进程中途死掉不会留下半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------- Agent 对象 ----------


def check_agent_config(raw: dict[str, Any]) -> dict[str, Any]:
    """校验一份 agent 配置（创建与更新共用），返回归一后的副本。

    只做**形状**校验；"能不能比服务端更松"那条安全规则在 serve.py 里判
    （那里才知道服务端的启动参数）。
    """
    unknown = set(raw) - set(AGENT_CONFIG_KEYS)
    if unknown:
        raise StateError(f"agent 配置不认识这些键：{sorted(unknown)}；可用：{list(AGENT_CONFIG_KEYS)}")
    config: dict[str, Any] = {}
    for key in ("model", "base_url", "mode", "append_system_prompt"):
        value = raw.get(key, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise StateError(f"{key} 必须是字符串")
        config[key] = value
    approval = raw.get("approval", "") or ""
    if approval not in ("", "ask", "allow_all"):
        raise StateError("approval 只能是 ask 或 allow_all")
    config["approval"] = approval
    for key in ("sandbox", "sandbox_network"):
        value = raw.get(key)
        if value is not None and not isinstance(value, bool):
            raise StateError(f"{key} 只能是 true / false / null")
        config[key] = value
    budget = Budget.parse(raw.get("budget"))
    config["budget"] = budget.to_dict() if budget else None
    config["pricing"] = check_pricing(raw.get("pricing"))
    return config


def check_name(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise StateError("name 不能为空")
    name = raw.strip()
    if len(name) > MAX_NAME:
        raise StateError(f"name 最长 {MAX_NAME} 字符")
    return name


class AgentStore:
    """Agent 对象的注册表。directory=None 时只在内存里（测试 / --no-persist）。

    一个 agent 一个文件：{agent_id, name, version, archived, created_at,
    updated_at, versions: [{version, config, created_at}, ...]}。`config`
    永远是最新版本的副本（读最新不必翻列表）。
    """

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory
        self._agents: dict[str, dict[str, Any]] = {}
        if directory is not None and directory.is_dir():
            for path in sorted(directory.glob("agent-*.json")):
                record = _read_json(path)
                if record and isinstance(record.get("agent_id"), str):
                    self._agents[record["agent_id"]] = record

    def _save(self, record: dict[str, Any]) -> None:
        if self.directory is not None:
            _write_json(self.directory / f"{record['agent_id']}.json", record)

    def create(self, name: Any, config: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        record = {
            "agent_id": f"agent-{uuid.uuid4().hex[:12]}",
            "name": check_name(name),
            "version": 1,
            "archived": False,
            "created_at": now,
            "updated_at": now,
            "config": check_agent_config(config),
        }
        record["versions"] = [{"version": 1, "config": dict(record["config"]), "created_at": now}]
        self._agents[record["agent_id"]] = record
        self._save(record)
        return record

    def get(self, agent_id: str) -> dict[str, Any]:
        record = self._agents.get(agent_id)
        if record is None:
            raise NotFound(agent_id)
        return record

    def list(self) -> list[dict[str, Any]]:
        return sorted(self._agents.values(), key=lambda item: item["created_at"])

    def update(self, agent_id: str, patch: dict[str, Any], name: Any = None) -> dict[str, Any]:
        """出一个新版本：patch 里给的键覆盖，没给的沿用上一版。

        整份重新过一遍校验（而不是只校验 patch）：合并后的配置才是会被用的那份。
        """
        record = self.get(agent_id)
        if record["archived"]:
            raise StateError(f"agent {agent_id} 已归档，不能再更新")
        unknown = set(patch) - set(AGENT_CONFIG_KEYS)
        if unknown:
            raise StateError(f"agent 配置不认识这些键：{sorted(unknown)}")
        merged = {**record["config"], **patch}
        config = check_agent_config(merged)
        now = time.time()
        record["version"] += 1
        record["config"] = config
        record["updated_at"] = now
        if name is not None:
            record["name"] = check_name(name)
        record["versions"].append({"version": record["version"], "config": dict(config), "created_at": now})
        self._save(record)
        return record

    def archive(self, agent_id: str) -> dict[str, Any]:
        """归档 = 只读且不再接受新会话；已在跑的会话不受影响（它们钉的是版本副本）。
        不提供删除：删了之后老会话清单里的引用就悬空了，归档才是可审计的收尾。"""
        record = self.get(agent_id)
        record["archived"] = True
        record["updated_at"] = time.time()
        self._save(record)
        return record

    def resolve(self, ref: Any) -> tuple[dict[str, Any], int, dict[str, Any]]:
        """会话创建时的 `agent` 字段 → (agent 记录, 钉住的版本号, 该版本配置)。

        字符串 = 最新版本；{"id": ..., "version": n} = 钉到指定版本。归档的 agent
        拒绝新会话——与 Managed Agents 的语义一致，也是"归档"这个词该有的意思。
        """
        if isinstance(ref, str):
            agent_id, version = ref, None
        elif isinstance(ref, dict) and isinstance(ref.get("id"), str):
            agent_id, version = ref["id"], ref.get("version")
            if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
                raise StateError("agent.version 必须是整数")
        else:
            raise StateError('agent 必须是 agent_id 字符串或 {"id": ..., "version": n}')
        record = self.get(agent_id)
        if record["archived"]:
            raise StateError(f"agent {agent_id} 已归档，不接受新会话")
        if version is None:
            version = record["version"]
        for entry in record["versions"]:
            if entry["version"] == version:
                return record, version, dict(entry["config"])
        raise NotFound(f"{agent_id}@{version}")


def public_agent(record: dict[str, Any], with_versions: bool = False) -> dict[str, Any]:
    """对外形态：默认不带 versions 列表（列表接口不该把全部历史一起吐出去）。"""
    view = {key: value for key, value in record.items() if key != "versions"}
    if with_versions:
        view["versions"] = record["versions"]
    return view


# ---------- 会话清单 ----------


class SessionStore:
    """会话清单：一会话一文件，内容是 serve 重启后重建该会话所需的全部信息。

    **不存事件缓冲**——事件是进程内的环形缓冲，量大且对话历史已在会话日志里
    （事件只是历史的另一种投影）。清单记下 `next_seq` 水位，重启后游标从那里
    **接着编号**、重启前的事件计入 dropped_events：客户端手里的游标仍然单调，
    拉到的是"中间缺了一段"而不是"序号倒流"——前者协议里本来就有表达
    （first_seq / dropped_events），后者没有。
    """

    def __init__(self, directory: Path | None) -> None:
        self.directory = directory

    def save(self, manifest: dict[str, Any]) -> None:
        if self.directory is not None:
            _write_json(self.directory / f"{manifest['session_id']}.json", manifest)

    def delete(self, session_id: str) -> None:
        if self.directory is None:
            return
        try:
            (self.directory / f"{session_id}.json").unlink()
        except FileNotFoundError:
            pass

    def load_all(self) -> list[dict[str, Any]]:
        if self.directory is None or not self.directory.is_dir():
            return []
        found: list[dict[str, Any]] = []
        for path in sorted(self.directory.glob("sess-*.json")):
            record = _read_json(path)
            if record and isinstance(record.get("session_id"), str):
                found.append(record)
        return found
