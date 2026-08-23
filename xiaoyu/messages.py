"""把 Anthropic Messages API 包成 chat completions 的形状（第三只鸭子）。

**为什么必须有这一层**：直连 Claude 从前走官方 OpenAI 兼容端点，而 Anthropic
自己把它定位为"评估用、非生产"。实测它缺的恰好是 agent 负载最值钱的东西：
prompt caching 完全不支持（每轮全价重发 system + 工具 schema + 越滚越长的历史）、
`reasoning_effort` 被静默忽略（对 Claude 完全没有推理深度控制）、thinking 过程
不回传（多轮工具循环推理状态断裂）。原生 Messages 协议三样都有：缓存读打一折、
effort 可控、thinking blocks 可回放。这也是日后 Bedrock Mantle 的前置——它上面
的 Claude 只挂原生协议（见 providers.py PRESETS 尾部注释）。

**为什么不动内核**：与 responses.py 完全同一套路。`Registry.client()` 返回的
本来就是鸭子型对象，这里加的是第三只：外面看是 chat completions，里面说
Messages。翻译发生在**发出去的那一刻**，不落进历史——`agent.messages` 仍是
chat-completions 形态，压缩/落盘/resume/跨协议 `/model` 切换全部照旧。

**thinking 回传**：直接复用 `_reasoning` 私有键（responses.py 的 REASONING_KEY）
——agent 收集 `chunk.reasoning` 挂到 assistant 消息上的管线一行都不用改。
thinking block 必须**原样字节回放**（服务端校验 signature），且只回给产出它的
同一型号；display 默认 omitted 时 thinking 文本为空但 signature 仍在，照样要
攒块回放，不能按"有无文本"过滤。

**缓存断点**：在这一层自动注入两个 `cache_control`（上限 4）——system 块一个
（tools 在 system 之前渲染，一个断点盖住两者），最终消息的最后一个 block 一个
（多轮增量缓存）。低于模型最小可缓存前缀（512~4096 token 按模型）时断点静默
不生效，无害。

**依赖的内核不变量**（改内核时留意）：
- 孤儿 tool_result（没有配对 tool_use）在 Messages 侧是硬 400——靠
  `agent._repair_history` 在每次请求前清理，这里不再兜；
- 尾部 assistant 消息（prefill）在 Claude 4.6+ 硬 400——内核每次请求都以
  user/tool 消息收尾，不会产生；scripted/手工回放的历史需自行保证。
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from .responses import (
    OPERATOR_KEY,
    REASONING_KEY,
    Chunk,
    Completion,
    Function,
    Message,
    NonStreamChoice,
    Usage,
    _text_chunk,
    _tool_chunk,
)

#  Messages 协议 max_tokens 必填，而内核从不传（chat 侧可省略）。
#  流式给大：Claude 5 线 thinking 默认开启且计入 max_tokens，余量要留足；
#  非流式（_summarize）给小：SDK 对超大的非流式请求有 ~10 分钟护栏会直接拒
_STREAM_MAX_TOKENS = 64_000
_SYNC_MAX_TOKENS = 16_000

#  chat 参数名 → Messages 参数名。没列出的原样透传：宁可上游 400 也不静默丢
_PARAM_ALIASES = {"max_completion_tokens": "max_tokens"}

#  刻意丢弃的参数——与"未知参数透传"相反，这几个是**已知必炸**：Claude 5 线
#  对采样参数硬 400（官方迁移指南明文），丢掉严格好于透传。stream_options
#  只有 include_usage 一个用途，Messages 的 usage 一定随流回来，吃掉即可
_DROPPED_PARAMS = ("temperature", "top_p", "top_k", "stream_options")

_CACHE_CONTROL = {"type": "ephemeral"}

#  服务端压缩（beta）。内核用私有键 COMPACTION_KEY 表达"请在 N 输入 token 时
#  压缩"，这里翻成 context_management + beta 头；compaction 块作为 reasoning
#  item 存在 assistant 消息上（与 thinking 同一条管线），下轮原样回放。
#  服务端自动忽略块之前的全部内容，所以本地历史不必真删。
COMPACTION_KEY = "_compaction"
COMPACTION_BETA = "compact-2026-01-12"
COMPACTION_EDIT = "compact_20260112"
#  服务端硬下限：trigger 低于它是 400
COMPACTION_MIN_TRIGGER = 50_000
#  支持 compact_20260112 的型号（官方清单，裸名子串匹配：带日期后缀也命中）
_COMPACTION_MODELS = (
    "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
    "claude-sonnet-4-6", "claude-sonnet-5",
    "claude-fable-5", "claude-mythos",
)


#  任务预算（beta）：服务端在生成过程中给模型注入倒计时标记（只模型可见），
#  内核私有键 TASK_BUDGET_KEY {total, remaining?} → output_config.task_budget。
#  官方下限 20k；支持型号 Opus 5 / 4.8 / 4.7 / Fable / Mythos / Sonnet 5
TASK_BUDGET_KEY = "_task_budget"
TASK_BUDGET_BETA = "task-budgets-2026-03-13"
TASK_BUDGET_MIN = 20_000
_TASK_BUDGET_MODELS = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-fable-5", "claude-mythos", "claude-sonnet-5",
)


def supports_task_budget(model: str) -> bool:
    bare = model.rpartition("/")[2].lower()
    return any(bare.startswith(name) for name in _TASK_BUDGET_MODELS)


#  认会话中 `role: system` 的型号（官方：Opus 5 / Opus 4.8 / Fable 5 / Mythos；
#  Sonnet 5 与 4.7 以下不认，发了是 400）。保守：不在清单一律当 user 发
_MID_SYSTEM_MODELS = ("claude-opus-5", "claude-opus-4-8", "claude-fable-5", "claude-mythos")


def supports_mid_system(model: str) -> bool:
    bare = model.rpartition("/")[2].lower()
    return any(bare.startswith(name) for name in _MID_SYSTEM_MODELS)


def supports_server_compaction(model: str) -> bool:
    """型号是否在服务端压缩的支持清单里。保守：不认识的一律 False，
    传了不支持的型号是硬 400，比少压一次贵得多。"""
    bare = model.rpartition("/")[2].lower()
    return any(bare.startswith(name) for name in _COMPACTION_MODELS)


# ---------- 请求方向：chat completions → Messages ----------


def _split_system(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """摘出 system 文本。内核只在 messages[0] 产生 system，但防御性地收齐全部
    （拼接顺序保持原序）——Messages 只认顶层 system 参数，消息流里出现会 400。"""
    from . import media

    texts: list[str] = []
    rest: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == "system":
            if text := media.text_of(message.get("content")):
                texts.append(text)
        else:
            rest.append(message)
    return "\n\n".join(texts), rest


def _load_arguments(raw: Any) -> dict[str, Any]:
    """tool_calls 的 arguments 是 JSON **字符串**，Messages 的 tool_use.input
    要 dict。坏 JSON / 空串 / 非对象一律 {}——模型偶尔吐半截 JSON，
    炸在翻译层不如让上游按空参数执行后自己发现。"""
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _image_block(url: str) -> dict[str, Any]:
    """chat 的 image_url（media.inline 之后是 data URL 或外链）→ Messages 的
    image block。拆不开的 data URL 走 url source 兜底——让上游给出可读的报错，
    而不是在这里炸（与 media.data_url 对缺失缓存文件的处理哲学一致）。"""
    if url.startswith("data:"):
        header, sep, payload = url.partition(";base64,")
        if sep:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": header[len("data:") :],
                    "data": payload,
                },
            }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _to_blocks(content: Any) -> list[dict[str, Any]]:
    """user 内容部件 → Messages content blocks。"""
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "image_url":
            if url := (part.get("image_url") or {}).get("url", ""):
                blocks.append(_image_block(url))
        elif text := str(part.get("text") or ""):
            blocks.append({"type": "text", "text": text})
    return blocks


def _assistant_blocks(
    message: dict[str, Any], model: str, provider: str = ""
) -> list[dict[str, Any]]:
    """assistant 消息 → content blocks，顺序是硬要求：thinking 在最前
    （服务端要求 thinking 先于它引出的 text/tool_use），然后正文，然后工具调用。

    thinking 回放只认**产出它的同一路由**（provider+model 都对上才回）：
    signature 是模型/服务侧私有状态，切模型不能回放；同名模型跑在两家上
    （直连 vs 网关兜底）跨家回放同样过不了校验。且必须原样引用存储的
    dict——重建/改字段都会过不了服务端校验。旧会话存量无 provider 标签，
    按不匹配处理（丢 reasoning 无害）。
    """
    blocks: list[dict[str, Any]] = []
    reasoning = message.get(REASONING_KEY) or {}
    items = reasoning.get("items") or []
    if reasoning.get("model") == model and (reasoning.get("provider") or "") == provider:
        blocks.extend(items)
    elif (reasoning.get("provider") or "") == provider:
        #  compaction 块不绑型号（官方：任一支持的型号都认），同家切模型照样回放；
        #  thinking 仍只回给产出它的型号
        blocks.extend(item for item in items if item.get("type") == "compaction")
    content = message.get("content")
    if isinstance(content, str):
        #  Anthropic 拒绝空/纯空白 text block，chat 侧却允许——静默跳过
        if content.strip():
            blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        blocks.extend(_to_blocks(content))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "input": _load_arguments(function.get("arguments")),
            }
        )
    return blocks


def _place_operators(converted: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把暂存的 operator 条目（role=system）放到服务端认可的位置，放不下的
    折回 user。约束（官方）：不能是第一条；前一条必须是 user（tool_result 合并
    后的 user 也算）；后一条必须是 assistant 或它就是最后一条。

    放置策略：后面紧跟一条 user（典型：两轮之间切了档，再来新提问）就把它
    **移到那条 user 之后**——"规则变了"在模型作答前看到即可，先后无伤；
    仍不满足就折成 user 文本（与旧行为一个字节不差）。相邻的 operator 条目
    合成一条 system（官方未说允许连发，不赌）。
    """
    placed: list[dict[str, Any]] = []
    pending_ops: list[str] = []

    def flush(next_role: str | None) -> None:
        if not pending_ops:
            return
        text = "\n\n".join(pending_ops)
        pending_ops.clear()
        prev_is_user = bool(placed) and placed[-1]["role"] == "user"
        if prev_is_user and next_role in (None, "assistant"):
            placed.append({"role": "system", "content": text})
        else:
            placed.append({"role": "user", "content": text})

    for entry in converted:
        if entry["role"] == "system":
            pending_ops.append(entry["content"])
            continue
        if entry["role"] == "user" and pending_ops:
            #  后面紧跟 user：先放这条 user，operator 挪到它后面再定夺
            placed.append(entry)
            continue
        flush(entry["role"])
        placed.append(entry)
    flush(None)
    return placed


