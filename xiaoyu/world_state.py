"""世界状态差量：环境变了就告诉模型一次，只说变了的那部分。

system prompt 每会话只建一次（它是 prompt cache 的前缀资产，抖一下整段作废），
但会话进行中环境会变：/model 切了模型、Shift-Tab 换了档、技能目录落了新技能、
MCP server 重连后工具集变了、项目指令文件被改了、new_context 翻了篇。以前这些
要么没告诉模型，要么各处手写一条注入——每处一套措辞、一套判重逻辑。

这里统一成「节」（Section）：每节会做两件事——
- `snapshot(agent)`：把这一维环境拍成一个可 JSON 的 dict；
- `render(previous, current)`：给定模型上次看到的快照与现在的快照，决定要不要
  说、说什么。`previous is None` 表示"不知道模型上次看到了什么"（恢复会话），
  此时应完整描述当前值而不是假装模型记得。

`WorldState` 每步比对一次，把所有变了的节拼成**一条** `<world_state>` 注入，
然后把基线推进到当前快照。没变的步零输出；各节 snapshot 只看内存里已有的值，
唯一的例外是项目指令——每步重读那几个小文件取哈希（用户中途改 AGENTS.md
是真实场景，stat+read 几 KB 的代价可以接受）。基线随会话日志落盘，resume 时接回；
接不回就按"未知"处理——宁可多说一次，不赌模型记得。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:  # pragma: no cover
    from .agent import Agent

Snapshot = dict[str, Any]

#  注入文本的识别前缀：压缩与子代理蒸馏按它跳过（这是环境播报，不是用户原话）
TAG_OPEN = "<world_state>"
TAG_CLOSE = "</world_state>"
#  整条注入的字符上限：项目指令正文可能很长，超了就只给指针
RENDER_CAP = 2000
#  项目指令变更时随播报附带正文的上限；超过只说"已更新，请重读"
PROJECT_TEXT_CAP = 1200


class Section(Protocol):
    id: str

    def snapshot(self, agent: "Agent") -> Snapshot: ...

    def render(self, previous: Snapshot | None, current: Snapshot) -> str | None: ...


def _listed(names: list[str], cap: int = 12) -> str:
    shown = "、".join(names[:cap])
    return shown + (f" 等 {len(names)} 个" if len(names) > cap else "")


@dataclass
class ValueSection:
    """单值节：模型名、档位、工作目录、窗口编号这类"一个值"的环境维度。"""

    id: str
    read: Callable[["Agent"], Any]
    describe: Callable[[Any], str]
    #  恢复会话时要不要在"当前环境"全量块里复述（窗口编号这种一次性信息不必）
    describe_when_unknown: bool = True

    def snapshot(self, agent: "Agent") -> Snapshot:
        return {"value": self.read(agent)}

    def render(self, previous: Snapshot | None, current: Snapshot) -> str | None:
        if previous is None:
            return self.describe(current["value"]) if self.describe_when_unknown else None
        if previous.get("value") == current["value"]:
            return None
        return self.describe(current["value"])


@dataclass
class NamesSection:
    """名单节：技能、工具这类"一组名字"的维度，变更按增删播报。"""

    id: str
    read: Callable[["Agent"], list[str]]
    label: str

    def snapshot(self, agent: "Agent") -> Snapshot:
        return {"names": sorted(set(self.read(agent)))}

    def render(self, previous: Snapshot | None, current: Snapshot) -> str | None:
        names = list(current["names"])
        if previous is None:
            return f"当前可用{self.label}：{_listed(names) or '（无）'}"
        before = set(previous.get("names") or ())
        added = sorted(set(names) - before)
        removed = sorted(before - set(names))
        if not added and not removed:
            return None
        parts = []
        if added:
            parts.append(f"新增 {_listed(added)}")
        if removed:
            parts.append(f"移除 {_listed(removed)}")
        return f"{self.label}有变：" + "；".join(parts)


@dataclass
class ProjectInstructionsSection:
    """项目指令节：只存正文哈希，变了再把新正文（有预算）带给模型。"""

    id: str = "project_instructions"
    read: Callable[["Agent"], str] = lambda agent: agent._project_instructions()  # noqa: SLF001

    def snapshot(self, agent: "Agent") -> Snapshot:
        text = self.read(agent)
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12] if text else ""
        #  正文本身不进快照（快照要落盘、要比对，留哈希即可）；render 需要时
        #  从 agent 再读一次——只在变化的那一步发生
        return {"digest": digest, "_text": text}

    def render(self, previous: Snapshot | None, current: Snapshot) -> str | None:
        if previous is None:
            #  恢复会话：system prompt 是按当前文件新建的，模型已经看到，不重复
            return None
        if previous.get("digest") == current["digest"]:
            return None
        text = str(current.get("_text") or "")
        if not text:
            return "项目指令文件已移除；system prompt 里那份不再有效。"
        if len(text) > PROJECT_TEXT_CAP:
            return (
                "项目指令已更新（正文较长，未随本条附带）；与 system prompt 里那份"
                "不一致时以磁盘上的项目指令文件为准，需要时重新读取。"
            )
        return f"项目指令已更新，以下为新版（覆盖 system prompt 里那份）：\n{text.strip()}"


def persistable(snapshot: dict[str, Snapshot]) -> dict[str, Snapshot]:
    """去掉下划线开头的临时字段，得到可落盘、可比对的基线。"""
    return {
        section_id: {key: value for key, value in values.items() if not key.startswith("_")}
        for section_id, values in snapshot.items()
    }


def default_sections() -> list[Section]:
    from . import modes

    def mode_text(value: Any) -> str:
        name, ready = value
        return f"当前档位：{modes.label(name)}" + ("" if ready else "（沙箱不可用）")

    return [
        ValueSection("model", lambda a: a.config.model, lambda v: f"当前模型：{v}"),
        ValueSection(
            "mode",
            lambda a: [a.mode, bool(a.sandbox_ready())],
            mode_text,
        ),
        ValueSection(
            "cwd", lambda a: str(a.config.workspace), lambda v: f"当前工作目录：{v}"
        ),
        NamesSection("skills", lambda a: [item.name for item in a.skills], "技能"),
        NamesSection("tools", lambda a: list(a.toolbox.names()), "工具"),
        ProjectInstructionsSection(),
        ValueSection(
            "context_window",
            lambda a: a._context_window,  # noqa: SLF001
            lambda v: f"当前处于第 {v} 个上下文窗口",
            describe_when_unknown=False,
        ),
    ]


@dataclass
class WorldState:
    """基线 + 节清单。`baseline is None` = 不知道模型上次看到了什么。"""

    sections: list[Section] = field(default_factory=default_sections)
    baseline: dict[str, Snapshot] | None = None

    def capture(self, agent: "Agent") -> dict[str, Snapshot]:
        return {section.id: section.snapshot(agent) for section in self.sections}

    def adopt(self, agent: "Agent") -> None:
        """把当前环境设为基线而不播报：会话开头 system prompt 已经说过了。"""
        self.baseline = persistable(self.capture(agent))

    def diff(self, agent: "Agent") -> str | None:
        """比对并推进基线。有话要说返回一条带标签的注入文本，否则 None。"""
        current = self.capture(agent)
        unknown = self.baseline is None
        lines: list[str] = []
        for section in self.sections:
            previous = None if unknown else (self.baseline or {}).get(section.id)
            text = section.render(previous, current[section.id])
            if text:
                lines.append(text)
        self.baseline = persistable(current)
        if not lines:
            return None
        head = "以下是恢复会话后的当前环境：" if unknown else "环境有变（只列变了的项）："
        body = "\n".join(f"- {line}" for line in lines)
        text = f"{TAG_OPEN}\n{head}\n{body}\n不必回应本条。\n{TAG_CLOSE}"
        if len(text) > RENDER_CAP:
            text = text[: RENDER_CAP - len(TAG_CLOSE) - 4].rstrip() + "…\n" + TAG_CLOSE
        return text


def is_world_state_note(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(TAG_OPEN)


def dump_baseline(baseline: dict[str, Snapshot] | None) -> str | None:
    return None if baseline is None else json.dumps(baseline, ensure_ascii=False, sort_keys=True)
