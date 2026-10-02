"""SKILL.md 技能：与 Anthropic / agentskills.io 规范同形态。

- 扫描目录（前者优先）：`~/.agents/skills/`（跨客户端规范库）、`<用户配置目录>/skills/`、
  工作区自带的 `<工作区>/.xiaoyu/skills/` 与 `<工作区>/.agents/skills/`，
  以及插件包带来的 `<用户配置目录>/plugins/<包名>/skills/`（见 `plugins.py`）
- 工作区自带的技能排在用户自己的之后：同名时用户的胜出，仓库不能顶掉你已有的技能；
  工作区没过信任门（见 `folder_trust.py`）时整类不加载
- 每个技能一个目录，内含 `SKILL.md`：YAML frontmatter（name / description）+ markdown 正文
- 渐进披露：索引（名字 + 一句话描述）进 system prompt，
  正文由模型用 `skill` 工具按需加载——技能再多也不占常驻上下文
- frontmatter 用零依赖解析：只认 `---` 块里平铺的 `key: value`，够用就好
- 插件包里的技能带命名空间前缀（`aws-core:aws-cdk`）：两家插件各带一个同名技能
  也不会互相顶掉，模型看到的名字就是调用的名字
"""

from __future__ import annotations

import fnmatch
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import fsguard, plugins, tokens
from .config import home_dir, user_config_dir

#  插件命名空间与技能名之间的分隔符（`<插件>:<技能>`，业界通行形态）
NAMESPACE_SEP = ":"


@dataclass(frozen=True)
class Skill:
    name: str  # 调用名；插件技能是 `<包名>:<技能名>`
    description: str
    path: Path  # SKILL.md 的完整路径
    plugin: str | None = None  # 来自哪个插件包；None = 散装技能目录
    project: bool = False  # 工作区自带的（随仓库来的，不是用户自己装的）
    #  负例（"别用于…"）：写进索引帮模型排除误触发。实测负例能显著降低误选，
    #  且比在 description 里堆正例更省——它落在描述尾部，预算紧张时最先被截掉
    when_not: str = ""


@dataclass(frozen=True)
class SkillSource:
    """一个扫描来源。`plugin` 非空则该目录下的技能名要加命名空间前缀。"""

    directory: Path
    plugin: str | None = None
    project: bool = False


#  工作区里放技能的位置（相对工作区根）：小羽自己的目录在前，跨客户端约定的在后
PROJECT_SKILL_DIRS = ((".xiaoyu", "skills"), (".agents", "skills"))


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


def project_skill_dirs(workspace: Path) -> list[Path]:
    """工作区自带技能的目录（存在与否不论）。"""
    return [workspace.joinpath(*parts) for parts in PROJECT_SKILL_DIRS]


def has_project_skills(workspace: Path) -> bool:
    """工作区里有没有自带技能（给"这次没加载"的提示用）。探测出错当没有。"""
    try:
        return any(
            next(directory.glob("*/SKILL.md"), None) is not None
            for directory in project_skill_dirs(workspace)
        )
    except OSError:
        return False


def skill_sources(workspace: Path | None = None) -> list[SkillSource]:
    """全部扫描来源：散装目录在前，工作区自带的居中，插件包在后。

    插件排后面不是因为它次要，而是因为它带命名空间、本来就不会和散装技能撞名——
    排序只决定散装目录之间谁胜出。

    `workspace` 给了才扫工作区自带的技能；要不要给由调用方按信任门的结论定
    （不信任的工作区传 None）。XIAOYU_SKILLS_DIR 指定了技能目录时不扫工作区：
    那个开关的语义就是"只认它"。
    """
    sources = [SkillSource(directory) for directory in skill_dirs()]
    if workspace is not None and not os.environ.get("XIAOYU_SKILLS_DIR", "").strip():
        sources += [
            SkillSource(directory, project=True) for directory in project_skill_dirs(workspace)
        ]
    sources += [SkillSource(path, plugin=name) for name, path in plugins.installed_skill_dirs()]
    return sources


def sources_fingerprint(workspace: Path | None = None, *, directories: tuple[Path, ...] | None = None) -> tuple:
    """扫描来源目录的轻量指纹（路径 + 命名空间 + mtime）。

    给轮首的技能差量检测用：技能的**增删**表现为来源目录下子目录的增删，
    必然改变父目录 mtime——所以无变化的轮次只花几次 stat，零文件读取。
    局限（可接受）：原地编辑已有 SKILL.md 不动父目录 mtime，改 name/description
    要 /skills reload 或下次会话才反映到索引——正文本来就是 skill 工具现读的，
    不受影响。
    """
    rows = []
    for source in (skill_sources(workspace) if directories is None else [SkillSource(p) for p in directories]):
        try:
            mtime = source.directory.stat().st_mtime_ns
        except OSError:
            mtime = None
        rows.append((str(source.directory), source.plugin, mtime))
    return tuple(rows)