def _to_messages(
    messages: list[dict[str, Any]], model: str, provider: str = ""
) -> list[dict[str, Any]]:
    """（已摘掉 system 的）消息列表 → Messages 形态。

    关键差异：`role: tool` 在 Messages 里是 user 消息中的 tool_result block，
    且**同一轮的所有 tool_result 必须并入同一条 user 消息**（分开发是硬 400）
    ——内核是一次调用一条 tool 消息，这里把连续的 tool 消息合并。

    翻空了的消息（纯空白 assistant、无有效部件的 user）整条丢弃：
    相邻同角色消息合法（服务端自动并轮），丢弃不会造成 user/assistant 断序。
    """
    converted: list[dict[str, Any]] = []
    previous_was_tool = False
    mid_system = supports_mid_system(model)
    for message in messages:
        role = message.get("role")
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                #  内核的工具结果一定是纯字符串（图片走随后的独立 user 消息）
                "content": message.get("content") or "",
            }
            if previous_was_tool and converted:
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            previous_was_tool = True
            continue
        previous_was_tool = False
        if role == "assistant":
            if blocks := _assistant_blocks(message, model, provider):
                converted.append({"role": "assistant", "content": blocks})
            continue
        #  user：纯字符串直通（最省，也合法）；部件列表逐个翻译
        content = message.get("content")
        if message.get(OPERATOR_KEY) and mid_system and isinstance(content, str) and content:
            #  operator 条目先暂存成 role=system，位置由 _place_operators 定
            converted.append({"role": "system", "content": content})
        elif isinstance(content, list):
            if blocks := _to_blocks(content):
                converted.append({"role": "user", "content": blocks})
        elif content:
            converted.append({"role": "user", "content": content})
    return _place_operators(converted) if mid_system else converted


