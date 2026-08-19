"""插件包（plugin bundle）通道：一条命令装齐第三方的 skills + MCP 声明。

⚠️ 与 `tools.py` 里的**插件工具**（entry point 组 `xiaoyu.tools`、开关
`XIAOYU_ENABLE_PLUGINS`）不是一回事：那是**代码级**工具通道，第三方 Python 包
往进程里注册函数；这里是**内容级**分发通道，装的是提示文本（SKILL.md）和
子进程声明（mcp.json）。两者互不依赖。

认的是 agent-plugins.org 1.0.0 那套中立规范（AWS 的 agent-toolkit-for-aws 就按
它分发，多家客户端各自认领同一个包）：

    <包根>/
      plugin.json            元数据（$schema + name 必填，version/repository 等可选）
      skills/<名字>/SKILL.md  技能，与小羽已支持的形态完全一致
      mcp.json               MCP server 声明，形状与 .mcp.json 相同
      com.<厂商>.<客户端>/     某家客户端专属的东西（hooks 就在这里），本通道不碰

各家的私有变体按"中立规范优先"依次兜底：`.claude-plugin/plugin.json` 等只在
根 `plugin.json` 缺席时才认（对应各家客户端插件缓存目录的实际形态）。

**为什么值得单开一条通道**：散装 SKILL.md 快照装完即冻结，上游更新看不见；
插件包记来源与版本，`xiaoyu plugin update` 能拉新。技能名还带上了插件命名空间
（`aws-core:aws-cdk`），装两家同名技能不会互相顶掉。

**刻意不做的**：hooks 不装（中立规范里根本没有 hooks，那是各家私有扩展），
但发现了必须报出来——aws-core 的 hook 恰恰是 secret-safety 这类安全用途，
静默丢掉比不装更危险。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import user_config_dir


class PluginError(Exception):
    """插件包安装/解析过程中可预期的失败，调用方打印后退出即可。"""


#  包名会变成目录名、技能命名空间前缀、MCP server 名前缀——三处都得干净。
#  刻意**不允许点号**：① MCP server 名只认字母数字下划线连字符，允许点就得做字符
#  替换，而 `a.b` 与 `a_b` 替换后撞成同一个名字，后装的会悄悄顶掉先装的；
#  ② 安装目录的临时名靠"点开头"与合法包名区分（见 materialize）。
#  取值域与 MCP server 名对齐后，包名一路原样透传，不需要任何消毒。
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def check_name(name: str) -> str:
    """包名校验。名字会被拼进 `rmtree` 的路径里，每个入口都必须过这一关。"""
    if not NAME_PATTERN.match(name):
        raise PluginError(
            f"包名不合法：{name!r}（只能用字母/数字/下划线/连字符，且以字母数字开头）"
        )
    return name

#  元数据文件：中立规范优先，各家私有变体兜底
MANIFEST_FILES = (
    "plugin.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    ".cursor-plugin/plugin.json",
)
#  MCP 声明：中立规范的 mcp.json 优先，常见私有变体 .mcp.json 兜底
MCP_FILES = ("mcp.json", ".mcp.json")
#  一个仓库装多个插件时的清单（marketplace）
MARKETPLACE_FILES = (".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json")
SKILLS_DIRNAME = "skills"

#  git clone 的超时（秒）：卡死的网络不该让命令行永远挂着
CLONE_TIMEOUT = 300.0


def plugins_root() -> Path:
    """插件包的安装根目录：`<用户配置目录>/plugins/<包名>/`。

    刻意**不写** `~/.agents/skills`：那是跨客户端共享的规范库，写进去会和
    别家客户端自己的插件版本形成双份漂移。插件包是小羽自己管的东西，
    就放自己家里。
    """
    return user_config_dir() / "plugins"


def registry_path() -> Path:
    """来源账本：记 source/ref/commit/version，`update` 靠它知道去哪拉新。

    账本只是**元数据**，"装没装"以目录在不在为准——和 mcp.json 一样，
    文件本身就是真相，手动删目录不会留下幽灵条目。
    """
    return plugins_root() / "installed.json"


# ---------- 包识别 ----------


@dataclass(frozen=True)
class Bundle:
    """一个插件包被识别出来的内容。"""

    name: str
    path: Path
    version: str
    description: str
    manifest_file: str | None  # 相对包根的路径；None = 无元数据，按目录约定识别
    skills: tuple[str, ...]
    mcp_servers: dict[str, dict[str, Any]]
    mcp_file: str | None
    notes: tuple[str, ...]  # 识别到但本通道不处理的东西，逐条报给用户


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PluginError(f"{path} 读不出来：{exc}") from exc
    if not isinstance(data, dict):
        raise PluginError(f"{path} 的顶层不是 JSON 对象")
    return data


def _first_existing(root: Path, candidates: tuple[str, ...]) -> tuple[str, Path] | None:
    for relative in candidates:
        path = root / relative
        if path.is_file():
            return relative, path
    return None


def scan_bundle_skills(root: Path) -> tuple[str, ...]:
    """包里的技能名。读不了目录按"没有技能"处理，不抛。

    取的是 frontmatter 里的 `name`（目录名只是兜底）——必须和 `skills.scan_skills`
    同一套口径，否则这里报出来的名字模型根本调不到：技能目录叫 `iam`、
    frontmatter 写 `aws-iam` 时，装完提示的 `pkg:iam` 是个不存在的名字。
    """
    from . import skills as skills_module

    skills_dir = root / SKILLS_DIRNAME
    if not skills_dir.is_dir():
        return ()
    names = []
    for md in skills_dir.glob("*/SKILL.md"):
        try:
            meta = skills_module.parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        name = (meta.get("name") or md.parent.name).strip()
        if name:
            names.append(name)
    return tuple(sorted(names))


def _scan_hooks(root: Path, manifest: dict[str, Any]) -> list[str]:
    """找出包里声明的 hooks——不为了装，只为了如实报出来。"""
    found: list[str] = []
    extensions = manifest.get("extensions")
    if isinstance(extensions, dict):
        for namespace, value in extensions.items():
            if isinstance(value, dict) and "hooks" in value:
                found.append(f"{namespace} 的 hooks")
    if not found:
        #  没有 extensions 声明时按目录约定找一遍（有的插件缓存形态把
        #  hooks 直接摊在包根下，元数据里并没有指针）
        for candidate in sorted(root.glob("*/hooks/hooks.json")) + [root / "hooks" / "hooks.json"]:
            if candidate.is_file():
                found.append(str(candidate.relative_to(root)))
    return found


def inspect_bundle(root: Path, fallback_name: str | None = None) -> Bundle:
    """识别一个包目录。既没技能也没 MCP 声明的目录不算插件包，直接拒。"""
    if not root.is_dir():
        raise PluginError(f"不是一个目录：{root}")

    manifest: dict[str, Any] = {}
    manifest_file: str | None = None
    if hit := _first_existing(root, MANIFEST_FILES):
        manifest_file, path = hit
        manifest = _read_json(path)

    name = check_name(str(manifest.get("name") or fallback_name or root.name).strip())

    skills = scan_bundle_skills(root)

    mcp_servers: dict[str, dict[str, Any]] = {}
    mcp_file: str | None = None
    notes: list[str] = []
    if hit := _first_existing(root, MCP_FILES):
        mcp_file, path = hit
        raw = _read_json(path).get("mcpServers")
        if isinstance(raw, dict):
            for server, entry in raw.items():
                if not isinstance(entry, dict):
                    notes.append(f"{mcp_file} 里的 {server} 不是对象，跳过")
                    continue
                #  形状即类型（与 mcp.py 的配置解析同一判据）：有 url 是远端，
                #  有 command 是本地 stdio
                remote = isinstance(entry.get("url"), str) and bool(entry["url"].strip())
                if entry.get("type") == "sse":
                    #  中立规范还允许老式 SSE，小羽没实现——静默丢掉会让用户
                    #  以为装上了，装完发现工具凭空少一半
                    notes.append(
                        f"{mcp_file} 里的 {server} 是 sse 类型，"
                        "小羽支持 stdio 与 Streamable HTTP（url），未安装"
                    )
                    continue
                if not remote and not isinstance(entry.get("command"), str):
                    notes.append(f"{mcp_file} 里的 {server} 既没有 command 也没有 url，跳过")
                    continue
                mcp_servers[str(server)] = entry

    for hook in _scan_hooks(root, manifest):
        notes.append(f"带 {hook}，本通道不安装 hooks")

    if not skills and not mcp_servers:
        raise PluginError(
            f"{root} 里既没有 {SKILLS_DIRNAME}/ 也没有可用的 MCP 声明，不像一个插件包"
        )

    return Bundle(
        name=name,
        path=root,
        version=str(manifest.get("version") or "").strip(),
        description=str(manifest.get("description") or "").strip(),
        manifest_file=manifest_file,
        skills=skills,
        mcp_servers=mcp_servers,
        mcp_file=mcp_file,
        notes=tuple(notes),
    )


def find_marketplace(root: Path) -> list[tuple[str, str]]:
    """仓库根上的插件清单 →[(包名, 相对子目录)]。不是 marketplace 就返回空。

    两种形状都认：中立规范的 `source: {"source": "local", "path": "./x"}`，
    和常见的简写变体 `source: "./x"`。指向远端仓库的条目跳过——那是另一次
    `plugin add`，不该在这里递归展开。
    """
    hit = _first_existing(root, MARKETPLACE_FILES)
    if hit is None:
        return []
    _, path = hit
    entries = _read_json(path).get("plugins")
    if not isinstance(entries, list):
        return []
    result: list[tuple[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        source = entry.get("source")
        if isinstance(source, dict):
            source = source.get("path") if source.get("source") == "local" else None
        if not name or not isinstance(source, str):
            continue
        #  清单是从网上拉来的：别让 "../../etc" 把安装路径指到仓库外面。
        #  ⚠️ 两个踩过的坑：① **不能**用 `lstrip("./")` 去掉开头的 "./"——那是按
        #  字符集剥，会把 "../../etc" 一路剥成 "etc"，看着人畜无害实则已经越界；
        #  ② 绝对路径要在**剥完之后**判——对原串判 `isabs(" /etc")` 是 False，
        #  而 `Path(" /etc".strip())` 已经是绝对路径了。
        parts = [part for part in Path(source.strip()).parts if part != "."]
        if not parts or ".." in parts:
            continue
        relative = Path(*parts)
        #  ⚠️ Windows 上 `Path("/etc").is_absolute()` 是 **False**（没有盘符只算
        #  "当前盘的根相对"），只判 is_absolute 的话 " /etc" 这类逃逸在 Windows 上
        #  照样放行。drive 与 root 都要看。
        if relative.is_absolute() or relative.drive or relative.root:
            continue
        #  返回正斜杠形式：这个值会写进账本的 subpath 长期保存，
        #  用 str() 在 Windows 上得到反斜杠，同一份账本就不跨平台了。
        #  `root / "p/a"` 在 Windows 上照样能解析。
        result.append((name, relative.as_posix()))
    return result


# ---------- 取源 ----------


@dataclass(frozen=True)
class Origin:
    """一次安装的来源，原样落进账本供 `update` 复现。"""

    kind: str  # "git" | "local"
    source: str  # 用户当初敲的那串
    url: str = ""
    ref: str = ""
    commit: str = ""
    subpath: str = ""


#  `owner/repo` 简写：两段、不含空格与协议头
_SHORTHAND = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def resolve_source(source: str) -> tuple[str, str]:
    """用户给的来源 →(kind, url)。本地目录优先判定，免得同名目录被当成 owner/repo。"""
    expanded = Path(source).expanduser()
    if expanded.is_dir():
        return "local", str(expanded.resolve())
    #  file:// 也走 git：拿到的是**已提交**的内容，和直接指目录（工作区当前状态）
    #  不是一回事——本地开发插件包时这个区别很要紧
    if source.startswith(("https://", "http://", "git@", "ssh://", "file://")):
        return "git", source
    if _SHORTHAND.match(source):
        return "git", f"https://github.com/{source}.git"
    raise PluginError(
        f"看不懂的来源：{source}（可以是本地目录、owner/repo，或 git 仓库 URL）"
    )


def _git(args: list[str], cwd: Path | None = None) -> str:
    if shutil.which("git") is None:
        raise PluginError("PATH 里找不到 git，装不了远端插件包（本地目录仍然可以装）")
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CLONE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise PluginError(f"git {args[0]} 超过 {int(CLONE_TIMEOUT)} 秒没完成") from exc
    if done.returncode != 0:
        raise PluginError(f"git {args[0]} 失败：{(done.stderr or done.stdout).strip()}")
    return done.stdout.strip()


def fetch(source: str, ref: str = "", into: Path | None = None) -> tuple[Path, Origin]:
    """把来源取到本地。返回（内容根目录, 来源记录）。

    本地目录直接返回原路径（**不复制**，安装时才复制）；git 来源浅克隆到
    `into`（调用方负责建与清临时目录）。
    """
    kind, url = resolve_source(source)
    if kind == "local":
        return Path(url), Origin(kind="local", source=source, url=url, ref=ref)

    if into is None:
        raise PluginError("内部错误：git 来源必须给临时目录")
    args = ["clone", "--depth", "1", "--quiet"]
    if ref:
        args += ["--branch", ref]
    _git([*args, url, str(into)])
    commit = _git(["rev-parse", "HEAD"], cwd=into)
    return into, Origin(kind="git", source=source, url=url, ref=ref, commit=commit)


def _contained(root: Path, candidate: Path) -> Path:
    """确认候选路径真在 root 里面。清单/参数里的路径过完形状检查还得再验一次实际落点
    ——符号链接、盘符、平台差异都能让"看着是相对路径"的东西落到外面去。"""
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise PluginError(f"这个来源指向了仓库外面：{candidate}") from exc
    return candidate


def pick_bundle(root: Path, want: str | None, subpath: str = "") -> tuple[Path, str | None]:
    """在取到的内容里定位到具体那个包。

    - 给了 `subpath` 就照办；
    - 根上是 marketplace（一仓多包）：`want` 命中哪个装哪个，没给且只有一个就装它，
      多个则报错把清单列出来让用户挑；
    - 否则根目录自己就是包。
    """
    if subpath:
        if ".." in Path(subpath).parts or os.path.isabs(subpath):
            raise PluginError(f"子目录得是包内相对路径：{subpath}")
        target = _contained(root, root / subpath)
        if not target.is_dir():
            raise PluginError(f"来源里没有这个子目录：{subpath}")
        return target, None

    listed = find_marketplace(root)
    if listed:
        if want:
            for name, relative in listed:
                if name == want:
                    return _contained(root, root / relative), name
            names = "、".join(name for name, _ in listed)
            raise PluginError(f"这个仓库里没有名为 {want!r} 的包。有的是：{names}")
        if len(listed) == 1:
            name, relative = listed[0]
            return _contained(root, root / relative), name
        names = "、".join(name for name, _ in listed)
        raise PluginError(
            f"这个仓库装了 {len(listed)} 个插件包，用 --name 指定要装哪个：{names}"
        )
    return root, want


# ---------- 账本 ----------


def load_registry() -> dict[str, Any]:
    path = registry_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        #  账本坏了只是丢来源信息（update 会提示重装），不该让已装的包全体消失
        return {}
    plugins = data.get("plugins")
    return plugins if isinstance(plugins, dict) else {}


def save_registry(plugins: dict[str, Any]) -> None:
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps({"plugins": plugins}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def installed_dirs() -> list[tuple[str, Path]]:
    """已安装的包 →[(包名, 目录)]。以目录为准，账本只补元数据。

    只认名字合法的目录：`materialize` 崩在半路留下的 `.staging-x` 残骸不是包，
    被当成包会一路污染技能索引、`plugin list` 和 `plugin update`。
    """
    root = plugins_root()
    if not root.is_dir():
        return []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    return [
        (child.name, child)
        for child in children
        if child.is_dir() and NAME_PATTERN.match(child.name)
    ]


def installed_skill_dirs() -> list[tuple[str, Path]]:
    """给 skills.py 的扫描来源：[(包名, skills 目录)]，只列真存在的。

    每次构造 Agent 都会走一遍，所以不读账本、不做校验——一次 iterdir 而已。
    """
    found = []
    for name, path in installed_dirs():
        skills_dir = path / SKILLS_DIRNAME
        if skills_dir.is_dir():
            found.append((name, skills_dir))
    return found


def declared_mcp(bundle: Bundle) -> dict[str, dict[str, Any]]:
    """包**声明**的 MCP 条目（带命名空间的名字）。注意和"实际装上的"是两回事。"""
    return {
        mcp_server_name(bundle.name, server): entry
        for server, entry in bundle.mcp_servers.items()
    }


def record(name: str, bundle: Bundle, origin: Origin, mcp_installed: list[str]) -> None:
    """把这次安装写进账本。

    除了装上的条目，还记下这次上游**声明**了什么（`declared_mcp`）：下次 update
    才能拿"上游 vs 上游"比对。拿"上游 vs 已装"比对的话，用户当初拒绝装 MCP 的包
    每次 update 都会重报一遍"有变化"——而这正是防 rug-pull 的那道提示，
    天天喊狼来了只会把用户训练成闭眼点是。
    """
    plugins = load_registry()
    plugins[name] = {
        "declared_mcp": declared_mcp(bundle),
        "kind": origin.kind,
        "source": origin.source,
        "url": origin.url,
        "ref": origin.ref,
        "commit": origin.commit,
        "subpath": origin.subpath,
        "version": bundle.version,
        "description": bundle.description,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "skills": list(bundle.skills),
        "mcp_servers": mcp_installed,
    }
    save_registry(plugins)


def forget(name: str) -> None:
    plugins = load_registry()
    if plugins.pop(name, None) is not None:
        save_registry(plugins)


# ---------- 落盘 ----------

#  拷贝时丢掉的东西：.git 是克隆产物（一个包动辄几十兆），其余按需扩展
_COPY_SKIP = shutil.ignore_patterns(".git", "__pycache__", ".DS_Store")


def _copy_filter(directory: str, names: list[str]) -> set[str]:
    """copytree 的 ignore 回调：固定黑名单 + **所有符号链接**。

    包是从网上拉来的，`symlinks=False` 会把链接指向的内容拷进来——一个
    `skills/x/SKILL.md -> ~/.ssh/id_rsa` 就把私钥拷成了可加载的技能正文；
    `symlinks=True` 保留链接同样能在读取时指出去。两种都不安全，一律不拷。
    """
    skipped = set(_COPY_SKIP(directory, names))
    base = Path(directory)
    skipped.update(name for name in names if (base / name).is_symlink())
    return skipped


#  安装目录里的临时名。**以点开头**——`NAME_PATTERN` 要求首字符是字母数字，
#  所以这两个名字永远不可能和真实包名撞上（曾经用 `<名字>.new`，而 `demo.new`
#  本身就是个合法包名，更新 demo 会把已装的 demo.new 整个删掉）。
def _staging_paths(root: Path, name: str) -> tuple[Path, Path]:
    return root / f".staging-{name}", root / f".backup-{name}"


def materialize(bundle_dir: Path, name: str) -> Path:
    """把包内容拷进安装目录。先拷到临时目录再整体换，装到一半崩了不留半个包。"""
    check_name(name)
    root = plugins_root()
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    staging, backup = _staging_paths(root, name)
    for leftover in (staging, backup):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    shutil.copytree(bundle_dir, staging, ignore=_copy_filter, symlinks=False)
    if target.exists():
        os.replace(target, backup)
    try:
        os.replace(staging, target)
    except OSError:
        if backup.exists():  # 换回去，别让用户落得两头空
            os.replace(backup, target)
        raise
    shutil.rmtree(backup, ignore_errors=True)
    return target


def uninstall_dir(name: str) -> bool:
    """删掉安装目录。返回是否真的删了东西。

    名字先过 `check_name`：这里是全模块唯一一处对用户给的名字做 `rmtree`，
    不校验的话 `plugin remove ../../../某目录`（或绝对路径，`Path(root) / "/etc"`
    直接等于 `/etc`）就是一条删任意目录的命令。
    """
    target = plugins_root() / check_name(name)
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def staging_dir() -> tempfile.TemporaryDirectory[str]:
    """git 克隆用的临时目录。放在系统临时区，调用方用 with 管生命周期。"""
    return tempfile.TemporaryDirectory(prefix="xiaoyu-plugin-")


# ---------- MCP 条目 ----------

#  MCP server 名的合法字符（见 cli.MCP_NAME_PATTERN）；包名里的点要换掉
_MCP_NAME_CLEAN = re.compile(r"[^A-Za-z0-9_-]")
#  账本之外还在条目里留个记号：`xiaoyu mcp list` 能看出这条是谁装的，
#  手动编辑 mcp.json 的人也不会一头雾水
OWNER_KEY = "_plugin"


def mcp_server_name(plugin: str, server: str) -> str:
    """插件的 server 名加上命名空间：两家插件各带一个 `docs` server 不会互相顶掉。

    包名原样透传（`NAME_PATTERN` 的取值域已经和 MCP server 名对齐），只消毒
    来自上游 JSON 的 server 名。包名也做字符替换的话，`a.b` 和 `a_b` 会撞成
    同一个条目——后装的顶掉先装的，之后 `remove a.b` 还摘不掉自己装的那条。
    """
    return f"{check_name(plugin)}__{_MCP_NAME_CLEAN.sub('_', server)}"


def user_mcp_path() -> Path:
    from . import mcp

    return mcp.scope_path("user", Path.cwd())


def read_user_mcp() -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """用户级 mcp.json →(路径, 整份数据, mcpServers 段)。"""
    from . import mcp

    path = user_mcp_path()
    try:
        data = mcp.read_config_file(path)
    except mcp.McpError as exc:
        raise PluginError(str(exc)) from exc
    servers = data.get("mcpServers")
    return path, data, servers if isinstance(servers, dict) else {}


def write_user_mcp(path: Path, data: dict[str, Any], servers: dict[str, Any]) -> None:
    from . import mcp

    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers", None)
    try:
        mcp.write_config_file(path, data)
    except OSError as exc:
        raise PluginError(f"写不进 {path}：{exc}") from exc


def guard_servers(plugin: str, servers: dict[str, dict[str, Any]]) -> None:
    """写盘前过一遍 MCP 准入规则（和 `xiaoyu mcp add` 同一个保存点）。

    插件包是从网上拉来的、里面就带着可执行声明——这道门比手敲一条声明时更该有。
    """
    from . import mcp_guard

    for server, entry in servers.items():
        args = [str(item) for item in entry.get("args") or []]
        env = {str(k): str(v) for k, v in (entry.get("env") or {}).items()}
        if reason := mcp_guard.admission_violation(str(entry["command"]), args, env):
            raise PluginError(f"插件 {plugin} 的 MCP server {server!r} 被安全规则拦下：{reason}")


def sync_mcp(plugin: str, servers: dict[str, dict[str, Any]]) -> list[str]:
    """把插件的 server 声明写进用户级 mcp.json，并摘掉该插件已不再带的旧条目。

    写的就是 mcp.json 本身（不是另起一套注册表）——`xiaoyu mcp list` 直接看得见，
    用户随时可以手改甚至手删，和 `xiaoyu mcp add` 两条路互相接管。
    """
    guard_servers(plugin, servers)
    path, data, existing = read_user_mcp()
    wanted = {mcp_server_name(plugin, server): entry for server, entry in servers.items()}
    kept = {
        name: entry
        for name, entry in existing.items()
        if not (isinstance(entry, dict) and entry.get(OWNER_KEY) == plugin)
        or name in wanted
    }
    for name, entry in wanted.items():
        kept[name] = {**entry, OWNER_KEY: plugin}
    write_user_mcp(path, data, kept)
    return sorted(wanted)


def drop_mcp(plugin: str) -> list[str]:
    """摘掉某个插件装的全部 MCP 条目。返回被摘掉的名字。"""
    check_name(plugin)
    path, data, existing = read_user_mcp()
    dropped = [
        name
        for name, entry in existing.items()
        if isinstance(entry, dict) and entry.get(OWNER_KEY) == plugin
    ]
    if not dropped:
        return []
    for name in dropped:
        existing.pop(name)
    write_user_mcp(path, data, existing)
    return sorted(dropped)


def installed_mcp(plugin: str) -> dict[str, dict[str, Any]]:
    """某个插件当前装着的 MCP 条目（键是加了命名空间的名字）。"""
    try:
        _, _, existing = read_user_mcp()
    except PluginError:
        return {}
    return {
        name: entry
        for name, entry in existing.items()
        if isinstance(entry, dict) and entry.get(OWNER_KEY) == plugin
    }


def strip_owner(entry: dict[str, Any]) -> dict[str, Any]:
    """去掉归属记号，好和包里声明的原始条目直接比。"""
    return {key: value for key, value in entry.items() if key != OWNER_KEY}


def same_servers(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """两组条目是否等价。整条比（不只是命令行）：上游改 timeout / disabled
    同样是变化，只比 command+args+env 的话这类改动既发现不了也应用不上。"""
    return {name: strip_owner(entry) for name, entry in left.items()} == {
        name: strip_owner(entry) for name, entry in right.items()
    }


def command_line(entry: dict[str, Any]) -> str:
    """一条 MCP 声明的完整命令行，装之前摊给用户看。"""
    return " ".join([str(entry.get("command", "?")), *(str(a) for a in entry.get("args") or [])])