#  frontmatter 顶层键的允许集：agent-skills 规范的五个 + 各家生态里已在用的几个。
#  拼错的键（descripton:）以前被静默吃掉——技能带着空描述进索引，模型永远
#  选不中它，而用户看不到任何线索。
#
#  ⚠️ 这张表是**笔误比对的基准，不是准入白名单**（2026-08-24 收窄）。第一版把
#  "不在表里"直接当问题报，于是别家生态的合法键——kdocs 官方技能的 `homepage:`、
#  agent-skills 的 `dependencies:`——每次启动各报一行。那些技能是 `npx skills`
#  装的第三方件，用户改了下次 update 就被覆盖，**噪音无法从源头消除**；而这条
#  检查真正要抓的"拼错 description"，另有独立的缺描述检查兜着。
#  现在只报**长得像已知键的笔误**（见 _typo_of），彻底陌生的键静默放行：
#  技能生态里各家自带私有键是常态，我们没有资格也没有必要给它们评判。
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


def _edit_distance(left: str, right: str) -> int:
    """Damerau-Levenshtein（相邻两字符换位算**一步**）。

    换位是最常见的手滑（`nmae` / `descrpition`），按普通 Levenshtein 算成两步
    会让它们恰好落在阈值外——正是最该抓的那一类漏网。键都是十几个字符，
    全量 DP 的开销可以忽略。
    """
    previous_2: list[int] = []
    previous = list(range(len(right) + 1))
    for i, left_char in enumerate(left, start=1):
        current = [i] + [0] * len(right)
        for j, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            current[j] = min(
                previous[j] + 1,  # 删
                current[j - 1] + 1,  # 增
                previous[j - 1] + cost,  # 改
            )
            if i > 1 and j > 1 and left_char == right[j - 2] and left[i - 2] == right_char:
                current[j] = min(current[j], previous_2[j - 2] + cost)  # 换位
        previous_2, previous = previous, current
    return previous[len(right)]


def _typo_of(key: str) -> str:
    """这个键像不像某个已知键的笔误：像就返回那个已知键，否则空串。

    阈值按长度收紧（短键 1、长键 2）：`date` 与 `name` 只差 2，按统一阈值会把
    一个常见的私有键报成 `name` 拼错——**误报比漏报贵得多**，因为用户对第三方
    技能里的键无能为力，只能每次启动看着它。大小写不同也算笔误（YAML 区分
    大小写，`Description:` 与拼错等效）。
    """
    lowered = key.lower()
    budget = 1 if len(lowered) <= 5 else 2
    best, best_distance = "", budget + 1
    for known in sorted(FRONTMATTER_KEYS):
        #  长度差就已经超预算的，不可能在阈值内
        if abs(len(known) - len(lowered)) > budget:
            continue
        distance = _edit_distance(lowered, known)
        if distance < best_distance:
            best, best_distance = known, distance
    return best