def _apply_tail_cache(converted: list[dict[str, Any]]) -> None:
    """缓存断点 #2：最终消息的最后一个 block。多轮对话里它随轮次后移，
    前一轮的断点仍是有效读取点，命中随会话增长逐轮累积。

    只对 user 尾消息生效（内核每次请求都以 user/tool 收尾）：assistant 尾
    消息的 blocks 里可能有**原样回放的 thinking dict**，往上面写 cache_control
    等于篡改必须字节一致的东西。
    """
    #  尾部若是 operator 的 system 条目，断点打在它前面那条 user 上：
    #  前缀照样全部盖住，且不往 system 条目上挂 cache_control（官方未说认）
    index = len(converted) - 1
    while index >= 0 and converted[index]["role"] == "system":
        index -= 1
    if index < 0 or converted[index]["role"] != "user":
        return
    content = converted[index]["content"]
    if isinstance(content, str):
        converted[index]["content"] = content = [{"type": "text", "text": content}]
    if content:
        content[-1]["cache_control"] = dict(_CACHE_CONTROL)


def to_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """工具 schema：chat 的嵌套 `{"function": {...}}` → Messages 的平铺
    `{name, description, input_schema}`。非 function 条目原样透传（对齐
    responses.to_tools 的行为；今天没有 server tool 会走到这条路）。"""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function" or "function" not in tool:
            converted.append(tool)
            continue
        function = tool["function"]
        converted.append(
            {
                "name": function.get("name", ""),
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object"},
            }
        )
    return converted


