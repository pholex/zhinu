"""snapshot 录制器：把一次真实模型会话收割成 scripted DSL fixture。

思想是 llm-replay（fixture = 持久化的会话转写；录制 =
真跑一次收割，回放 = keyless 重建每次调用的流式输出），载体按小羽体量适配：

**fixture 格式就是 scripted.py 的 DSL 脚本**（不是扩 session_log）。理由：
- 生产会话文件是消息级、供 resume 用，塞 chunk 级高频事件是为测试污染生产格式；
- 回放侧因此零新代码——XIAOYU_SCRIPTED_SCRIPTS 走的就是久经考验的 scripted 桩；
- DSL 人可读可手改："chunk 前就 throw 的 401"直接在脚本里写一行 `error:`
  即可表达，不需要另设 override 旁车一类的机制。

录制的是"内核读的面"（_consume_stream 实际消费的属性：delta.content、
tool_calls 分片、usage、reasoning），不是原始 SSE 字节——录制点在
wrap_transport 外侧，Responses/Anthropic 协议差异已被传输层抹平，录出来的
fixture 天然协议无关。

激活：XIAOYU_SNAPSHOT_RECORD=<场景目录>（providers.client() 构造时包一层）。
产物：<目录>/model.txt（DSL，一轮 = 一次 LLM 调用）+ request-<n>.json
（每次调用的请求头：model / system prompt / tool schemas，pinned header
对比用）。多次 create 按全局调用顺序追加——scripted 回放同样是进程级 FIFO，
主循环/摘要/子 agent 共用一条队列，顺序天然对齐，不需要
per-session 绑定。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

RECORD_ENV = "XIAOYU_SNAPSHOT_RECORD"


def dump_request_header(directory: Path, index: int, request: dict[str, Any]) -> None:
    """把一次 LLM 调用的请求头写成 request-<n>.json（scripted 捕获与录制共用）。

    只取三样：model、首条 system 消息正文、tool schemas——这是"改了 prompt /
    工具没 refresh 快照"要锁的全部表面。写失败静默吞掉（与 session_log
    同一纪律：诊断设施绝不影响被测会话本身）。
    """
    system = ""
    for message in request.get("messages") or []:
        if isinstance(message, dict) and message.get("role") == "system":
            system = str(message.get("content") or "")
            break
    record = {
        "model": request.get("model"),
        "system": system,
        "tools": request.get("tools"),
    }
    try:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"request-{index}.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError:
        pass


#  —— chunk → DSL 行 ——


def _dsl_text(content: str) -> str:
    #  恒 JSON 引号形态：换行、行首空白、`---`、`#` 开头的正文都原样保真
    return f"text: {json.dumps(content, ensure_ascii=False)}"


def chunk_lines(chunk: Any) -> list[str]:
    """一个流式 chunk → 若干 DSL 行（镜像 agent._consume_stream 读的属性）。

    一个 chunk 拆成多行意味着回放时变成多个 chunk——对消费方语义等价：
    累加器不关心属性怎么分布，只关心顺序。
    """
    lines: list[str] = []
    if items := getattr(chunk, "reasoning", None):
        lines.append(f"reasoning: {json.dumps(list(items), ensure_ascii=False)}")
    choices = getattr(chunk, "choices", None) or []
    delta = getattr(choices[0], "delta", None) if choices else None
    if delta is not None:
        if getattr(delta, "content", None):
            lines.append(_dsl_text(delta.content))
        for fragment in getattr(delta, "tool_calls", None) or []:
            part: dict[str, Any] = {"index": getattr(fragment, "index", 0) or 0}
            if getattr(fragment, "id", None):
                part["id"] = fragment.id
            function = getattr(fragment, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    part["name"] = function.name
                if getattr(function, "arguments", None):
                    part["arguments"] = function.arguments
            lines.append(f"tool_call_part: {json.dumps(part, ensure_ascii=False)}")
    if usage := getattr(chunk, "usage", None):
        payload = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }
        lines.append(f"usage: {json.dumps(payload, ensure_ascii=False)}")
    return lines


def response_lines(response: Any) -> list[str]:
    """非流式响应（摘要路径）→ DSL 行。"""
    lines: list[str] = []
    choices = getattr(response, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    if message is not None and getattr(message, "content", None):
        lines.append(_dsl_text(message.content))
    if usage := getattr(response, "usage", None):
        payload = {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
            "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
        }
        lines.append(f"usage: {json.dumps(payload, ensure_ascii=False)}")
    return lines


#  —— 录制 wrapper ——


class Recorder:
    """一个场景目录一个 Recorder：按全局调用顺序把每次 create 追加成一轮。

    进程内单例（见 _recorder）：Registry 可能构造多个 provider client，
    它们共享同一条录制流——回放侧的 scripted 队列也是进程级共享，两边对齐。
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.calls = 0
        self._started = False

    def next_index(self) -> int:
        self.calls += 1
        return self.calls

    def write_turn(self, index: int, stream: bool, lines: list[str]) -> None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "model.txt").open(
                "w" if not self._started else "a", encoding="utf-8"
            ) as handle:
                if not self._started:
                    handle.write("# 由 XIAOYU_SNAPSHOT_RECORD 录制；一轮 = 一次 LLM 调用\n")
                    self._started = True
                else:
                    handle.write("---\n")
                handle.write(f"# ── 调用 {index}（{'stream' if stream else 'non-stream'}）\n")
                for line in lines:
                    handle.write(line + "\n")
        except OSError:
            pass  # 录制坏了不影响真会话；缺轮的 fixture 回放时自会响亮失败


class RecordingClient:
    """包在（已含 Transport 的）真 client 最外侧，tee 每次 create 的请求头与输出。

    流中途的异常（限流 429、断连）记成 `error:` 行——429 场景因此可以直接
    从真实故障录制出来；错误文本本来就是 errors.classify 的分类输入。
    """

    def __init__(self, inner: Any, recorder: Recorder) -> None:
        self._inner = inner
        self._recorder = recorder
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _create(self, **request: Any) -> Any:
        index = self._recorder.next_index()
        dump_request_header(self._recorder.directory, index, request)
        streaming = bool(request.get("stream"))
        try:
            result = self._inner.chat.completions.create(**request)
        except Exception as exc:
            #  chunk 之前就抛（401 等）：这一轮就是一行 error
            self._recorder.write_turn(index, streaming, [f"error: {exc}"])
            raise
        if not streaming:
            self._recorder.write_turn(index, False, response_lines(result))
            return result
        return self._tee(result, index)

    def _tee(self, stream: Any, index: int) -> Iterator[Any]:
        lines: list[str] = []
        try:
            for chunk in stream:
                lines.extend(chunk_lines(chunk))
                yield chunk
        except Exception as exc:
            lines.append(f"error: {exc}")
            raise
        finally:
            #  finally 而非正常收尾：中断（Ctrl-C 关闭生成器）也要落盘已见部分
            self._recorder.write_turn(index, True, lines)


_recorder: Recorder | None = None


def maybe_record(client: Any) -> Any:
    """providers.client() 的挂点：录制开关开着就包一层，否则原样返回。"""
    global _recorder
    target = os.environ.get(RECORD_ENV, "").strip()
    if not target:
        return client
    if _recorder is None or _recorder.directory != Path(target):
        _recorder = Recorder(Path(target))
    return RecordingClient(client, _recorder)
