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


def index_block(skills: list[Skill], max_tokens: int | None = None) -> str:
    """拼进 system prompt 的技能索引。空列表返回空串。

    max_tokens 是整个索引块的估算 token 预算（调用方一般给上下文窗口的
    1%）：技能装得再多，
    常驻开销也被封顶；超预算的技能不进索引，但 /skills 和 skill 工具仍可用。
    """
    if not skills:
        return ""
    lines = [
        "",
        "可用技能（当任务和某个技能的描述匹配时，先用 skill 工具加载它的完整说明，再按说明执行）：",
    ]
    spent = sum(tokens.estimate_text(line) for line in lines)
    shown = 0
    for skill in skills:
        description = skill.description or "（无描述）"
        if len(description) > DESCRIPTION_CAP:
            description = description[:DESCRIPTION_CAP] + "…"
        line = f"- {skill.name}: {description}"
        cost = tokens.estimate_text(line)
        if max_tokens is not None and spent + cost > max_tokens and shown > 0:
            lines.append(f"- …还有 {len(skills) - shown} 个技能超出索引预算未列出（/skills 可查看全部）")
            break
        lines.append(line)
        spent += cost
        shown += 1
    return "\n".join(lines)