def to_request(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    stream: bool,
    extra: dict[str, Any],
    provider: str = "",
) -> dict[str, Any]:
    """拼出 anthropic client.messages.create 的完整参数（stream 除外，
    由调用方作为独立实参传入，与 responses 一路的形态一致）。"""
    system_text, rest = _split_system(messages)
    converted = _to_messages(rest, model, provider)
    _apply_tail_cache(converted)

    passthrough = {k: v for k, v in extra.items() if k not in _DROPPED_PARAMS}
    #  effort：chat 侧叫 reasoning_effort，Messages 侧住在 output_config.effort
    #  （GA，无 beta 头）。与调用方已给的 output_config 合并而非覆盖——
    #  structured outputs 的 format 也住那里
    effort = passthrough.pop("reasoning_effort", None)
    compaction = passthrough.pop(COMPACTION_KEY, None)
    task_budget = passthrough.pop(TASK_BUDGET_KEY, None)
    max_tokens = None
    for key in ("max_tokens", "max_completion_tokens"):
        if key in passthrough:
            value = passthrough.pop(key)
            max_tokens = max_tokens or value

    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens or (_STREAM_MAX_TOKENS if stream else _SYNC_MAX_TOKENS),
        "messages": converted,
    }
    if system_text:
        #  断点 #1：system 块。tools 在 system 之前渲染，这一个断点盖住两者
        request["system"] = [
            {"type": "text", "text": system_text, "cache_control": dict(_CACHE_CONTROL)}
        ]
    if tools:
        request["tools"] = to_tools(tools)
    for key, value in passthrough.items():
        request[_PARAM_ALIASES.get(key, key)] = value
    if effort:
        output_config = dict(request.get("output_config") or {})
        output_config["effort"] = effort
        request["output_config"] = output_config
    if task_budget and int(task_budget.get("total") or 0) >= TASK_BUDGET_MIN:
        budget: dict[str, Any] = {"type": "tokens", "total": int(task_budget["total"])}
        if task_budget.get("remaining") is not None:
            budget["remaining"] = max(0, int(task_budget["remaining"]))
        output_config = dict(request.get("output_config") or {})
        output_config["task_budget"] = budget
        request["output_config"] = output_config
        request["betas"] = [*request.get("betas", []), TASK_BUDGET_BETA]
    if compaction:
        edit: dict[str, Any] = {
            "type": COMPACTION_EDIT,
            "trigger": {
                "type": "input_tokens",
                "value": max(COMPACTION_MIN_TRIGGER, int(compaction.get("trigger") or 0)),
            },
        }
        if instructions := compaction.get("instructions"):
            edit["instructions"] = instructions
        request["context_management"] = {"edits": [edit]}
        #  betas 非空 → Transport 改走 client.beta.messages（见 responses._dispatch）
        request["betas"] = [*request.get("betas", []), COMPACTION_BETA]
    return request


