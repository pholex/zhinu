"""SKILL.md 技能：与 Anthropic / agentskills.io 规范同形态。

- 扫描目录（前者优先）：`~/.agents/skills/`（跨客户端规范库）、`<用户配置目录>/skills/`，
  以及插件包带来的 `<用户配置目录>/plugins/<包名>/skills/`（见 `plugins.py`）
- 每个技能一个目录，内含 `SKILL.md`：YAML frontmatter（name / description）+ markdown 正文
- 渐进披露：索引（名字 + 一句话描述）进 system prompt，
  正文由模型用 `skill` 工具按需加载——技能再多也不占常驻上下文
- frontmatter 用零依赖解析：只认 `---` 块里平铺的 `key: value`，够用就好
- 插件包里的技能带命名空间前缀（`aws-core:aws-cdk`）：两家插件各带一个同名技能
  也不会互相顶掉，模型看到的名字就是调用的名字
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from . import plugins, tokens
from .config import home_dir, user_config_dir

#  插件命名空间与技能名之间的分隔符（`<插件>:<技能>`，业界通行形态）
NAMESPACE_SEP = ":"


@dataclass(frozen=True)
class Skill:
    name: str  # 调用名；插件技能是 `<包名>:<技能名>`
    description: str
    path: Path  # SKILL.md 的完整路径
    plugin: str | None = None  # 来自哪个插件包；None = 散装技能目录


@dataclass(frozen=True)
class SkillSource:
    """一个扫描来源。`plugin` 非空则该目录下的技能名要加命名空间前缀。"""

    directory: Path
    plugin: str | None = None


def skill_dirs() -> list[Path]:
    """散装技能目录，靠前者优先（同名去重）。

    推不出 home（服务账户/容器随机 UID，见 `config.home_dir`）就跳过
    `~/.agents/skills` 这一来源——少一个技能目录是降级，构造 Agent 炸掉不是。
    """
    home = home_dir()
    dirs = [home / ".agents" / "skills"] if home is not None else []
    return [*dirs, user_config_dir() / "skills"]


def skill_sources() -> list[SkillSource]:
    """全部扫描来源：散装目录在前，插件包在后。

    插件排后面不是因为它次要，而是因为它带命名空间、本来就不会和散装技能撞名——
    排序只决定散装目录之间谁胜出。
    """
    sources = [SkillSource(directory) for directory in skill_dirs()]
    sources += [SkillSource(path, plugin=name) for name, path in plugins.installed_skill_dirs()]
    return sources


def parse_frontmatter(text: str) -> dict[str, str]:
    """提取首个 --- 块里的平铺 key: value。不是合法 frontmatter 就返回空。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    result: dict[str, str] = {}
    pending: str | None = None  # 正在收集多行值（>- / | 等块标量）的键
    collected: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            if pending:
                result[pending] = " ".join(collected).strip()
            return result
        if pending is not None:
            #  块标量的内容行有缩进；遇到顶格行则块结束，回落到普通解析
            if line.startswith((" ", "\t")) or not line.strip():
                if line.strip():
                    collected.append(line.strip())
                continue
            result[pending] = " ".join(collected).strip()
            pending, collected = None, []
        key, sep, value = line.partition(":")
        #  嵌套结构（如 metadata:）的子行有缩进，跳过——索引只需要顶层键
        if sep and key == key.lstrip() and key.strip():
            value = value.strip()
            if value in (">", ">-", ">+", "|", "|-", "|+"):
                #  YAML 块标量：收集后续缩进行，折叠成一行（索引只要一句话）
                pending, collected = key.strip(), []
                continue
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return {}  # 没有闭合的 --- 不算 frontmatter


