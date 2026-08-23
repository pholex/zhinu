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

import os
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
    #  负例（"别用于…"）：写进索引帮模型排除误触发。实测负例能显著降低误选，
    #  且比在 description 里堆正例更省——它落在描述尾部，预算紧张时最先被截掉
    when_not: str = ""


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
    #  XIAOYU_SKILLS_DIR（os.pathsep 分隔）优先：宿主指定技能目录 / eval 隔离用；
    #  给了就**只**认它，不再混入默认目录（隔离的前提是不被机器上的技能污染）
    if override := os.environ.get("XIAOYU_SKILLS_DIR", "").strip():
        return [Path(part).expanduser() for part in override.split(os.pathsep) if part.strip()]
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


def sources_fingerprint() -> tuple:
    """扫描来源目录的轻量指纹（路径 + 命名空间 + mtime）。

    给轮首的技能差量检测用：技能的**增删**表现为来源目录下子目录的增删，
    必然改变父目录 mtime——所以无变化的轮次只花几次 stat，零文件读取。
    局限（可接受）：原地编辑已有 SKILL.md 不动父目录 mtime，改 name/description
    要 /skills reload 或下次会话才反映到索引——正文本来就是 skill 工具现读的，
    不受影响。
    """
    rows = []
    for source in skill_sources():
        try:
            mtime = source.directory.stat().st_mtime_ns
        except OSError:
            mtime = None
        rows.append((str(source.directory), source.plugin, mtime))
    return tuple(rows)


#  frontmatter 顶层键的允许集：agent-skills 规范的五个 + 各家生态里已在用的几个。
#  拼错的键（descripton:）以前被静默吃掉——技能带着空描述进索引，模型永远
#  选不中它，而用户看不到任何线索。
FRONTMATTER_KEYS = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "version",
        "permissions",
        "when_to_use",
        "when_not",
        "triggers",
        "updated",
        "agent_transfer_payload",
    }
)


def frontmatter_problems(meta: dict[str, str]) -> list[str]:
    """frontmatter 的问题清单（空列表=没问题）。未知键回显允许集，便于对照改。"""
    problems: list[str] = []
    unknown = sorted(key for key in meta if key not in FRONTMATTER_KEYS)
    if unknown:
        problems.append(
            f"未知的 frontmatter 键 {', '.join(unknown)}（允许：{', '.join(sorted(FRONTMATTER_KEYS))}）"
        )
    if not meta.get("description", "").strip():
        problems.append("缺少 description")
    return problems


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
            problems = frontmatter_problems(meta)
            if problems:
                #  未知键 + 没描述 = 多半是拼错了 description：这样的技能进了
                #  索引也永远选不中，跳过并说明原因；只是多了个生僻键则照常加载。
                skip = len(problems) > 1
                print(
                    f"[技能 {name!r}{'跳过' if skip else ''}：{'；'.join(problems)}（{skill_md}）]",
                    file=sys.stderr,
                )
                if skip:
                    continue
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
                when_not=meta.get("when_not", "").strip(),
            )
    return list(found.values())