# ---------- 响应方向：Messages → chat completions ----------


def _refusal_detail(event: Any) -> str:
    """refusal 的可读详情：stop_details 里的 explanation/category 能拿多少拿多少。"""
    details = getattr(getattr(event, "delta", None), "stop_details", None)
    for attr in ("explanation", "category"):
        if value := getattr(details, attr, None):
            return str(value)
    return "安全分类器拒绝（无详情）"


def stream_chunks(events: Iterator[Any]) -> Iterator[Chunk]:
    """Messages 事件流 → chat completions chunk 流。

    分片下标用 content block 的 index：同一条消息里唯一且保序（text/thinking
    与 tool_use 共享下标空间，有空洞没关系——内核按 index 攒分片、按 index 排序）。

    usage 的 prompt_tokens = input + cache_read + cache_creation：三个字段
    **合起来**才是真实 prompt 大小，内核的 token 记账锚点（agent._anchor）
    靠它估算上下文水位，只报 input_tokens 会让锚点在缓存命中时严重偏小。

    stop_reason 的处理与 responses 一路对齐：refusal 才抛（fatal，报到 REPL）；
    max_tokens / 上下文超限等截断**不抛**、保留半截——chat 遇到
    finish_reason=length 也是正常返回半截正文，抛了反而把已产出的内容全丢掉。
    """
    input_tokens = 0
    output_tokens = 0
    #  index → 攒块状态（thinking/redacted 攒内容，tool 记有没有见过参数分片）
    open_blocks: dict[int, dict[str, Any]] = {}

    for event in events:
        kind = getattr(event, "type", "")
        if kind == "message_start":
            usage = getattr(getattr(event, "message", None), "usage", None)
            input_tokens = (
                (getattr(usage, "input_tokens", 0) or 0)
                + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            )
        elif kind == "content_block_start":
            block = event.content_block
            block_type = getattr(block, "type", "")
            if block_type == "tool_use":
                #  id 和 name 在 start 事件一次给全（chat 侧也是一次给全），
                #  input 随后按 input_json_delta 分片累加
                open_blocks[event.index] = {"kind": "tool", "seen_args": False}
                yield _tool_chunk(
                    event.index,
                    id=getattr(block, "id", "") or "",
                    function=Function(name=getattr(block, "name", "") or "", arguments=""),
                )
            elif block_type == "thinking":
                open_blocks[event.index] = {
                    "kind": "thinking",
                    "thinking": getattr(block, "thinking", "") or "",
                    "signature": getattr(block, "signature", "") or "",
                }
            elif block_type == "redacted_thinking":
                open_blocks[event.index] = {
                    "kind": "redacted",
                    "data": getattr(block, "data", "") or "",
                }
            elif block_type == "compaction":
                open_blocks[event.index] = {
                    "kind": "compaction",
                    "content": getattr(block, "content", "") or "",
                }
        elif kind == "content_block_delta":
            delta = event.delta
            delta_type = getattr(delta, "type", "")
            if delta_type == "text_delta":
                yield _text_chunk(delta.text)
            elif delta_type == "input_json_delta":
                if state := open_blocks.get(event.index):
                    state["seen_args"] = True
                yield _tool_chunk(event.index, function=Function(arguments=delta.partial_json))
            elif delta_type == "thinking_delta":
                if state := open_blocks.get(event.index):
                    state["thinking"] += delta.thinking
            elif delta_type == "signature_delta":
                if state := open_blocks.get(event.index):
                    state["signature"] = delta.signature
            elif delta_type == "compaction_delta":
                #  摘要整段一次到（官方：不分片）；累加只为防御
                if state := open_blocks.get(event.index):
                    state["content"] += getattr(delta, "content", "") or ""
        elif kind == "content_block_stop":
            state = open_blocks.pop(getattr(event, "index", -1), None)
            if state is None:
                continue
            if state["kind"] == "thinking":
                #  display 默认 omitted 时 thinking 是空串、signature 仍在——
                #  照样回放，不按"有无文本"过滤。存纯 dict：会话日志要能 json 落盘
                yield Chunk(
                    reasoning=[
                        {
                            "type": "thinking",
                            "thinking": state["thinking"],
                            "signature": state["signature"],
                        }
                    ]
                )
            elif state["kind"] == "redacted":
                yield Chunk(reasoning=[{"type": "redacted_thinking", "data": state["data"]}])
            elif state["kind"] == "compaction":
                #  与 thinking 同一条私有键管线：挂在 assistant 消息上、落盘、回放。
                #  agent 按 type 识别它（见 Agent._compaction_floor）
                yield Chunk(reasoning=[{"type": "compaction", "content": state["content"]}])
            elif state["kind"] == "tool" and not state["seen_args"]:
                #  无参工具调用可能一个 input_json_delta 都不发：补一个 "{}"，
                #  否则内核攒出 arguments=""，下游 json.loads 会炸
                yield _tool_chunk(event.index, function=Function(arguments="{}"))
        elif kind == "message_delta":
            if usage := getattr(event, "usage", None):
                output_tokens = getattr(usage, "output_tokens", 0) or 0
            if getattr(getattr(event, "delta", None), "stop_reason", None) == "refusal":
                #  安全分类器拒绝：HTTP 200 但没有可用内容。抛出去落到
                #  errors.classify → fatal（不换模型不重试），详情报给用户
                raise RuntimeError(f"Anthropic 拒绝了这次请求：{_refusal_detail(event)}")
        elif kind == "message_stop":
            #  usage 收尾 chunk：choices 留空——内核见空 choices 就跳过，
            #  与 OpenAI chat 流最后那个纯 usage chunk 形状一致
            yield Chunk(usage=Usage(prompt_tokens=input_tokens, completion_tokens=output_tokens))
        #  ping / 未知事件一律忽略；流层错误由 anthropic SDK 自己抛类型化异常


