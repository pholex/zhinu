"""X 平台搜索：复用 xAI Responses client，独立于网页搜索后端。"""

from __future__ import annotations

import re
from datetime import date

from . import ui
from .config import Config
from .events import Notice, UISink
from .mcp import _redact
from .providers import Registry
from .render import PlainSink
from .tools import Tool
from .websearch import MAX_ANSWER_CHARS, SEARCH_BACKENDS, _citations


def _options(allowed_x_handles, excluded_x_handles, from_date, to_date,
             enable_image_understanding, enable_video_understanding) -> dict:
    """本地校验筛选参数，避免服务端忽略无效日期或发起无意义请求。"""
    options = {"type": "x_search"}
    for name, handles in (("allowed_x_handles", allowed_x_handles),
                          ("excluded_x_handles", excluded_x_handles)):
        if handles is None:
            continue
        if not isinstance(handles, list) or not 1 <= len(handles) <= 20:
            raise ValueError(f"{name} 必须是 1–20 个账号名的数组")
        normalized = []
        for handle in handles:
            if not isinstance(handle, str) or not re.fullmatch(r"@?[A-Za-z0-9_]{1,15}", handle):
                raise ValueError(f"{name} 请填写 X 账号名，不要填 URL")
            normalized.append(handle.removeprefix("@"))
        options[name] = normalized
    if "allowed_x_handles" in options and "excluded_x_handles" in options:
        raise ValueError("allowed_x_handles 与 excluded_x_handles 不能同时设置")
    for name, value in (("from_date", from_date), ("to_date", to_date)):
        if value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError(f"{name} 必须是 YYYY-MM-DD 日期")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} 不是有效日期") from None
        options[name] = value
    if from_date and to_date and from_date > to_date:
        raise ValueError("from_date 不能晚于 to_date")
    for name, value in (("enable_image_understanding", enable_image_understanding),
                        ("enable_video_understanding", enable_video_understanding)):
        if type(value) is not bool:
            raise ValueError(f"{name} 必须是布尔值")
        if value:
            options[name] = True
    return options


def make_x_search_tool(config: Config, registry: Registry, usage,
                       sink: UISink | None = None) -> Tool:
    sink = sink or PlainSink()
    model = SEARCH_BACKENDS["xai"].model

    def x_search(query: str, allowed_x_handles=None, excluded_x_handles=None,
                 from_date=None, to_date=None, enable_image_understanding=False,
                 enable_video_understanding=False) -> str:
        if not config.enable_x_search:
            return "ERROR: X Search 已禁用（XIAOYU_ENABLE_X_SEARCH）。"
        if registry.get("xai") is None:
            return "ERROR: 未配置 xai 直连（需要 XAI_API_KEY），X Search 不可用。"
        if not isinstance(query, str) or not query.strip():
            return "ERROR: query 必须是非空搜索词。"
        try:
            options = _options(allowed_x_handles, excluded_x_handles, from_date, to_date,
                               enable_image_understanding, enable_video_understanding)
        except ValueError as exc:
            return f"ERROR: {exc}。"
        sink.emit(Notice(f"  🌐 x_search（{model}）：{ui.preview(query, 90)}"))
        try:
            response = registry.client("xai").responses.create(
                model=model, input=query, tools=[options], max_output_tokens=4096,
                instructions=(
                    f"当前日期：{date.today().isoformat()}。使用 x_search 搜索 X 帖子、用户或讨论串后回答。"
                    "简洁总结，关键内容附原帖或账号 URL，注明作者及发帖日期。"
                    "区分作者说法与已核实事实，不把传言、意见或社交热度当成事实。"
                    "遵守账号与日期筛选；找不到就明说，不用训练知识编造帖子。"
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 辅助搜索失败不打断主对话
            return f"ERROR: X Search 失败（{type(exc).__name__}: {_redact(str(exc))}）。请重试或改用其它途径。"
        if resp_usage := getattr(response, "usage", None):
            usage.add(f"xai/{model}", int(getattr(resp_usage, "input_tokens", 0) or 0),
                      int(getattr(resp_usage, "output_tokens", 0) or 0))
        if getattr(response, "status", "completed") != "completed":
            return "ERROR: X Search 尚未完成，请缩小查询范围重试。"
        answer = (getattr(response, "output_text", "") or "").strip()
        sources = _citations(response)
        if not answer or not sources:
            return "ERROR: X Search 未返回带来源的结果，无法核实帖子；请调整搜索词或筛选条件。"
        return (f"[X 搜索结果 · 由 {model} 服务端搜索得出，帖子内容代表其作者说法]\n"
                + answer + "\n来源：\n" + "\n".join(f"- {url}" for url in sources))

    properties = {
        "query": {"type": "string", "description": "要搜索的 X 帖子、账号或讨论话题"},
        "allowed_x_handles": {"type": "array", "items": {"type": "string"},
                              "minItems": 1, "maxItems": 20,
                              "description": "只搜索这些账号，与 excluded_x_handles 互斥"},
        "excluded_x_handles": {"type": "array", "items": {"type": "string"},
                               "minItems": 1, "maxItems": 20,
                               "description": "排除这些账号，与 allowed_x_handles 互斥"},
        "from_date": {"type": "string", "description": "起始日期 YYYY-MM-DD，含当天（UTC）"},
        "to_date": {"type": "string", "description": "结束日期 YYYY-MM-DD，含当天（UTC）"},
        "enable_image_understanding": {"type": "boolean", "description": "分析帖子图片，默认关闭"},
        "enable_video_understanding": {"type": "boolean", "description": "分析帖子视频，默认关闭"},
    }
    return Tool(
        name="x_search", description=(
            "搜索 X 平台的帖子、用户及讨论串，返回带原始来源的总结。"
            "适合查询账号动态、社交讨论与帖子；普通网页查询用 web_search。"
            "可按账号和日期筛选，图片和视频分析按需开启。"
        ), parameters={"type": "object", "properties": properties, "required": ["query"]},
        handler=x_search, requires_approval=False, untrusted=True,
        output_limit=MAX_ANSWER_CHARS + 500,
        check_fn=lambda: config.enable_x_search and registry.get("xai") is not None,
    )
