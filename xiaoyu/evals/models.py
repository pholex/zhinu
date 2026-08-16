"""候选模型清单与单价。

数据不入库——候选集和单价都跟着你用的端点走，别人的那份对你没意义。
按顺序找：`$XIAOYU_EVAL_MODELS` → 同目录 `models.local.json` → 随包分发的
`models.example.json`（占位示例，保证没配过也能跑起来看到表格长什么样）。
照着 `models.example.json` 复制一份改名 `models.local.json` 即可。

⚠️ 成本一律按「本地 token 计数 × 单价」算，别用端点侧回报的 spend：
有些模型压根不回 usage，那边的账面接近 $0，而 token 照样海量。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL_PATH = HERE / "models.local.json"
EXAMPLE_PATH = HERE / "models.example.json"
ENV_VAR = "XIAOYU_EVAL_MODELS"


@dataclass(frozen=True)
class Candidate:
    name: str
    family: str
    #  给人看的定位，不参与计算
    note: str = ""


def data_path() -> Path:
    """当前生效的数据文件。找不到本机那份就退到示例，永远返回一个存在的路径。"""
    if env := os.environ.get(ENV_VAR):
        candidate = Path(env).expanduser()
        if candidate.is_file():
            return candidate
    return LOCAL_PATH if LOCAL_PATH.is_file() else EXAMPLE_PATH


def load(path: Path | None = None) -> tuple[list[Candidate], dict[str, dict[str, float]]]:
    """读一份数据文件，返回（候选清单, 单价表）。文件坏了当空处理，不让 eval 起不来。"""
    try:
        raw = json.loads((path or data_path()).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    candidates: list[Candidate] = []
    prices: dict[str, dict[str, float]] = {}
    for entry in raw.get("models", []):
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        candidates.append(
            Candidate(name, str(entry.get("family", "")), str(entry.get("note", "")))
        )
        if isinstance(entry.get("in"), (int, float)) and isinstance(
            entry.get("out"), (int, float)
        ):
            prices[name] = {"in": float(entry["in"]), "out": float(entry["out"])}
    return candidates, prices


#  顺序即报告里的展示顺序
CANDIDATES, PRICES = load()
CANDIDATE_NAMES: list[str] = [candidate.name for candidate in CANDIDATES]


def _price_of(model: str, prices: dict[str, dict[str, float]] | None = None) -> dict[str, float] | None:
    """查单价。带 provider 前缀的全限定名（`deepseek/deepseek-v4-pro`）也要能查到——
    Usage 的 key 自多 provider 起就是全限定名了，这张表还是按裸名建的。

    ⚠️ 这只是「查得到」的容错，不是「算得准」：直连和网关同一个模型单价并不相同，
    这里一律按表里那份算。真要分开算价，得给 Preset 配单价表（当前不做，
    单价表只服务 eval 报告，eval 走单端点路径）。
    """
    table = PRICES if prices is None else prices
    if price := table.get(model):
        return price
    _, sep, bare = model.partition("/")
    return table.get(bare) if sep else None


def cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    prices: dict[str, dict[str, float]] | None = None,
) -> float | None:
    """按本地 token 计数算美元成本。没有单价数据返回 None。"""
    price = _price_of(model, prices)
    if not price:
        return None
    return prompt_tokens * price["in"] + completion_tokens * price["out"]


def format_cost(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.3f}"


def relative_price(
    model: str, prices: dict[str, dict[str, float]] | None = None
) -> float | None:
    """相对最便宜候选的输入单价倍数，用来一眼看出贵多少。"""
    table = PRICES if prices is None else prices
    price = _price_of(model, table)
    if not price or not table:
        return None
    cheapest = min(entry["in"] for entry in table.values())
    if cheapest <= 0:
        return None
    return price["in"] / cheapest