def load_skill_body(skill: Skill) -> str:
    """技能正文（去 frontmatter）。读失败返回错误文本交给模型。"""
    try:
        return strip_frontmatter(skill.path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return f"ERROR: 读取技能失败：{exc}"


#  索引里单条描述的字符上限。这是"挡失控极端值"的兜底，不是控总量的手段——
#  控总量归 index_block 的预算（它会按需逐字符压回去，且压得公平）。
#  两道一起收紧等于双重设限：预算宽裕时也照砍，砍掉的还偏偏是描述末尾的
#  路由信息（"什么时候别用这个技能"这类反向边界常写在最后），而那正是索引
#  该有的东西——"怎么执行"才留给 skill 工具加载正文。
DESCRIPTION_CAP = 1_024
#  负例上限：它是排除线索不是正文，短即可；超了截断（不加省略号，尾部本就是提示）
WHEN_NOT_CAP = 200


#  预算不够时的说明。技能全在、只是描述变短——不说清楚，模型会把截断的
#  描述当成技能的全部能力，从而漏掉本该匹配上的技能。
_SHORTENED_NOTE = "- （预算所限，以上部分描述已截短；技能一个不少，要用哪个先用 skill 工具读完整说明）"


def _omitted_note(count: int) -> str:
    #  这条只在第 3 级出现，那时每一行都只剩光名字——不点明"描述已全部略去"，
    #  模型会把它们当成本来就没写描述的技能。
    #  这行本身也在跟技能名抢这点预算，能短则短。
    return f"- …预算耗尽：描述全略去，另有 {count} 个技能名未列出（/skills 看全部）"


@dataclass(frozen=True)
class IndexReport:
    """index_block 最近一次的预算降级情况（给用户看的，不进 prompt）。"""

    total: int
    truncated: int  # 描述被截短的技能数
    truncated_chars: int  # 被截掉的字符总数
    omitted: int  # 连名字都没列出的技能数

    #  平均截掉不到这么多字不值得打扰用户：几十个字的尾巴对路由影响很小。
    WARN_AVG_CHARS = 100

    def warning(self) -> str | None:
        if self.omitted:
            return (
                f"技能索引预算不足：{self.omitted}/{self.total} 个技能只剩名字甚至未列出。"
                "小羽可能找不到它们——停用不用的技能/插件，或换上下文更大的模型。"
            )
        if self.truncated and self.truncated_chars / self.truncated > self.WARN_AVG_CHARS:
            return (
                f"技能索引预算不足：{self.truncated}/{self.total} 个技能的描述被截短"
                f"（平均少 {self.truncated_chars // self.truncated} 字）。"
                "技能都还在，但匹配会变钝——停用不用的技能/插件可腾出预算。"
            )
        return None


last_index_report: IndexReport | None = None


def budget_warning() -> str | None:
    """最近一次 index_block 的用户侧警告（无需警告返回 None）。启动时打一次即可。"""
    return last_index_report.warning() if last_index_report else None


def _render(name: str, description: str) -> str:
    return f"- {name}: {description}" if description else f"- {name}"


def _line_cost(line: str) -> int:
    """一行的成本要含它后面的换行符：行是 "\\n".join 起来的，不记账就会
    系统性超支（每行漏 1 个字符，几十行就是好几个 token）。"""
    return tokens.estimate_text(line + "\n")


def index_block(
    skills: list[Skill], max_tokens: int | None = None, rank_by_usage: bool = False
) -> str:
    """拼进 system prompt 的技能索引。空列表返回空串。

    rank_by_usage=True 时先按使用账本排序（用得多的在前、未用过的按 mtime 新的
    在前）：预算降级丢的是尾部，排序让"真在用的"优先存活，而不是让来源+文件名
    的偶然顺序决定谁被丢。排序稳定确定，索引在会话内不抖（prefix cache 前缀）。

    max_tokens 是整个索引块的估算 token 预算（调用方给上下文窗口的 2%）。
    超预算时**分三级降级，技能名尽最大努力保住**——索引的唯一作用是让模型
    "看见"某个技能存在，整条丢掉等于这个技能静默失效（装了却永不被选中），
    这比多花几百 token 糟得多：

    1. 全量放得下 → 全放；
    2. 放不下、但"只列名字"放得下 → 剩余额度**逐字符轮流**分给各条描述，
       谁也不能独吞（否则前几个技能吃光预算，后面全成光名字）；
    3. 连名字都放不下 → 才开始丢，尾部折叠成一行提示。

    任何一级降级都不影响 /skills 和 skill 工具：它们看的是完整技能表。

    下限：表头 45 + 尾部提示 30~40 + 至少一个技能名 ≈ **92 token** 压不下去
    （随机压测过，≥92 的预算不超支）。max_tokens 比这还小时照样输出这个最小
    块——技能表整块消失比略微超支糟得多。按 2% 比例算，只有上下文窗口小于
    ~5k 才会踩到，现实中不存在。
    """
    if not skills:
        return ""
    if rank_by_usage:
        from . import skill_usage

        skills = skill_usage.ranked(skills)
    header = [
        "",
        "可用技能（当任务和某个技能的描述匹配时，先用 skill 工具加载它的完整说明，再按说明执行）：",
    ]
    entries: list[tuple[str, str]] = []
    for skill in skills:
        description = skill.description or "（无描述）"
        if len(description) > DESCRIPTION_CAP:
            description = description[:DESCRIPTION_CAP] + "…"
        if skill.when_not:
            #  负例挂在描述尾部：跟着描述一起进预算，紧张时优先被截（waterfill 保前缀）
            when_not = skill.when_not[:WHEN_NOT_CAP]
            description = f"{description}（别用于：{when_not}）"
        entries.append((skill.name, description))

    budget = None
    if max_tokens is not None:
        budget = max(max_tokens - sum(_line_cost(line) for line in header), 0)
    lines, note = _allocate(entries, budget)
    global last_index_report
    last_index_report = _report(entries, lines)
    return "\n".join(header + lines + ([note] if note else []))


def _report(entries: list[tuple[str, str]], lines: list[str]) -> IndexReport:
    truncated = 0
    truncated_chars = 0
    for (name, description), line in zip(entries, lines):
        kept = line[len(f"- {name}: ") :] if line.startswith(f"- {name}: ") else ""
        lost = len(description) - len(kept)
        if lost > 0:
            truncated += 1
            truncated_chars += lost
    return IndexReport(
        total=len(entries),
        truncated=truncated,
        truncated_chars=truncated_chars,
        omitted=len(entries) - len(lines),
    )


def _allocate(entries: list[tuple[str, str]], budget: int | None) -> tuple[list[str], str | None]:
    """按预算把 entries 渲染成索引行。返回 (行, 尾部提示或 None)。"""
    full = [_render(name, description) for name, description in entries]
    if budget is None or sum(_line_cost(line) for line in full) <= budget:
        return full, None

    #  放不下就一定会带一行提示，它的开销要先从预算里扣掉——别让提示本身超支。
    #  两级各扣各的那条（下面 shortened / budget 两处）：统一按较贵的一条预留，
    #  会让常见的第 2 级白白少掉几 token。
    #
    #  每行的固定开销按带分隔符和换行的 "- name: \n" 算（真渲染成光名字时只会
    #  更省），描述的边际成本才是水填充要分配的东西。
    costs = [tokens.estimate_prefix_costs(f"- {name}: \n", description) for name, description in entries]
    base = sum(row[0] for row in costs)
    shortened = max(budget - _line_cost(_SHORTENED_NOTE), 0)
    if base <= shortened:
        return _waterfill(entries, costs, shortened - base), _SHORTENED_NOTE

    #  第 3 级：光名字也塞不下，能列几个列几个（至少留一个，否则索引形同虚设）。
    #  提示行的字数随丢弃个数变，按"全丢"预留是上界。
    omitted = max(budget - _line_cost(_omitted_note(len(entries))), 0)
    lines: list[str] = []
    spent = 0
    for (name, _), row in zip(entries, costs):
        if spent + row[0] > omitted and lines:
            break
        lines.append(_render(name, ""))
        spent += row[0]
    return lines, _omitted_note(len(entries) - len(lines))


def _waterfill(
    entries: list[tuple[str, str]], costs: list[list[int]], spare: int
) -> list[str]:
    """剩余额度逐字符轮流分给各条描述，直到谁都再多要一个字符都超支。

    轮流（而不是按顺序装满）是关键：技能表的顺序是"按来源分组、组内按文件名
    排序"（见 scan_skills），顺序装满等于让排在前面那个来源的技能吃光预算。

    分配只看 token 边际成本，所以同样字数下 ASCII 描述比中文描述便宜、能拿到
    更多字符——这是对的，它们本来就更省。顺序确定、无随机，索引在会话内稳定
    （system prompt 是 prompt cache 的前缀，抖一下就全废）。
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