def strip_frontmatter(text: str) -> str:
    """去掉 frontmatter，返回正文。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return "\n".join(lines[index + 1 :]).strip()
    return text


def scan_skills() -> list[Skill]:
    """扫描所有来源。同名技能第一个来源胜出，被盖掉的打一行 stderr。

    撞名以前是静默丢弃：装了两份同名技能时，模型加载到的是哪一份全凭目录顺序，
    而用户在 `/skills` 里只看得到一条——排查起来毫无线索。插件技能带命名空间，
    撞名只可能发生在散装目录之间，报出来的量很小。
    """
    found: dict[str, Skill] = {}
    for source in skill_sources():
        if not source.directory.is_dir():
            continue
        for skill_md in sorted(source.directory.glob("*/SKILL.md")):
            try:
                meta = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = (meta.get("name") or skill_md.parent.name).strip()
            if not name:
                continue
            if source.plugin:
                name = f"{source.plugin}{NAMESPACE_SEP}{name}"
            if name in found:
                print(
                    f"[技能 {name!r} 撞名：用 {found[name].path}，忽略 {skill_md}]",
                    file=sys.stderr,
                )
                continue
            found[name] = Skill(
                name=name,
                description=meta.get("description", "").strip(),
                path=skill_md,
                plugin=source.plugin,
            )
    return list(found.values())


def load_skill_body(skill: Skill) -> str:
    """技能正文（去 frontmatter）。读失败返回错误文本交给模型。"""
    try:
        return strip_frontmatter(skill.path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return f"ERROR: 读取技能失败：{exc}"


#  索引里单条描述的字符上限：
#  索引只用于"发现"，完整说明在 invoke 时才加载——冗长描述只会常驻浪费上下文。
DESCRIPTION_CAP = 250


#  预算不够时的说明。技能全在、只是描述变短——不说清楚，模型会把截断的
#  描述当成技能的全部能力，从而漏掉本该匹配上的技能。
_SHORTENED_NOTE = "- （预算所限，以上部分描述已截短；技能一个不少，要用哪个先用 skill 工具读完整说明）"


def _omitted_note(count: int) -> str:
    return f"- …还有 {count} 个技能超出索引预算未列出（/skills 可查看全部）"


def _render(name: str, description: str) -> str:
    return f"- {name}: {description}" if description else f"- {name}"


def _line_cost(line: str) -> int:
    """一行的成本要含它后面的换行符：行是 "\\n".join 起来的，不记账就会
    系统性超支（每行漏 1 个字符，几十行就是好几个 token）。"""
    return tokens.estimate_text(line + "\n")


def index_block(skills: list[Skill], max_tokens: int | None = None) -> str:
    """拼进 system prompt 的技能索引。空列表返回空串。

    max_tokens 是整个索引块的估算 token 预算（调用方给上下文窗口的 2%）。
    超预算时**分三级降级，技能名尽最大努力保住**——索引的唯一作用是让模型
    "看见"某个技能存在，整条丢掉等于这个技能静默失效（装了却永不被选中），
    这比多花几百 token 糟得多：

    1. 全量放得下 → 全放；
    2. 放不下、但"只列名字"放得下 → 剩余额度**逐字符轮流**分给各条描述，
       谁也不能独吞（否则前几个技能吃光预算，后面全成光名字）；
    3. 连名字都放不下 → 才开始丢，尾部折叠成一行提示。

    任何一级降级都不影响 /skills 和 skill 工具：它们看的是完整技能表。

    下限：表头 + 尾部提示 + 一个光名字（约 90 token）压不下去。max_tokens
    比这还小时照样输出这个最小块——技能表整块消失比略微超支糟得多。按 2%
    比例算，只有上下文窗口小于 ~5k 才会踩到，现实中不存在。
    """
    if not skills:
        return ""
    header = [
        "",
        "可用技能（当任务和某个技能的描述匹配时，先用 skill 工具加载它的完整说明，再按说明执行）：",
    ]
    entries: list[tuple[str, str]] = []
    for skill in skills:
        description = skill.description or "（无描述）"
        if len(description) > DESCRIPTION_CAP:
            description = description[:DESCRIPTION_CAP] + "…"
        entries.append((skill.name, description))

    budget = None
    if max_tokens is not None:
        budget = max(max_tokens - sum(_line_cost(line) for line in header), 0)
    lines, note = _allocate(entries, budget)
    return "\n".join(header + lines + ([note] if note else []))


def _allocate(entries: list[tuple[str, str]], budget: int | None) -> tuple[list[str], str | None]:
    """按预算把 entries 渲染成索引行。返回 (行, 尾部提示或 None)。"""
    full = [_render(name, description) for name, description in entries]
    if budget is None or sum(_line_cost(line) for line in full) <= budget:
        return full, None

    #  放不下就一定会带一行提示——先把它的开销从预算里扣掉，别让提示本身超支。
    #  两种提示取贵的那个：这时还不知道会降到第 2 级还是第 3 级。
    reserve = max(_line_cost(_SHORTENED_NOTE), _line_cost(_omitted_note(len(entries))))
    budget = max(budget - reserve, 0)

    #  每行的固定开销按带分隔符和换行的 "- name: \n" 算（真渲染成光名字时只会
    #  更省），描述的边际成本才是水填充要分配的东西。
    costs = [tokens.estimate_prefix_costs(f"- {name}: \n", description) for name, description in entries]
    base = sum(row[0] for row in costs)
    if base <= budget:
        return _waterfill(entries, costs, budget - base), _SHORTENED_NOTE

    #  第 3 级：光名字也塞不下，能列几个列几个（至少留一个，否则索引形同虚设）
    lines: list[str] = []
    spent = 0
    for (name, _), row in zip(entries, costs):
        if spent + row[0] > budget and lines:
            break
        lines.append(_render(name, ""))
        spent += row[0]
    return lines, _omitted_note(len(entries) - len(lines))


def _waterfill(
    entries: list[tuple[str, str]], costs: list[list[int]], spare: int
) -> list[str]:
    """剩余额度逐字符轮流分给各条描述，直到谁都再多要一个字符都超支。

    轮流（而不是按顺序装满）是关键：技能表按名字排序，顺序装满等于让字母序
    靠前的技能吃光预算。
    """
    taken = [0] * len(entries)
    while True:
        progressed = False
        for index, row in enumerate(costs):
            if taken[index] >= len(row) - 1:
                continue
            delta = row[taken[index] + 1] - row[taken[index]]
            if delta <= spare:
                taken[index] += 1
                spare -= delta
                progressed = True
        if not progressed:
            break
    #  截短处不补省略号：补了就得为它再记账，而"描述被截短"已由尾部提示统一说明。
    return [_render(name, description[:count]) for (name, description), count in zip(entries, taken)]