def to_completion(response: Any) -> Completion:
    """非流式响应 → chat completions 形状（`_summarize` 走这条路）。

    refusal 在这里表现为空正文——_summarize 现有的空摘要守卫会自动换下一个模型。"""
    text = "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(response, "content", None) or [])
        if getattr(block, "type", "") == "text"
    )
    return Completion(
        choices=[NonStreamChoice(Message(content=text))],
        usage=_usage(getattr(response, "usage", None)),
    )


def _usage(raw: Any) -> Usage | None:
    if raw is None:
        return None
    return Usage(
        prompt_tokens=(
            (getattr(raw, "input_tokens", 0) or 0)
            + (getattr(raw, "cache_read_input_tokens", 0) or 0)
            + (getattr(raw, "cache_creation_input_tokens", 0) or 0)
        ),
        completion_tokens=getattr(raw, "output_tokens", 0) or 0,
    )


# ---------- client 工厂 ----------


def client(base_url: str, api_key: str, timeout: float) -> Any:
    """构造 anthropic SDK client。Transport 首次遇到 Messages 协议请求才调用
    （懒构造：不用 Claude 直连的进程不付 import 和连接池成本）。

    Provider.base_url 是 `.../v1`（OpenAI 兼容面的约定），而 anthropic SDK
    自己拼 `/v1/messages`——不剥掉尾部 /v1 会打到 /v1/v1/messages。

    max_retries=0 沿用全仓公约：重试全部收归 agent._stream_with_recovery 一层。
    """
    import anthropic

    base = base_url.rstrip("/").removesuffix("/v1")
    return anthropic.Anthropic(
        base_url=base or base_url,
        api_key=api_key,
        timeout=timeout,
        max_retries=0,
    )
