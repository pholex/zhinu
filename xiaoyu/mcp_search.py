"""MCP 工具检索：标识符分词 + BM25 + 精确名捷径。

MCP 工具不再全量进模型的 schema（见 tools.py 的 search_tool/use_tool）：
工具集因此在会话内保持稳定——新 server 中途连上不再作废 prompt cache 的
工具前缀，几百个工具的 schema 也不再吃掉上下文。检索语料是
「server 名 + 工具名 + 描述 + 参数名」，外加标识符拆词（SearchDashboards →
search dashboards、grafana-ai → grafana ai），模型用自然语言就能搜到。

索引每次搜索现建（几十到几百个工具、亚毫秒级）：不维护增量状态，
就没有状态不同步。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

#  BM25 标准参数
_K1 = 1.2
_B = 0.75

#  下划线算词内字符：save_issue 这种复合名整体保留一份，拆词另加
_ALNUM = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def split_identifier(token: str) -> list[str]:
    """标识符 → 词列表：按 __ / _ / - 切，再切 camelCase 边界。全大写缩写不拆。"""
    parts: list[str] = []
    for chunk in re.split(r"[_\-]+", token):
        if not chunk:
            continue
        parts.extend(piece for piece in _CAMEL.split(chunk) if piece)
    return parts


def tokenize(text: str) -> list[str]:
    """检索用分词：原词 + 拆词，全部小写。"""
    tokens: list[str] = []
    for match in _ALNUM.finditer(text):
        word = match.group()
        tokens.append(word.lower())
        pieces = split_identifier(word)
        if len(pieces) > 1:
            tokens.extend(piece.lower() for piece in pieces)
    return tokens


@dataclass(frozen=True)
class Entry:
    """一个可检索的工具。params 是参数名列表（schema properties 的键）。"""

    name: str  # 全限定名 mcp__<server>__<tool>
    server: str
    description: str
    parameters: dict[str, Any]

    def document(self) -> str:
        params = " ".join(param_names(self.parameters))
        return f"{self.server} {self.name} {self.description} {params}"


def param_names(schema: dict[str, Any]) -> list[str]:
    properties = schema.get("properties")
    return list(properties) if isinstance(properties, dict) else []


def bare_name(qualified: str) -> str:
    """mcp__server__tool → tool（没有 __ 分段就原样返回）。"""
    return qualified.rsplit("__", 1)[-1]


def search(entries: list[Entry], query: str, limit: int = 5) -> list[tuple[Entry, float]]:
    """按 BM25 排序返回前 limit 个 (entry, score)。

    精确名捷径：query 恰好是某工具的全限定名或裸名（大小写不敏感）
    时直接返回那一个，分数 1.0——模型抄着名字来查 schema 是最高频形态。
    """
    if not entries:
        return []
    needle = query.strip().lower()
    if needle:
        for entry in entries:
            if needle in (entry.name.lower(), bare_name(entry.name).lower()):
                return [(entry, 1.0)]

    #  查询里混着标识符时把拆词追加进去
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    docs = [tokenize(entry.document()) for entry in entries]
    avg_len = sum(len(doc) for doc in docs) / len(docs) or 1.0
    #  文档频率
    doc_freq: dict[str, int] = {}
    for doc in docs:
        for term in set(doc):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    total = len(docs)

    scored: list[tuple[Entry, float]] = []
    for entry, doc in zip(entries, docs):
        counts: dict[str, int] = {}
        for term in doc:
            counts[term] = counts.get(term, 0) + 1
        score = 0.0
        for term in query_tokens:
            frequency = counts.get(term, 0)
            if not frequency:
                continue
            df = doc_freq.get(term, 0)
            idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            score += idf * (
                frequency
                * (_K1 + 1.0)
                / (frequency + _K1 * (1.0 - _B + _B * len(doc) / avg_len))
            )
        if score > 0.0:
            scored.append((entry, score))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: max(1, limit)]
