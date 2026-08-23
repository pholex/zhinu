"""文本工具调用（tool mode = text）：给不支持 function calling 的模型装上工具。

**场景**：本地 vLLM / Ollama 上的小模型、企业客户自带的老端点、某些"OpenAI 兼容"
但 `tools` 参数直接 400 或静默忽略的服务。内核每一轮都带工具 schema，这些端点
等于一轮都跑不了。

**为什么做在 Transport 这一层、而不是内核**：和 responses.py / messages.py 一个
道理——翻译发生在**出网那一刻**，不落进历史。`agent.messages` 仍是标准的
chat-completions 形态（assistant.tool_calls + role=tool 配对），压缩切点、历史
修复、会话日志、打转检测、降级链全部原样工作；同一份历史今天在文本协议上跑、
明天 /model 切到原生 function calling 的模型照样接得上。

**协议**（对模型的约定，见 protocol_note）：要调用工具就输出 ```tool_call 代码块，
块内一个 JSON 对象 `{"name": …, "arguments": {…}}`；也认部分开源模型训练
时见过的 `<tool_call>…</tool_call>` 标签。工具结果以 `<tool_result>` 包着、作为
user 消息回灌——role=tool 在不支持工具的 chat template 上多半直接报错。

**流式解析**的关键是"扣住可能是标记开头的尾巴"：正文逐 chunk 往外发，但凡缓冲区
尾部可能是某个标记的前缀（一个反引号、半个 `<tool_`）就先不发，等下一个 chunk
把它证实或证伪；真撞到标记就从那里起全部扣住，流结束时一次性解析成 tool_call
分片。用户看到的正文因此最多晚几个字符，而不会看到半截代码块闪过又消失。

**不做**：把"文本里的 JSON"解析成工具调用天生比原生协议脆（模型写错 JSON、
块后面又续写正文、把示例当真调用）。这一路是逃生舱，不是一等路线——原生
function calling 能用就别开它。解析失败的块原样留在正文里，不猜、不修。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Iterator

from . import media
from .responses import (
    Chunk,
    Completion,
    Message,
    NonStreamChoice,
    _text_chunk,
    _tool_chunk,
    strip_private,
)

#  工具模式的取值。也是通用兜底 provider 的 XIAOYU_PROVIDER_<NAME>_TOOLS 取值
NATIVE = "native"
TEXT = "text"

#  两种调用标记：我们教的代码块，以及部分开源模型训练时见过的 <tool_call> 标签
FENCE_OPEN = "```tool_call"
FENCE_CLOSE = "```"
TAG_OPEN = "<tool_call>"
TAG_CLOSE = "</tool_call>"
_MARKERS = (FENCE_OPEN, TAG_OPEN)

#  工具结果回灌时的包装标签
RESULT_OPEN = "<tool_result"
RESULT_CLOSE = "</tool_result>"


# ---------- 请求方向：协议说明 + 历史改写 ----------


def protocol_note(tools: list[dict[str, Any]]) -> str:
    """拼给 system prompt 的协议说明：规则 + 正反示例 + 工具清单。

    反例那一条（"没有输出 tool_call 却声称读过"）不是装饰：不会 function calling
    的模型最常见的失败就是**用嘴完成任务**——内核另有产物对账护栏兜底，但在
    源头说一次便宜得多。
    """
    lines = [
        "# 工具调用规则",
        "本接口不支持原生 function calling。需要调用工具时，在回复中输出如下代码块，"
        "每个代码块恰好一个调用；需要多个调用就输出多个代码块：",
        FENCE_OPEN,
        '{"name": "<工具名>", "arguments": {<参数对象>}}',
        FENCE_CLOSE,
        "要求：",
        "1. arguments 必须是合法 JSON 对象，严格按该工具的参数 schema 填写；",
        "2. 输出完代码块后立即停止，不要在代码块之后写任何文字——工具结果会以 "
        "<tool_result> 的形式发回给你，你在下一条回复里继续；",
        "3. 不需要工具时正常作答，不要输出代码块，也不要把示例当成真的调用；",
        "4. 绝不能在没有调用工具的情况下宣称已经读取/创建/修改了文件或执行了命令——"
        "没有 tool_call 就等于什么都没做。",
        "",
        "示例（正确）：",
        "用户：看看 main.py 里有什么",
        "助手：",
        FENCE_OPEN,
        '{"name": "read_file", "arguments": {"path": "main.py"}}',
        FENCE_CLOSE,
        "",
        "示例（错误，禁止）：",
        "助手：我已经读取了 main.py，内容是……（没有输出 tool_call 却声称读过）",
        "",
        "# 可用工具",
    ]
    for tool in tools:
        function = tool.get("function", tool)
        name = function.get("name", "")
        description = str(function.get("description", "")).strip()
        parameters = function.get("parameters") or {"type": "object", "properties": {}}
        lines.append(f"## {name}")
        if description:
            lines.append(description)
        lines.append("参数 schema：" + json.dumps(parameters, ensure_ascii=False))
    return "\n".join(lines)


def render_call(call: dict[str, Any]) -> str:
    """一条 tool_call → 模型自己的那种代码块（历史里它看到的是自己约定的写法）。"""
    function = call.get("function") or {}
    raw = function.get("arguments") or "{}"
    try:
        arguments = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        arguments = raw
    body = json.dumps({"name": function.get("name", ""), "arguments": arguments}, ensure_ascii=False)
    return f"{FENCE_OPEN}\n{body}\n{FENCE_CLOSE}"


def _append_text(content: Any, text: str) -> Any:
    """往 content 尾部接一段文本。content 可能是 None / 字符串 / 部件列表。"""
    if not text:
        return content
    if content is None or content == "":
        return text
    if isinstance(content, str):
        return f"{content}\n\n{text}"
    return [*content, {"type": "text", "text": f"\n\n{text}"}]


def to_text_messages(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """chat-completions 历史 → 纯文本历史。

    - system：有工具就把协议说明接在末尾（没有 system 就补一条在最前）；
    - assistant 带 tool_calls：正文 + 代码块，tool_calls 键删掉；
    - 连续的 role=tool：合并成一条 user 消息，每个结果包在 <tool_result> 里；
    - 其余原样（私有键一并摘掉——这一路没有任何协议会消费它们）。
    """
    messages = strip_private(messages)
    names: dict[str, str] = {}
    converted: list[dict[str, Any]] = []
    pending_results: list[str] = []

    def flush_results() -> None:
        if pending_results:
            converted.append({"role": "user", "content": "\n\n".join(pending_results)})
            pending_results.clear()

    for message in messages:
        role = message.get("role")
        if role == "tool":
            call_id = str(message.get("tool_call_id", ""))
            name = names.get(call_id, "")
            body = media.text_of(message.get("content"))
            pending_results.append(
                f'{RESULT_OPEN} name="{name}" id="{call_id}">\n{body}\n{RESULT_CLOSE}'
            )
            continue
        flush_results()
        if role == "assistant" and message.get("tool_calls"):
            blocks = []
            for call in message["tool_calls"]:
                names[str(call.get("id", ""))] = (call.get("function") or {}).get("name", "")
                blocks.append(render_call(call))
            rebuilt = {k: v for k, v in message.items() if k != "tool_calls"}
            rebuilt["content"] = _append_text(message.get("content"), "\n".join(blocks))
            converted.append(rebuilt)
            continue
        converted.append(message)
    flush_results()

    if tools:
        note = protocol_note(tools)
        if converted and converted[0].get("role") == "system":
            converted[0] = {
                **converted[0],
                "content": _append_text(converted[0].get("content"), note),
            }
        else:
            converted.insert(0, {"role": "system", "content": note})
    return converted


# ---------- 响应方向：正文里抠出调用 ----------


def _json_objects(text: str) -> list[Any]:
    """按花括号配平从文本里抠出所有顶层 JSON 对象（字符串里的括号不算）。
    配不平 / 解析失败的片段跳过——不猜不修，见模块注释。"""
    found: list[Any] = []
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            if depth:
                in_string = True
            continue
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                try:
                    found.append(json.loads(text[start : index + 1]))
                except json.JSONDecodeError:
                    pass
    return found


def _as_call(item: Any) -> dict[str, Any] | None:
    """一个 JSON 对象 → tool_call（chat completions 形状）。认几个常见别名；
    没有工具名的不算调用。"""
    if not isinstance(item, dict):
        return None
    name = item.get("name") or item.get("tool") or item.get("function")
    if isinstance(name, dict):  # {"function": {"name": …, "arguments": …}} 形状
        item, name = name, name.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    arguments: Any = None
    for key in ("arguments", "parameters", "input", "args"):
        if key in item:
            arguments = item[key]
            break
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            pass
    if not isinstance(arguments, dict):
        arguments = {} if arguments is None else {"value": arguments}
    return {
        "id": f"call_text_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {"name": name.strip(), "arguments": json.dumps(arguments, ensure_ascii=False)},
    }


def _blocks(text: str) -> list[tuple[int, int, str]]:
    """找出全部调用块：(起点, 终点, 块内正文)。两种标记混用也认；
    没闭合的块一直到文末（模型可能在闭合前就被 max_tokens 截断）。"""
    blocks: list[tuple[int, int, str]] = []
    cursor = 0
    while True:
        candidates = [
            (text.find(FENCE_OPEN, cursor), FENCE_OPEN, FENCE_CLOSE),
            (text.find(TAG_OPEN, cursor), TAG_OPEN, TAG_CLOSE),
        ]
        candidates = [item for item in candidates if item[0] >= 0]
        if not candidates:
            return blocks
        start, opener, closer = min(candidates)
        body_start = start + len(opener)
        end = text.find(closer, body_start)
        if end < 0:
            blocks.append((start, len(text), text[body_start:]))
            return blocks
        blocks.append((start, end + len(closer), text[body_start:end]))
        cursor = end + len(closer)


def parse_calls(text: str) -> tuple[str, list[dict[str, Any]]]:
    """正文 → (给用户看的正文, tool_calls)。

    第一个**可解析**的块之前的文字是正文；块与块之间、最后一个块之后的文字丢弃
    （协议要求块后停止，续写的多半是模型"替工具作答"的幻觉）。一个块也解析
    不出来就原样返回全文、零调用——让人看见模型到底写了什么。
    """
    calls: list[dict[str, Any]] = []
    first_start: int | None = None
    for start, _end, body in _blocks(text):
        parsed = [call for call in map(_as_call, _json_objects(body)) if call]
        if parsed and first_start is None:
            first_start = start
        calls.extend(parsed)
    if not calls:
        return text, []
    return text[: first_start or 0].rstrip(), calls


def _safe_prefix_end(buffer: str, emitted: int) -> tuple[int, bool]:
    """缓冲区里可以放心往外发的正文终点。返回 (终点, 是否已撞到完整标记)。

    撞到完整标记：终点就是标记起点，之后全部扣住。
    没撞到：扣住可能是某个标记前缀的尾巴（最长的那个）。
    """
    hits = [index for index in (buffer.find(marker, emitted) for marker in _MARKERS) if index >= 0]
    if hits:
        return min(hits), True
    hold = 0
    tail_window = buffer[emitted:]
    for marker in _MARKERS:
        for length in range(min(len(marker) - 1, len(tail_window)), 0, -1):
            if tail_window.endswith(marker[:length]):
                hold = max(hold, length)
                break
    return len(buffer) - hold, False


def stream_chunks(chunks: Iterator[Any]) -> Iterator[Chunk]:
    """上游的 chat chunk 流 → 正文分片照发、调用块结束时变成 tool_call 分片。

    usage / reasoning 这类没有正文的 chunk 原样放行（内核按 getattr 取，不关心
    来源）。上游若意外给了原生 tool_calls 分片也放行——那说明端点其实会
    function calling，配置开错了，但不该因此丢掉一次正确的调用。
    """
    buffer = ""
    emitted = 0
    holding = False
    for chunk in chunks:
        choices = getattr(chunk, "choices", None) or []
        delta = choices[0].delta if choices else None
        text = getattr(delta, "content", None) if delta is not None else None
        if not text:
            yield chunk
            continue
        buffer += text
        if holding:
            continue
        end, hit = _safe_prefix_end(buffer, emitted)
        if end > emitted:
            yield _text_chunk(buffer[emitted:end])
            emitted = end
        holding = hit
    if not holding:
        if len(buffer) > emitted:
            yield _text_chunk(buffer[emitted:])
        return
    lead, calls = parse_calls(buffer)
    if not calls:
        #  撞到标记却解析不出调用：全文当正文放出去（已发的部分不重发）
        if len(buffer) > emitted:
            yield _text_chunk(buffer[emitted:])
        return
    if len(lead) > emitted:
        yield _text_chunk(lead[emitted:])
    for index, call in enumerate(calls):
        yield _tool_chunk(
            index,
            id=call["id"],
            function=_function(call["function"]["name"], call["function"]["arguments"]),
        )


def _function(name: str, arguments: str) -> Any:
    from .responses import Function

    return Function(name=name, arguments=arguments)


def to_completion(response: Any) -> Completion:
    """非流式响应：正文解析成 message.tool_calls。usage 原样带过（字段名与 chat 一致）。"""
    choice = response.choices[0]
    text = media.text_of(getattr(choice.message, "content", None))
    lead, calls = parse_calls(text)
    message = Message(content=lead or None, tool_calls=calls or None)
    return Completion(choices=[NonStreamChoice(message)], usage=getattr(response, "usage", None))