def frontmatter_problems(meta: dict[str, str]) -> list[str]:
    """frontmatter 的问题清单（空列表=没问题）。

    只报**疑似拼错**的键（连带给出正确拼法，比回显整张允许集更可行动），
    陌生但不像笔误的键静默放行——理由见 FRONTMATTER_KEYS 上方。
    """
    problems: list[str] = []
    typos = [
        (key, known)
        for key in sorted(meta)
        if key not in FRONTMATTER_KEYS and (known := _typo_of(key))
    ]
    if typos:
        problems.append(
            "疑似拼错的 frontmatter 键："
            + "、".join(f"{key} → 是不是 {known}？" for key, known in typos)
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
    #  上一个顶层键写了行内值、还可能被缩进行续写（YAML 的多行普通标量）
    open_key: str | None = None

    def unquote(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            return value[1:-1]
        return value

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
        if open_key is not None and line.startswith((" ", "\t")) and line.strip():
            #  值写了一行没写完、下一行缩进接着写：折成一行。description 常这么
            #  排版，只取首行的话续行里的触发词就丢了，技能永远选不中
            result[open_key] = unquote(f"{result[open_key]} {line.strip()}")
            continue
        key, sep, value = line.partition(":")
        #  嵌套结构（如 metadata:）的子行有缩进，跳过——索引只需要顶层键
        if sep and key == key.lstrip() and key.strip():
            value = value.strip()
            if value in (">", ">-", ">+", "|", "|-", "|+"):
                #  YAML 块标量：收集后续缩进行，折叠成一行（索引只要一句话）
                pending, collected, open_key = key.strip(), [], None
                continue
            result[key.strip()] = unquote(value)
            #  空值后面的缩进行是嵌套结构的子行，不是续写
            open_key = key.strip() if value else None
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


DISABLED_ENV = "XIAOYU_SKILLS_DISABLED"


def disabled_patterns() -> tuple[str, ...]:
    """停用清单：逗号分隔的技能名，可用通配（`lark-*`、`aws-core:*`）。

    技能库是几家客户端共用的（~/.agents/skills），为了给这一家的索引腾预算去删
    文件，会把别家也删掉；只能在这一家这边点名不要。
    """
    raw = os.environ.get(DISABLED_ENV, "")
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


def is_disabled(name: str, directory: str = "", patterns: tuple[str, ...] | None = None) -> bool:
    """名字（带插件前缀的全名）或目录名，任一个命中停用清单即停用。"""
    patterns = disabled_patterns() if patterns is None else patterns
    if not patterns:
        return False
    candidates = {name.lower(), directory.lower()} - {""}
    return any(fnmatch.fnmatchcase(item, pattern) for item in candidates for pattern in patterns)


def disabled_skills(workspace: Path | None = None) -> list[str]:
    """被停用清单挡掉的技能名（给 /skills 看的；正常扫描不含它们）。"""
    return sorted(skill.name for skill in _scan(workspace, keep_disabled=True)[1])


def scan_skills(workspace: Path | None = None, *, directories: tuple[Path, ...] | None = None) -> list[Skill]:
    """扫描所有来源（停用清单点名的不进结果）。"""
    return _scan(workspace, directories=directories)[0]


def _scan(
    workspace: Path | None = None, keep_disabled: bool = False,
    *, directories: tuple[Path, ...] | None = None,
) -> tuple[list[Skill], list[Skill]]:
    """扫描所有来源。同名技能第一个来源胜出，被盖掉的打一行 stderr。

    撞名以前是静默丢弃：装了两份同名技能时，模型加载到的是哪一份全凭目录顺序，
    而用户在 `/skills` 里只看得到一条——排查起来毫无线索。插件技能带命名空间，
    撞名只可能发生在散装目录之间，报出来的量很小。
    """
    found: dict[str, Skill] = {}
    disabled: list[Skill] = []
    patterns = disabled_patterns() if directories is None else []
    for source in (skill_sources(workspace) if directories is None else [SkillSource(p) for p in directories]):
        if not source.directory.is_dir():
            continue
        for skill_md in plugins.skill_files(source.directory):
            try:
                #  glob 不看文件类型：仓库里 SKILL.md 可以是指向设备的链接
                fsguard.require_regular(skill_md)
                meta = parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = (meta.get("name") or skill_md.parent.name).strip()
            if not name:
                continue
            if source.plugin:
                name = f"{source.plugin}{NAMESPACE_SEP}{name}"
            elif source.project and NAMESPACE_SEP in name:
                #  `<插件>:<技能>` 这个形态是留给插件包的。仓库自带的技能排在插件
                #  之前扫，自己起一个带前缀的名字就能把已装插件的同名技能顶掉
                print(
                    f"[技能 {name!r} 跳过：工作区自带的技能名不能含 {NAMESPACE_SEP!r}"
                    f"（那是插件包的命名空间）（{skill_md}）]",
                    file=sys.stderr,
                )
                continue
            if is_disabled(name, skill_md.parent.name, patterns):
                #  在撞名判定之前就摘掉：停用的那份不该占着名字把后面同名的挡掉
                if keep_disabled:
                    disabled.append(
                        Skill(name=name, description="", path=skill_md, plugin=source.plugin,
                              project=source.project, when_not="")
                    )
                continue
            problems = frontmatter_problems(meta)
            if problems:
                #  疑似笔误 + 没描述 = 多半就是把 description 拼错了：这样的技能
                #  进了索引也永远选不中，跳过并说明原因；只是某个键拼歪了、描述
                #  仍在，则照常加载。彻底陌生的键连问题都不算，走不到这里。
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
                project=source.project,
                when_not=meta.get("when_not", "").strip(),
            )
    return list(found.values()), disabled


def load_skill_body(skill: Skill) -> str:
    """技能正文（去 frontmatter）。读失败返回错误文本交给模型。"""
    try:
        fsguard.require_regular(skill.path)  # 扫描之后可能被换成特殊文件
        return strip_frontmatter(skill.path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        return f"ERROR: 读取技能失败：{exc}"


# ---------- 参数占位（$ARGUMENTS / $ARGUMENTS[i] / $i / $名字） ----------

#  frontmatter 里声明具名参数的两种写法（都按位置对应 $1、$2…）：
#      arguments: [env, version]        # 顶层行内列表
#      arguments:                       # 顶层块列表
#        - env
#        - version
#      metadata:
#        arguments: [env, version]      # 规范里私有键都挂 metadata 下，也认
_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _split_inline_list(value: str) -> list[str]:
    """`[a, b]` / `a, b` → 名字列表；不合法的名字丢掉（占位符只能是标识符形态）。"""
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    names = []
    for part in value.split(","):
        part = part.strip().strip("\"'")
        if part and _ARG_NAME.match(part):
            names.append(part)
    return names


def declared_arguments(text: str) -> list[str]:
    """SKILL.md 文本里声明的具名参数（按位置对应）；没声明返回空列表。

    零依赖手解析，只认上面注释里的几种形态——技能作者要的是"给参数起个名"，
    不是整套 YAML。
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []
    names: list[str] = []
    collecting = False  # 正在收 `arguments:` 下面的 `- 项` 块列表
    in_metadata = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        indented = line[:1] in (" ", "\t")
        if collecting:
            if indented and stripped.startswith("- "):
                item = stripped[2:].strip().strip("\"'")
                if _ARG_NAME.match(item):
                    names.append(item)
                continue
            if indented and not stripped:
                continue
            collecting = False
            if names:
                return names
        if not indented:
            in_metadata = stripped.startswith("metadata:")
        key, sep, value = stripped.partition(":")
        if not sep or key.strip() != "arguments":
            continue
        if indented and not in_metadata:
            continue  # 别的嵌套块里的 arguments 不是给我们的
        if value.strip():
            found = _split_inline_list(value)
            if found:
                return found
            continue
        collecting = True
    return names


def skill_arguments(skill: Skill) -> list[str]:
    """读 SKILL.md 取声明的具名参数；读失败按没声明。"""
    try:
        return declared_arguments(skill.path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []


#  占位符形态。`ARGUMENTS\[` 要排在裸名字之前，否则 `$ARGUMENTS[2]` 会被当成
#  `$ARGUMENTS` 后面跟了个 `[2]`。花括号形态（${1}、${env}）顺手也认——紧跟
#  字母数字的位置（`$1abc`）没有花括号就写不出来。
_PLACEHOLDER = re.compile(
    r"\$(?:\{(?P<braced>ARGUMENTS|\d+|[A-Za-z_][A-Za-z0-9_]*)\}"
    r"|ARGUMENTS\[(?P<index>\d+)\]"
    r"|(?P<bare>ARGUMENTS(?![A-Za-z0-9_])|\d+|[A-Za-z_][A-Za-z0-9_]*))"
)


def expand_arguments(body: str, arguments: str, names: list[str] | None = None) -> tuple[str, list[str]]:
    """把正文里的参数占位换成实际参数；返回 (正文, 缺参的占位列表)。

    - `$ARGUMENTS` = 全部参数原文；`$ARGUMENTS[i]` / `$i` = 按空白切的第 i 个（从 1 起）；
    - `$名字` 只认 frontmatter 声明过的名字（按位置对应）：别的 `$xxx`（shell 片段
      里的 `$PATH`、`$HOME`）不是占位，原样不动；
    - 没给到的占位**原样保留**并回报，由调用方提示。裸 `$i` 缺参不回报：
      技能正文里的 `awk '{print $1}'` 太常见，报出来全是误伤；`$ARGUMENTS`
      与声明过的 `$名字` 不会出现在别的语境里，缺了就是真缺。
    """
    names = names or []
    arguments = arguments.strip()
    parts = arguments.split()
    missing: list[str] = []

    def value_at(index: int) -> str | None:
        return parts[index - 1] if 1 <= index <= len(parts) else None

    def replace(match: re.Match) -> str:
        token = match.group("braced") or match.group("bare")
        index_text = match.group("index")
        if index_text is not None:
            value = value_at(int(index_text))
            if value is None and match.group(0) not in missing:
                missing.append(match.group(0))
            return match.group(0) if value is None else value
        if token == "ARGUMENTS":
            if not arguments:
                if match.group(0) not in missing:
                    missing.append(match.group(0))
                return match.group(0)
            return arguments
        if token.isdigit():
            value = value_at(int(token))
            return match.group(0) if value is None else value
        if token in names:
            value = value_at(names.index(token) + 1)
            if value is None and match.group(0) not in missing:
                missing.append(match.group(0))
            return match.group(0) if value is None else value
        return match.group(0)

    return _PLACEHOLDER.sub(replace, body), missing


# ---------- 支持文件清单 ----------

#  清单条数上限：技能目录里带整个 node_modules 的也有，全列等于把目录树塞进上下文
SUPPORTING_FILES_CAP = 40


def supporting_files(skill_dir: Path, cap: int = SUPPORTING_FILES_CAP) -> tuple[list[tuple[str, Path]], int]:
    """技能目录下除 SKILL.md 外的文件：[(相对路径, 绝对路径)…]（最多 cap 条）与总数。

    隐藏文件 / 目录（.git、.DS_Store）与 __pycache__ 不算；不跟符号链接进目录
    （技能库里指向工作区外的链接不该被顺着列出来）。按路径排序，输出稳定。
    """
    rows: list[tuple[str, Path]] = []
    try:
        for dirpath, dirnames, filenames in os.walk(skill_dir, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                full = Path(dirpath) / filename
                if full == skill_dir / "SKILL.md":
                    continue
                rows.append((full.relative_to(skill_dir).as_posix(), full))
    except OSError:
        return [], 0
    return rows[:cap], len(rows)


def supporting_files_note(skill: Skill, cap: int = SUPPORTING_FILES_CAP) -> str:
    """给 skill 工具头部用的一段清单文本；目录里没别的文件时返回空串。"""
    rows, total = supporting_files(skill.path.parent, cap)
    if not rows:
        return ""
    lines = [f"[支持文件（引用时用右侧的绝对路径）："]
    lines += [f"  {relative} → {absolute}" for relative, absolute in rows]
    if total > len(rows):
        lines.append(f"  …另有 {total - len(rows)} 个未列出，需要时 list_files 看技能目录")
    return "\n".join(lines) + "]"


def clip_body(skill: Skill, body: str, budget: int) -> str:
    """正文超过 budget 时只给开头连续的一段，并指明从文件第几行接着读。

    工具输出的通用超长处理是"留头留尾、中段落盘"——对日志合适，对技能不合适：
    技能正文是按顺序读的指令，掐掉中间等于步骤三直接跳到步骤九，而模型手里
    那份看起来还挺完整。这里改成给连续的开头，其余让模型用 read_file 分段读。
    切点落在行边界上。
    """
    if len(body) <= budget:
        return body
    cut = body.rfind("\n", 0, max(1, budget))
    if cut < budget // 2:
        cut = budget  # 一行就超长（压成一行的正文）：只能硬切
    shown = body[:cut]
    #  正文在文件里从第几行开始（frontmatter 占掉了前面若干行）
    first_line = 1
    try:
        raw = skill.path.read_text(encoding="utf-8", errors="replace")
        start = raw.find(body[:200]) if body else -1
        if start > 0:
            first_line = raw.count("\n", 0, start) + 1
    except OSError:
        pass
    next_line = first_line + shown.count("\n") + 1
    total = len(body)
    return (
        f"{shown}\n\n"
        f"[技能正文共 {total} 字符，以上是开头连续的 {len(shown)} 字符，**后面还有内容没给出**。"
        f"接着读：read_file(path=\"{skill.path}\", offset={next_line})，按需分段读完"
        "再照着做——不要凭已读的这部分推测后面的步骤。]"
    )


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
                f"小羽可能找不到它们——用 {DISABLED_ENV} 停用不用的技能（逗号分隔，可通配），"
                "或换上下文更大的模型。"
            )
        if self.truncated and self.truncated_chars / self.truncated > self.WARN_AVG_CHARS:
            return (
                f"技能索引预算不足：{self.truncated}/{self.total} 个技能的描述被截短"
                f"（平均少 {self.truncated_chars // self.truncated} 字）。"
                f"技能都还在，但匹配会变钝——用 {DISABLED_ENV} 停用不用的技能可腾出预算。"
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
