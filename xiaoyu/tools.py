"""小羽的工具集：read_file / write_file / bash。

设计要点：
- 工具是纯函数式的"执行 + 返回文本"，成功失败都返回字符串交给模型判断，
  只有真正的内部异常才向上抛。
- 路径一律相对 workspace 解析；逃出 workspace 的路径强制需要人工确认。
- 所有输出统一截断，避免一次 grep 把上下文打爆。
"""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import browser, mcp, sandbox
from .background import MONITOR_DEFAULT_TIMEOUT, TaskManager, kill_tree as _kill_tree
from .config import Config
from .rewind import RewindStore


#  硬性拦截：不可撤销的系统级破坏，
#  连 --yolo / auto_approve 都不放行——审批是"用户想不想"，这里是"绝不"。
#  只收录误伤概率极低的模式；宁可漏（还有人工审批兜底），不可错杀日常命令。
_HARDLINE_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), reason)
    for pattern, reason in [
        #  rm 递归删根/家目录（目标必须是裸 / ~ $HOME /*，/tmp/foo 这类不命中）
        (
            r"\brm\s+(?=(?:[^;|&]*\s)?-\w*r)[^;|&]*\s(?:/|/\*|~|~/|\$HOME/?|\"\$HOME\"/?)\s*(?:$|[;|&])",
            "rm 递归删除根目录或家目录",
        ),
        (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
        (r"\bmkfs(\.\w+)?\b", "格式化文件系统（mkfs）"),
        (r"\bdd\b[^;|&]*\bof=/dev/", "dd 直写块设备"),
        (r">\s*/dev/(sd[a-z]|disk\d|nvme\d)", "重定向覆写块设备"),
        (r"\bdiskutil\s+(erase|zero)", "diskutil 抹盘"),
        (r"(?i)\bformat\s+[a-z]:", "Windows format 格式化分区"),
        (
            r"(?i)\b(rd|rmdir)\s+/s\b[^;|&]*\s[a-z]:\\?\s*(?:$|[;|&])",
            "Windows 递归删除盘根",
        ),
        (
            r"(?i)Remove-Item\b[^;|&]*-Recurse[^;|&]*\s[a-z]:\\\s*(?:$|[;|&])",
            "PowerShell 递归删除盘根",
        ),
    ]
)


def hardline_violation(command: str) -> str | None:
    """命中硬性拦截规则时返回原因，否则 None。"""
    for pattern, reason in _HARDLINE_RULES:
        if pattern.search(command):
            return reason
    return None


def _decode_output(data: bytes | str | None) -> str:
    """子进程输出解码：UTF-8 优先，失败按本地代码页（GBK 等）兜底。

    中文 Windows 的 PowerShell 5.1 / 老 native 程序会按 OEM 代码页输出——
    硬解 UTF-8 会把「无法将 xx 识别为 cmdlet」这类关键报错变成一串 �，
    模型只能盲猜（真实会话里因此多绕了十几轮）。
    """
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    preferred = locale.getpreferredencoding(False) or ""
    for encoding in (preferred, "gbk"):
        if not encoding or encoding.replace("-", "").lower() == "utf8":
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _partial_chunks(stdout: bytes | str | None, stderr: bytes | str | None) -> str:
    """把 stdout/stderr 拼成给模型看的分段文本，空段不占行。"""
    chunks = []
    if out := _decode_output(stdout).strip():
        chunks.append(f"stdout:\n{out}")
    if err := _decode_output(stderr).strip():
        chunks.append(f"stderr:\n{err}")
    return "\n".join(chunks)


@functools.lru_cache(maxsize=1)
def _pwsh_path() -> str | None:
    """PowerShell 7 (pwsh) 的路径，没装返回 None。

    pwsh 相对预装的 5.1：默认 UTF-8 输出、支持 && / ||、native 命令的 stderr
    原样透传而不是包装成红色 ErrorRecord（5.1 的包装会把 CLI 输出的 JSON 拆碎）。
    有则优先。
    """
    return shutil.which("pwsh")


#  PS 5.1 对重定向的管道按系统 OEM 代码页（中文机是 GBK）编码输出，
#  先把两个输出编码都拨到 UTF-8，Python 侧才能稳定解码中文。
_PS51_UTF8_PREFIX = "$OutputEncoding=[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "


def _shell_argv(command: str) -> list[str]:
    """按平台选 shell。Windows 没有 /bin/bash，硬编码会让所有命令 WinError 2。"""
    if os.name == "nt":
        if pwsh := _pwsh_path():
            return [pwsh, "-NoProfile", "-Command", command]
        #  PowerShell 5.1 随 Windows 预装，语义比 cmd 稳定；-NoProfile 避免用户配置拖慢/干扰
        return ["powershell", "-NoProfile", "-Command", _PS51_UTF8_PREFIX + command]
    return ["/bin/bash", "-lc", command]


#  整树终止挪到了 background.py（后台任务的 watcher 也要用；安全函数只此一份），
#  本模块经顶部 import 以 _kill_tree 旧名继续使用。


_URL_PATTERN = re.compile(r"https?://\S+")
_AUTH_KEYWORDS = (
    "浏览器",
    "扫码",
    "二维码",
    "browser",
    "authoriz",
    "authenticat",
    "device",
    "oauth",
    "verify",
    "login",
    "扫描",
    "授权",
)


def _interactive_auth_hint(output: str) -> str:
    """超时输出里带浏览器授权链接时的附加提示。

    OAuth device flow / 登录扫码类命令会阻塞到用户完成授权——套在 timeout 里
    跑必然超时（真实会话里模型对同一条 auth login 连撞三次超时才换姿势）。
    检测到这种形态就把正确姿势直接告诉模型。
    """
    if not _URL_PATTERN.search(output):
        return ""
    low = output.lower()
    if not any(keyword in low for keyword in _AUTH_KEYWORDS):
        return ""
    return (
        "\n[提示] 输出中有需要用户在浏览器完成的授权链接。这类交互式认证命令"
        "阻塞等待只会超时：把链接原样展示给用户、请其完成授权，等用户答复后"
        "再用查询状态类命令确认，不要重复干等或反复重跑。"
    )


@functools.lru_cache(maxsize=1)
def non_inheritable_env_names() -> frozenset[str]:
    """模型跑的子进程**永远**拿不到的环境变量名（统一大写比较）。

    集合 = 小羽自己的密钥：网关 key、每个直连 provider 的 key（含别名）、
    serve 令牌。这些是"小羽调模型用的"，模型跑的命令没有任何正当理由需要
    它们——而 `env | grep KEY` 是最省事的外泄方式。
    名单从 providers.PRESETS 推导而不是手抄：新增 provider 自动纳入。
    例外同样从 PRESETS 推导：声明为 shared_key_envs 的多产品通用名
    （如 GOOGLE_API_KEY）"没有任何正当理由"不成立——gcloud 一类无关命令
    正当地需要它，剥了是误伤，故不进名单。
    """
    from .config import GATEWAY_KEY_ENVS
    from .providers import PRESETS

    names = set(GATEWAY_KEY_ENVS) | {"XIAOYU_SERVE_TOKEN"}
    shared: set[str] = set()
    for preset in PRESETS.values():
        names.update(preset.key_envs)
        shared.update(preset.shared_key_envs)
    return frozenset(name.upper() for name in names) - {name.upper() for name in shared}


def _hardened_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境：剔除 LD_* / DYLD_*（进程加固）与小羽自己的密钥。

    LD_PRELOAD / DYLD_INSERT_LIBRARIES 能往任何被执行的程序里注入代码——
    模型跑的命令不该继承这类注入通道。

    密钥剔除在 `extra` 合并**之后**做，且不区分大小写：否则 extra 里一个
    `deepseek_api_key` 就把刚剔掉的又塞回去。extra 里其它变量（宿主有意
    递给工具的 GITHUB_TOKEN 之类）是操作者的明确决定，照传。
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LD_", "DYLD_"))
    }
    if extra:
        env.update(extra)
    secrets = non_inheritable_env_names()
    return {key: value for key, value in env.items() if key.upper() not in secrets}


#  进程级 core dump 关闭只做一次（rlimit 跨 fork/exec 继承，见 _harden_core_limit）。
_core_limit_hardened = False


def _harden_core_limit() -> None:
    """在**父进程**里把 RLIMIT_CORE 压到 (0, 0)：命令崩溃时不把内存（可能含
    密钥）dump 到磁盘；本进程（内存里有 provider 密钥）崩溃时同样不落 core。

    绝不能退回 preexec_fn 方案（历史写法）：preexec_fn 强迫 CPython 放弃
    posix_spawn 改走 fork()，并在 fork 与 exec 之间的子进程里跑 Python 字节码
    （旧实现里还有一次 import）。xiaoyu 有常驻工作线程（browser 专用线程、
    七襄/斗巧线程池），fork 瞬间其他线程持有的锁（import 锁、分配器锁）在
    子进程里永远无人释放——子进程可能在 exec 前死锁，并带着继承的每个 fd
    一起挂住。tests/test_subprocess_hardening.py 的 AST 哨兵挡回归。

    rlimit 跨 fork/exec 继承，父进程设一次即可覆盖所有后代——包括 playwright
    自己拉起的浏览器进程，覆盖面比逐 spawn 设置更广。硬上限一并压到 0，
    后代无法自行抬回。
    """
    global _core_limit_hardened
    if _core_limit_hardened or os.name == "nt":
        return
    _core_limit_hardened = True
    import resource

    with contextlib.suppress(Exception):
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _subprocess_hardening() -> dict[str, Any]:
    """POSIX 子进程加固参数：独立会话（core dump 由 _harden_core_limit 进程级处理）。

    - start_new_session：新进程组/会话，防 TIOCSTI 类终端注入，也让超时杀得干净。
      不带 preexec_fn 时 CPython 可走 posix_spawn（POSIX_SPAWN_SETSID），
      比 fork 快且无 fork-with-threads 风险。
    Windows 没有这些语义，返回空。
    stdin 归属各调用点自定（MCP server 要 PIPE、工具命令要 DEVNULL），不在此统一。
    """
    if os.name == "nt":
        return {}
    _harden_core_limit()
    return {"start_new_session": True}


def _platform_shell_note() -> str:
    """拼进 bash 工具描述的平台提示：模型得知道自己在对谁说话。"""
    if os.name == "nt":
        if _pwsh_path():
            return (
                "本机是 Windows：命令由 PowerShell 7 (pwsh) 执行，"
                "用 PowerShell 写法（Get-ChildItem、Select-String、Get-Date…），"
                "不要用 bash/Unix 语法。"
            )
        return (
            "本机是 Windows：命令由 Windows PowerShell 5.1 执行，"
            "用 PowerShell 写法（Get-ChildItem、Select-String、Get-Date…），"
            "不要用 bash/Unix 语法。5.1 的已知坑："
            "不支持 && 和 ||，用 ; 分隔或分成多次调用；"
            "运行 .ps1 脚本可能被执行策略拦截，改用 powershell -ExecutionPolicy Bypass -File；"
            "native 程序写 stderr 会被包装成红色错误记录，是否真失败以 exit_status 为准。"
        )
    if sys.platform == "darwin":
        return "注意本机是 macOS：BSD sed/grep 不支持 GNU 语法，替换文本请用 perl。"
    return "本机是 Linux。"


#  需确认的工具在 schema 里注入的"目的"参数名：模型顺手写一句为何调用，
#  确认框里展示给用户——审批时"这条命令要干嘛"不用人肉猜。
#  执行前由 agent 剥离，不进 handler。
PURPOSE_PARAM = "__tool_use_purpose"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., str]
    #  fail-closed 的默认值方向：没声明的能力按最危险假设。
    #  第三方/插件工具忘了声明时，宁可多问一次，不能静默放行。
    #  真正只读的工具需要显式写 requires_approval=False。
    requires_approval: bool = True
    #  可用性探测：返回 False 时不进 schemas、拒绝执行。
    #  每次组装 schemas 都会调用，必须是廉价检查（which / 路径存在 / 列表非空）。
    check_fn: Callable[[], bool] | None = None

    def available(self) -> bool:
        if self.check_fn is None:
            return True
        try:
            return bool(self.check_fn())
        except Exception:  # noqa: BLE001 - 探测出错按不可用处理，不炸主流程
            return False

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的 function-calling schema。"""
        parameters = self.parameters
        #  只给需确认的工具注入目的参数：免确认的工具没人看这句话，纯浪费 token。
        #  浅拷贝后加键，不改动原 parameters（它可能被复用/被测试断言）。
        if self.requires_approval:
            parameters = {
                **parameters,
                "properties": {
                    **parameters.get("properties", {}),
                    PURPOSE_PARAM: {
                        "type": "string",
                        "description": "用一句话说明这次调用的目的（会展示在用户的确认提示里）",
                    },
                },
            }
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


#  第三方工具插件的 entry point 组名。
#  这是"接内部工具"的代码层通道：内部工具包只要在自己的 pyproject.toml 里声明
#      [project.entry-points."xiaoyu.tools"]
#      my_tool = "my_pkg.xiaoyu_plugin:make_tools"
#  被 pip install 后即被发现。入口是可调用：接收 Config，返回 Tool 或 Tool 列表。
PLUGIN_GROUP = "xiaoyu.tools"


def load_plugin_tools(config: Config) -> list[Tool]:
    """加载 entry_points 插件工具。单个插件坏了只警告，不拦启动。

    fail-closed：插件没显式声明 requires_approval 的，按 Tool 的默认值需要确认。
    """
    try:
        from importlib.metadata import entry_points

        candidates = entry_points(group=PLUGIN_GROUP)
    except Exception:  # noqa: BLE001 - 插件发现失败不能影响内置工具
        return []

    tools: list[Tool] = []
    for entry in candidates:
        try:
            made = entry.load()(config)
        except Exception as exc:  # noqa: BLE001 - 坏插件只警告
            print(f"[插件 {entry.name} 加载失败：{type(exc).__name__}: {exc}]", file=sys.stderr)
            continue
        for tool in made if isinstance(made, list) else [made]:
            if isinstance(tool, Tool):
                tools.append(tool)
            else:
                print(f"[插件 {entry.name} 返回了非 Tool 对象，已忽略]", file=sys.stderr)
    return tools


#  Unicode 标点归一表（容错匹配的最后一级）：
#  模型从 IDE/网页复制代码时最常混进来的"看着一样字节不同"的字符。
_UNICODE_PUNCT_TRANS = str.maketrans(
    {
        **{ord(ch): "-" for ch in "‐‑‒–—―−"},
        **{ord(ch): "'" for ch in "‘’‚‛"},
        **{ord(ch): '"' for ch in "“”„‟"},
        #  不换行空格、各宽度排版空格、全角空格 → 普通空格
        **{
            code: " "
            for code in (
                0x00A0, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
                0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x3000,
            )
        },
    }
)

#  逐级放宽的行归一化：任何一级出现「唯一命中」即采用（唯一性护栏不放松）。
#  精确匹配是第 0 级（在 _str_replace 主流程里），这里是 1-3 级。
_FUZZY_LEVELS: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("忽略行尾空白", lambda line: line.rstrip()),
    ("忽略首尾空白", lambda line: line.strip()),
    ("Unicode 标点归一", lambda line: line.translate(_UNICODE_PUNCT_TRANS).strip()),
)


@dataclass
class FuzzyMatch:
    line_index: int  # 命中起始行（0-based）
    line_count: int  # 匹配的行数
    level: str  # 用了哪一级归一化


def fuzzy_find_lines(text: str, old_str: str) -> FuzzyMatch | list[int] | None:
    """按行做逐级放宽的匹配。

    返回：唯一命中 → FuzzyMatch；某级多处命中 → 行号列表（1-based，报歧义用）；
    全部落空 → None。多行 old_str 末尾的空串是"结尾换行"的哨兵，参与匹配前
    先剥掉再重试。
    """
    text_lines = text.split("\n")
    pattern = old_str.split("\n")
    if len(pattern) > 1 and pattern[-1] == "":
        pattern = pattern[:-1]
    if not pattern:
        return None
    for level, norm in _FUZZY_LEVELS:
        normalized_text = [norm(line) for line in text_lines]
        normalized_pattern = [norm(line) for line in pattern]
        hits = [
            index
            for index in range(len(normalized_text) - len(normalized_pattern) + 1)
            if normalized_text[index : index + len(normalized_pattern)] == normalized_pattern
        ]
        if len(hits) == 1:
            return FuzzyMatch(hits[0], len(pattern), level)
        if len(hits) > 1:
            return [index + 1 for index in hits]
    return None


#  搜索/列目录时跳过的目录，避免噪声和把上下文撑爆
_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
}


def _locate_grep() -> str | None:
    """找一个能用的 grep：先看 PATH，Windows 上再探一次 Git for Windows 自带的。

    装了 Git 就有一个货真价实的 GNU grep 躺在 `Git\\usr\\bin\\grep.exe`，但只有
    `Git\\cmd` 会进 PATH，`usr\\bin` 不进——所以从 cmd/PowerShell 起的小羽里
    `which("grep")` 必然落空（Git Bash 里反而找得到，同一台机器两种结果）。
    捡起它比退到纯 Python 兜底快一个数量级，而装 Git 的概率几乎是 100%。

    不缓存结果：一次 which 的开销远小于一次搜索，换来的是用户中途装完
    ripgrep/Git 不必重启会话。

    路径拼接走 os.path 而非 Path：Path() 按调用时的 os.name 分派，测试里伪造
    Windows 只能 patch os.name，那会连带把 Path 变成 WindowsPath（在 POSIX 上
    一构造就抛 UnsupportedOperation）。同 cli.py 的 _running_launcher。
    """
    if found := shutil.which("grep"):
        return found
    if os.name != "nt":
        return None

    candidates: list[str] = []
    if git := shutil.which("git"):
        #  ...\Git\cmd\git.exe 或 ...\Git\bin\git.exe → ...\Git\usr\bin\grep.exe
        git_root = os.path.dirname(os.path.dirname(git))
        candidates.append(os.path.join(git_root, "usr", "bin", "grep.exe"))
    roots = [
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramW6432"),
        os.environ.get("ProgramFiles(x86)"),
    ]
    candidates += [
        os.path.join(root, "Git", "usr", "bin", "grep.exe") for root in roots if root
    ]
    #  用户级安装（不需要管理员那种）落在 %LOCALAPPDATA%\Programs\Git
    if local := os.environ.get("LOCALAPPDATA"):
        candidates.append(os.path.join(local, "Programs", "Git", "usr", "bin", "grep.exe"))

    for candidate in candidates:
        with contextlib.suppress(OSError):
            if os.path.isfile(candidate):
                return candidate
    return None


def _glob_matches(item: Path, root: Path, glob: str) -> bool:
    """纯 Python 搜索里的 glob 过滤，语义对齐 rg --glob：不含 / 的模式匹配文件名
    （任意层级都算），含 / 的模式匹配相对搜索起点的路径。"""
    if "/" not in glob:
        return fnmatch.fnmatch(item.name, glob)
    try:
        relative = item.relative_to(root).as_posix()
    except ValueError:
        relative = item.as_posix()
    return fnmatch.fnmatch(relative, glob.lstrip("/"))


class Toolbox:
    """持有 config 的工具集合。

    only 参数用来构造受限子集 —— explore 子 agent 拿到的就是 READONLY 那几个。
    **不能给子 agent bash**：bash 能写文件、能 pip install，"只读"就成了空话。
    """

    #  真正只读的工具集：不可能修改任何文件
    READONLY = ["read_file", "grep", "list_files"]

    def __init__(
        self,
        config: Config,
        only: list[str] | None = None,
        mcp_view: "mcp.McpView | None" = None,
    ) -> None:
        self.config = config
        self._tools: dict[str, Tool] = {}
        #  后台任务表（bash run_in_background / monitor）。通知回调由 Agent
        #  注入（agent.__init__ 里 tasks.notify = self.notify）；受限子集
        #  （explore 的 READONLY）没有 bash，这张表闲置无害。
        self.tasks = TaskManager()
        #  文件快照（/rewind）：write_file / str_replace 改前把原内容记进当前轮
        #  的快照点；轮次边界由 Agent.send 划（begin/finish）。
        self.rewind = RewindStore()
        #  已用 read_file 读过的文件 → 当时的 mtime。
        #  用来强制"先读再改"，并检测读完之后文件被外部改动。
        self._reads: dict[Path, float] = {}
        #  超长工具输出的落盘目录（spill）：
        #  懒创建，进程级临时目录。落盘失败退回纯截断，绝不影响工具本身。
        self._spill_dir: Path | None = None
        self._spill_seq = 0
        #  spill 召回表：短 id（序号字符串）→ 元信息。inline 预览只留头尾 + 一个
        #  短 id，中段随时用 recall(id, ...) 按需取回——id 只有一两个字符，压缩
        #  丢了 60 字符的临时路径也不影响召回（addressable recall）
        self._spills: dict[str, dict[str, Any]] = {}
        #  连续 read_file 次数，用于引导改用 explore
        self._read_streak = 0
        #  非 MCP 工具产出的图片（浏览器桥的截图等）：与 MCP 那份一起由 take_media 取走
        self._media: list[dict[str, Any]] = []
        self._media_lock = threading.Lock()
        self._register_builtins()
        #  插件工具排在所有内置工具之后（注册顺序 = 请求里的工具顺序 = prompt cache
        #  的前缀资产，内置/扩展分区拼接——插件变动不影响内置前缀）。
        #  only 子集（explore 用）不加载插件：受限集合里本来就没有它们。
        if only is None and config.enable_plugins:
            for tool in load_plugin_tools(config):
                #  不许覆盖内置工具：同名插件直接忽略（fail-closed）
                if tool.name not in self._tools:
                    self.register(tool)
        #  MCP server 工具：后台懒加载（不阻塞启动）。默认走**检索模式**：
        #  工具不进 schema，由 search_tool 按需
        #  检索、use_tool 调用——工具集会话内稳定，prompt cache 不再被中途连上
        #  的 server 作废。XIAOYU_MCP_TOOL_SEARCH=0 回到旧的全量注册
        #  （就绪后经 _absorb_mcp 追加在最尾部，append-only）。
        #  显式传入的 mcp_view 一律优先于配置发现（替代而非合并）：
        #  - 子 agent 继承父级 manager 的筛选视图（spec 的 mcp 字段，见
        #    agents.py）——绝不按子 workspace（可能是 worktree）再拉一批进程；
        #  - 嵌入宿主自建 manager（按宿主声明的 server 清单构造 ServerSpec +
        #    McpManager）后经此接线——server 生命周期归调用方，admission /
        #    OSV 闸在 server 启动层照常执行，不因注入而绕过。
        #  enable_mcp 只 gate 配置发现分支：显式注入=宿主明确要，开关不拦
        #  （宿主要关就别传 view，两个旋钮不叠加）。
        #  受限子集（only）没有 view 时也不 launch。
        self._mcp: Any
        if mcp_view is not None:
            self._mcp = mcp_view
        elif only is None:
            self._mcp = mcp.launch(config) if config.enable_mcp else None
        else:
            self._mcp = None
        self._mcp_search = self._mcp is not None and config.mcp_tool_search
        #  MCP server 上线公告的投递通道（Agent 注入 notify；None = 不公告，
        #  等注入后第一次组装 schemas 时补发）。已公告：server → 工具集指纹。
        self.notify_hook: Callable[[str, str], None] | None = None
        self._announced: dict[str, str] = {}
        if self._mcp_search:
            self._register_mcp_search()
        if only is not None:
            unknown = [name for name in only if name not in self._tools]
            if unknown:
                raise ValueError(f"未知工具：{unknown}")
            #  检索模式的两个元工具跟着视图走：不在 only 白名单语义里，
            #  它们是 MCP 继承的载体（use_tool 照常 requires_approval）
            keep = list(only) + (
                [name for name in ("search_tool", "use_tool") if name in self._tools]
                if self._mcp_search
                else []
            )
            self._tools = {name: self._tools[name] for name in keep}

    # ---------- 注册 / 查询 ----------

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def _register_mcp_search(self) -> None:
        """检索模式的两个元工具。它们自身常驻 schema（稳定），MCP 工具藏在后面。"""
        self.register(
            Tool(
                name="search_tool",
                description=(
                    "按关键词检索可用的 MCP 集成工具，返回匹配工具的确切参数 schema。"
                    "关键词带上 server 名和动作效果最好（如 \"linear create issue\"）；"
                    "直接给全限定工具名则精确返回那一个。调用 MCP 工具前必须先用它"
                    "拿到 schema——绝不要凭猜写参数名。status 为 partial 表示"
                    "还有 server 在连接中，结果可能不全。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "检索关键词（server 名 / 动作 / 工具名）",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多返回几个结果，默认 5",
                        },
                    },
                    "required": ["query"],
                },
                handler=self._search_mcp_tools,
                requires_approval=False,
                check_fn=lambda: self._mcp is not None,
            )
        )
        self.register(
            Tool(
                name="use_tool",
                description=(
                    "调用一个 MCP 集成工具。tool_name 是 search_tool 返回的全限定名"
                    "（形如 mcp__server__tool），tool_input 必须严格符合 search_tool "
                    "返回的参数 schema。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "全限定工具名（search_tool 的结果里抄）",
                        },
                        "tool_input": {
                            "type": "object",
                            "description": "传给该工具的参数对象（按其 schema 构造）",
                        },
                    },
                    "required": ["tool_name"],
                },
                handler=self._use_tool,
                requires_approval=True,
                check_fn=lambda: self._mcp is not None,
            )
        )

    def _ready_mcp_entries(self) -> list["mcp.RemoteTool"]:
        if self._mcp is None:
            return []
        return [remote for remote in self._mcp.ready_tools() if remote.check_fn()]

    def _search_mcp_tools(self, query: str, limit: int | None = None) -> str:
        from . import mcp_search

        available = self._ready_mcp_entries()
        entries = [
            mcp_search.Entry(
                name=remote.name,
                server=remote.server or "unknown",
                description=remote.description,
                parameters=remote.parameters,
            )
            for remote in available
        ]
        status = "partial" if self._mcp is not None and self._mcp.loading() else "ready"
        ranked = mcp_search.search(entries, str(query or ""), int(limit or 5))
        payload: dict[str, Any] = {
            "results": [
                {
                    "tool_name": entry.name,
                    "server": entry.server,
                    "score": round(score, 2),
                    "description": entry.description,
                    "input_schema": entry.parameters,
                }
                for entry, score in ranked
            ],
            "total_hidden_tools": len(entries),
            "status": status,
        }
        if not entries:
            payload["note"] = (
                "当前没有任何已就绪的 MCP 工具。"
                + ("还有 server 在连接中，稍后再试。" if status == "partial" else "")
            )
        elif not ranked:
            payload["note"] = "没有匹配的工具，换更贴近 server 名/动作的关键词再试。"
        return json.dumps(payload, ensure_ascii=False, indent=1)

    def _use_tool(self, tool_name: str, tool_input: Any = None) -> str:
        name = str(tool_name or "").strip()
        if tool_input is None:
            tool_input = {}
        elif isinstance(tool_input, str):
            #  宽进：模型偶尔把参数对象整个序列化成字符串
            try:
                tool_input = json.loads(tool_input)
            except json.JSONDecodeError:
                return "ERROR: tool_input 必须是 JSON 对象（按 search_tool 返回的 schema 构造）。"
        if not isinstance(tool_input, dict):
            return "ERROR: tool_input 必须是 JSON 对象。"
        if name in self._tools:
            #  原生工具纠偏：别绕 use_tool
            return (
                f"ERROR: {name} 是内置工具，不是 MCP 集成工具。"
                f"直接以 {name} 为名发起工具调用，不要经过 use_tool。"
            )
        if "__" not in name:
            return (
                f"ERROR: {name!r} 不是合法的 MCP 工具名。工具名是 search_tool 返回的"
                "全限定名（形如 mcp__server__tool），先用 search_tool 检索。"
            )
        remote = next(
            (item for item in (self._mcp.ready_tools() if self._mcp else []) if item.name == name),
            None,
        )
        if remote is None:
            return (
                f"ERROR: 没有名为 {name!r} 的 MCP 工具。用 search_tool 检索当前可用的工具。"
            )
        if not remote.check_fn():
            return f"ERROR: MCP 工具 {name} 当前不可用（server 未就绪或已退出）。"
        #  目的参数是给审批框看的，别漏进远端参数
        tool_input.pop(PURPOSE_PARAM, None)
        return remote.handler(**tool_input)

    def _announce_mcp(self) -> None:
        """检索模式的上线公告：server 就绪/工具集变化时经通知轨道告诉模型。

        不公告模型就不知道自己有哪些集成——工具已经不在 schema 里了。
        指纹 = 工具名集合的哈希，同 server 变更会以新 key 再公告一次。
        """
        if self.notify_hook is None:
            return
        groups: dict[str, list[str]] = {}
        for remote in self._ready_mcp_entries():
            #  指纹并入公告 key：代际 swap 只换 schema 不换名字时也要再公告一次
            groups.setdefault(remote.server or "unknown", []).append(
                f"{remote.name}:{remote.fingerprint}"
            )
        for server, names in sorted(groups.items()):
            digest = hashlib.sha256("\0".join(sorted(names)).encode("utf-8")).hexdigest()[:8]
            if self._announced.get(server) == digest:
                continue
            changed = server in self._announced
            self._announced[server] = digest
            verb = "工具集已更新" if changed else "已连接"
            self.notify_hook(
                f"MCP server「{server}」{verb}（{len(names)} 个工具）。"
                "要用 MCP 工具：先 search_tool 检索拿到确切参数 schema，"
                "再 use_tool 调用；绝不要凭猜写参数名。",
                f"mcp-online-{server}-{digest}",
            )

    def _absorb_mcp(self) -> None:
        """把已就绪的 MCP 工具追加注册进来（幂等：按名字去重）。

        MCP server 在后台线程里握手，就绪时机不定——每次查询工具集前同步一次。
        ready_tools 是 append-only 列表，注册顺序因此稳定，prompt cache 只在
        新工具首次出现时作废尾部。fail-closed：一律 requires_approval=True，
        server 自报的 readOnlyHint 不作为放行依据。

        检索模式（默认）不注册，只做上线公告——工具经 search_tool/use_tool 触达。
        """
        if self._mcp is None:
            return
        if self._mcp_search:
            self._announce_mcp()
            return
        for remote in self._mcp.ready_tools():
            existing = self._tools.get(remote.name)
            #  handler 同一 = 还是同一代的同一个声明；不同 = 代际 swap 换过
            #  （/mcp approve 后的新 schema）→ 原位覆盖（dict 赋值保序，
            #  schemas 顺序与 prompt cache 前缀不动）
            if existing is not None and existing.handler is remote.handler:
                continue
            self.register(
                Tool(
                    name=remote.name,
                    description=remote.description,
                    parameters=remote.parameters,
                    handler=remote.handler,
                    requires_approval=True,
                    #  server 进程退出后工具自动从 schemas 消失、拒绝执行
                    check_fn=remote.check_fn,
                )
            )

    def get(self, name: str) -> Tool | None:
        self._absorb_mcp()
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        #  顺序 = 注册顺序（dict 保序），会话内必须稳定：工具列表是每轮请求的
        #  前缀之一，顺序一变 prompt cache 全部作废。不要 sort、不要中途重排。
        self._absorb_mcp()
        return [tool.schema() for tool in self._tools.values() if tool.available()]

    def names(self) -> list[str]:
        self._absorb_mcp()
        return list(self._tools)

    def outside_workspace(self, args: dict[str, Any]) -> bool:
        """这次调用的路径参数是否逃出工作区。没有 path 参数按"没越界"算。

        公开出来是给 auto 档的放行判定用（modes.auto_approves）——
        "改的是不是工作区内的文件"这个判断只应该有一处实现。
        """
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return False
        try:
            resolved, outside = self._resolve(path)
        except OSError:
            #  路径解析不了（符号链接成环等）：按越界算，退回人工确认
            return True
        #  spill 落盘文件按"工作区内"对待：是我们自己写下的工具输出，
        #  取回它不该再弹一次确认——否则 spill 的取回指引形同虚设
        if outside and self._is_spill_path(resolved):
            return False
        return outside

    def _is_spill_path(self, resolved: Path) -> bool:
        """resolve 过的路径是否落在 spill 目录下（自家落盘的工具输出）。"""
        return self._spill_dir is not None and resolved.is_relative_to(self._spill_dir)

    def needs_approval(self, name: str, args: dict[str, Any]) -> bool:
        tool = self.get(name)
        if tool is None:
            return False
        if tool.requires_approval:
            return True
        #  读操作本身安全，但读到 workspace 外面去了仍然要问。
        return self.outside_workspace(args)

    def run(self, name: str, args: dict[str, Any]) -> str:
        """执行工具。参数错误/文件不存在等都作为文本结果返回给模型。"""
        tool = self.get(name)
        if tool is None:
            return f"ERROR: 未知工具 {name!r}。可用工具：{', '.join(self.names())}"
        if not tool.available():
            return f"ERROR: 工具 {name} 当前不可用（环境探测未通过），换其它办法。"

        #  连续读文件计数：用任何其它工具即重置
        if name == "read_file":
            self._read_streak += 1
        else:
            self._read_streak = 0

        blocked = self._streak_block_message(name)
        if blocked:
            return blocked

        try:
            output = self._bound_output(name, tool.handler(**args))
        except TypeError as exc:
            return f"ERROR: 调用 {name} 的参数不对：{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具错误要回给模型自愈
            return f"ERROR: {type(exc).__name__}: {exc}"

        note = self._streak_warn_note(name)
        return f"{output}\n\n{note}" if note else output

    def take_media(self) -> list[dict[str, Any]]:
        """本批工具调用产出的图片部件（取完即清）。来源：MCP 工具，以及经 push_media 交图的进程内工具（浏览器桥截图）。

        走 Toolbox 中转而不是让 agent 直接问 mcp：子 agent / 受限工具集
        （only=READONLY）根本没有 _mcp，调用方不该为此写分支。
        """
        with self._media_lock:
            own, self._media = self._media, []
        remote = self._mcp.take_media() if self._mcp is not None else []
        return own + remote

    def push_media(self, part: dict[str, Any]) -> None:
        """进程内工具交一张图（`media.image_part(ref)`），下一次 take_media 一并取走。"""
        with self._media_lock:
            self._media.append(part)

    @property
    def mcp_manager(self) -> Any:
        """本工具箱挂着的 MCP manager（或视图，或 None）。

        公开出来给声明式 subagent 做继承（agents.py 构造 McpView 用），
        不然它得伸手摸私有字段。"""
        return self._mcp

    # ---------- 连续读文件的引导与拦截 ----------

    def _explore_available(self) -> bool:
        return self.get("explore") is not None

    def _streak_block_message(self, name: str) -> str | None:
        if name != "read_file" or not self._explore_available():
            return None
        if self._read_streak < self.config.read_streak_block:
            return None
        return (
            f"ERROR: 你已连续 {self._read_streak - 1} 次 read_file，这是「逐个翻文件」的模式，"
            "会把这些内容永久堆在上下文里（实测比委托检索多花一倍 token）。\n"
            "改用 explore：把你想查清的问题描述给它，它用便宜模型只读检索并返回"
            "带 路径:行号 + 原文行 的结论。\n"
            "如果你确实是在读「马上要动手改」的那几个文件，先调用一次别的工具"
            "（如 grep 定位、或直接开始 str_replace），计数会重置。"
        )

    def _streak_warn_note(self, name: str) -> str | None:
        if name != "read_file" or not self._explore_available():
            return None
        if not (self.config.read_streak_warn <= self._read_streak < self.config.read_streak_block):
            return None
        remaining = self.config.read_streak_block - self._read_streak
        return (
            f"[提示] 你已连续读了 {self._read_streak} 个文件。"
            f"如果是在摸清代码而不是准备修改，请改用 explore（便宜模型只读检索，不占你的上下文）。"
            f"再连续读 {remaining} 次会被拦截。"
        )

    # ---------- 内部工具 ----------

    def _register_builtins(self) -> None:
        self.register(
            Tool(
                name="read_file",
                description=(
                    "读取文本文件内容。路径相对当前工作区解析。"
                    "大文件可以用 offset（起始行号，从 1 开始）和 limit（最多读多少行）只读一段。"
                    "注意：只读了一段的文件不算「读过」，覆盖或改动它之前仍需完整读一遍。"
                    "修改任何文件之前必须先完整读它。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "offset": {
                            "type": "integer",
                            "description": "起始行号，从 1 开始；省略表示从头读",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最多读多少行；省略表示读到结尾",
                        },
                    },
                    "required": ["path"],
                },
                handler=self._read_file,
                requires_approval=False,
            )
        )
        self.register(
            Tool(
                name="write_file",
                description=(
                    "把完整内容写入文件（整文件覆盖，不是补丁）。"
                    "只在新建文件或需要全量重写时用；改动已有文件的局部请用 str_replace。"
                    "覆盖已有文件前必须先用 read_file 读过它。"
                    "父目录不存在会自动创建。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "content": {
                            "type": "string",
                            "description": "文件的完整新内容",
                        },
                    },
                    "required": ["path", "content"],
                },
                handler=self._write_file,
                requires_approval=True,
            )
        )
        self.register(
            Tool(
                name="str_replace",
                description=(
                    "把文件里的一段文本精确替换成另一段——修改已有文件的首选工具。"
                    "调用前必须先用 read_file 读过该文件。"
                    "old_str 必须与文件内容逐字符完全一致（含缩进和空行），"
                    "并且在整个文件里只能出现一次；"
                    "如果不唯一，就把上下文往外扩几行直到唯一。"
                    "删除代码就把 new_str 设为空字符串。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件路径"},
                        "old_str": {
                            "type": "string",
                            "description": "要被替换掉的原文，必须逐字符精确且在文件中唯一",
                        },
                        "new_str": {
                            "type": "string",
                            "description": "替换成的新文本，留空表示删除",
                        },
                    },
                    "required": ["path", "old_str", "new_str"],
                },
                handler=self._str_replace,
                requires_approval=True,
            )
        )
        self.register(
            Tool(
                name="grep",
                description=(
                    "在工作区里按正则搜索文本，返回 路径:行号: 内容。"
                    "找符号定义、找调用点、找配置项都用它。"
                    "可用 glob 限定文件类型（如 *.py）。这是只读操作。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "正则表达式"},
                        "path": {
                            "type": "string",
                            "description": "搜索起点，默认工作区根目录",
                        },
                        "glob": {
                            "type": "string",
                            "description": "文件名 glob 过滤，如 *.py；省略表示不过滤",
                        },
                        "max_matches": {
                            "type": "integer",
                            "description": "最多返回多少条，默认 200；结果被截断时会提示总数",
                        },
                    },
                    "required": ["pattern"],
                },
                handler=self._grep,
                requires_approval=False,
            )
        )
        self.register(
            Tool(
                name="recall",
                description=(
                    "召回之前因超长而只留了头尾预览的工具输出。预览里给了「召回 id」，"
                    "用它取回被省略的中段，不必重跑原命令。"
                    "不给 id：列出本会话所有可召回的输出（id / 来源 / 规模 / 首行）。"
                    "给 id + pattern：在该输出里按正则找匹配行（带行号）。"
                    "给 id + offset/limit：取该输出的某一段行。这是只读操作。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "召回 id（预览里给的）；省略则列出全部"},
                        "pattern": {"type": "string", "description": "正则：在该输出里找匹配行"},
                        "offset": {"type": "integer", "description": "起始行号（从 1）；与 limit 配合取一段"},
                        "limit": {"type": "integer", "description": "最多取多少行"},
                    },
                    "required": [],
                },
                handler=self._recall,
                requires_approval=False,
                check_fn=lambda: bool(self._spills),
            )
        )
        self.register(
            Tool(
                name="list_files",
                description=(
                    "列出工作区里的文件（可用 glob 过滤，如 **/*.py）。"
                    "摸清项目结构时用它，比 bash ls 更干净：自动跳过 .git、"
                    "node_modules、__pycache__ 等噪声目录。这是只读操作。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "glob 模式，默认 **/*",
                        },
                        "path": {"type": "string", "description": "起点，默认工作区根目录"},
                        "limit": {"type": "integer", "description": "最多列多少个，默认 300"},
                    },
                    "required": [],
                },
                handler=self._list_files,
                requires_approval=False,
            )
        )
        self.register(
            Tool(
                name="bash",
                description=(
                    "在工作区目录下执行 shell 命令，返回合并后的 stdout/stderr 和退出码。"
                    "命令已经在工作区根目录下执行，不需要自己 cd 过去。"
                    "用它来查找文件、跑测试、跑构建、查 git 状态。"
                    "需要用户在浏览器完成的交互式认证命令（OAuth device flow、登录扫码）"
                    "不要阻塞干等：先用能立即返回的方式发起并拿到授权链接，"
                    "把链接展示给用户，等用户确认完成后再查状态。"
                    "长时间运行的命令（dev server、长构建、部署后的收敛等待）用 "
                    "run_in_background=true：立即返回 task id，完成时会在后续工具结果里"
                    "收到通知——不要轮询、不要 sleep 干等，也不要在命令末尾自己加 &。"
                    + _platform_shell_note()
                    + self._sandbox_note()
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "要执行的命令"},
                        "timeout": {
                            "type": "integer",
                            "description": (
                                f"超时秒数，默认 {self.config.bash_timeout}。"
                                "超时的命令会被终止，已产生的输出仍会返回给你；"
                                "预计耗时长的命令（完整测试、构建）请显式调大。"
                                "后台任务默认不受超时约束，显式给了才生效"
                            ),
                        },
                        "run_in_background": {
                            "type": "boolean",
                            "description": (
                                "true = 后台运行：立即返回 task id，命令继续跑，"
                                "完成时自动通知你。用 task_output 查看输出、kill_task 终止"
                            ),
                        },
                        #  升权协议：只有沙箱真的生效时这两个
                        #  参数才出现在 schema 里——没有沙箱就没有权限可升
                        **(
                            {
                                "sandbox_permissions": {
                                    "type": "string",
                                    "enum": self._escalation_modes(),
                                    "description": (
                                        "为**这一次**调用申请更高的沙箱权限（需用户批准，"
                                        "仅本次生效）。只在命令确实被沙箱拦截时使用，"
                                        "选最窄的足够档位，必须与 justification 成对出现。"
                                    ),
                                },
                                "justification": {
                                    "type": "string",
                                    "description": (
                                        "一句给用户看的解释：为什么这条命令需要升权。"
                                        "必须与 sandbox_permissions 成对出现。"
                                    ),
                                },
                            }
                            if self._escalation_modes()
                            else {}
                        ),
                    },
                    "required": ["command"],
                },
                handler=self._bash,
                requires_approval=True,
            )
        )
        self.register(
            Tool(
                name="task_output",
                description=(
                    "查看后台任务（run_in_background 的命令 / monitor）的输出与状态。"
                    "默认立即返回快照；要等任务结束就给 timeout（秒），到点仍在跑也会"
                    "返回现状。不要反复轮询——任务完成时你会自动收到通知；"
                    "需要等待就用一次带 timeout 的调用等到位。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "task_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要查看的任务 id 列表（单个也写成数组）",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "最多等多少秒（上限 600）；省略或 0 = 立即返回快照",
                        },
                    },
                    "required": ["task_ids"],
                },
                handler=self._task_output,
                requires_approval=False,
                check_fn=lambda: bool(self.tasks.known_ids()),
            )
        )
        self.register(
            Tool(
                name="kill_task",
                description="终止一个后台任务或 monitor（整棵进程树）。已结束的任务返回其退出码。",
                parameters={
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "要终止的任务 id"}
                    },
                    "required": ["task_id"],
                },
                handler=self._kill_task,
                requires_approval=False,
                check_fn=lambda: bool(self.tasks.known_ids()),
            )
        )
        self.register(
            Tool(
                name="monitor",
                description=(
                    "启动一个后台观察进程：命令的 stdout 每输出一行就是一个事件，"
                    "会自动送达给你，进程退出即结束观察。适用：盯 CI 状态、tail 日志、"
                    "轮询外部条件。**输出量纪律**：每一行都会打进你的上下文，"
                    "脚本只应输出关键事件（如 DONE / FAILED），管道过滤用 "
                    "grep --line-buffered（裸 grep 会整块缓冲，事件迟到几分钟）。"
                    "输出太快会被限流，持续刷屏会被自动终止。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "观察脚本/命令。每行 stdout 是一个事件，退出即结束",
                        },
                        "description": {
                            "type": "string",
                            "description": "在观察什么（每条事件通知都会带上这句话）",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": f"最长观察秒数，默认 {MONITOR_DEFAULT_TIMEOUT}（10 小时）",
                        },
                    },
                    "required": ["command", "description"],
                },
                handler=self._monitor,
                requires_approval=True,
            )
        )
        self.register(
            Tool(
                name="browser",
                description=(
                    "操作浏览器（Playwright）。action 取值："
                    "open=打开 url 并返回页面快照；"
                    "snapshot=当前页面快照（YAML，交互元素带 [ref=eN]）；"
                    "click=点击、fill=填输入框（配 text），selector 用快照里的 ref"
                    "（写作 aria-ref=e5）或 Playwright 选择器（text=、css）；"
                    "press=按键（key 如 Enter）；read=页面正文纯文本；"
                    "screenshot=截图存到 path（给用户看的，不会回传给你）；"
                    "close=关闭浏览器。"
                    "默认无头启动独立 Chromium（无登录态）。要操作需要登录态的页面，"
                    "请用户以 --remote-debugging-port=9222 启动本机 Chrome 并设"
                    " XIAOYU_BROWSER_CDP=http://127.0.0.1:9222，即可接管已登录会话。"
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": list(browser.ACTIONS)},
                        "url": {"type": "string", "description": "open 用"},
                        "selector": {"type": "string", "description": "click/fill 用"},
                        "text": {"type": "string", "description": "fill 用"},
                        "key": {"type": "string", "description": "press 用"},
                        "path": {"type": "string", "description": "screenshot 用"},
                    },
                    "required": ["action"],
                },
                handler=self._browser,
                requires_approval=True,
                #  lambda 晚绑定而非直接引用：测试/运行期 monkeypatch
                #  browser.available 时门控要跟着变。enable_browser 也进门控：
                #  可用性绑在宿主装没装 playwright 上，不给开关就没法把工具表
                #  钉死（e2e golden 曾因 CI runner 镜像预装 playwright 而漂移）
                check_fn=lambda: self.config.enable_browser and browser.available(),
            )
        )

    def _browser(
        self,
        action: str,
        url: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        key: str | None = None,
        path: str | None = None,
    ) -> str:
        return browser.session().run(
            action, url=url, selector=selector, text=text, key=key, path=path
        )

    def _sandbox_note(self) -> str:
        """拼进 bash 描述的沙箱边界说明：模型得先知道墙在哪，才不会撞了才学。"""
        if not sandbox.enabled(self.config.sandbox):
            return ""
        note = (
            "命令在沙箱里执行：**只能写工作区、临时目录和构建缓存**，"
            "写其它路径会返回 Operation not permitted / Read-only file system"
            "（这是策略拦截，不是命令写错）。全盘可读。"
        )
        note += "" if self.config.sandbox_network else "网络已禁用，联网命令会失败。"
        return note + (
            "确需更高权限时用最窄档位的 sandbox_permissions + justification "
            "原样重试那一条命令（需用户批准，仅该次调用生效）。"
        )

    def _escalation_modes(self) -> list[str]:
        """当前沙箱配置下可申请的升权档位（从窄到宽；未套沙箱时为空）。

        升权词汇**只能向上**——档位表按当前配置生成，
        已经放行的能力不会出现在表里（网络已放行就没有 allow-network 档），
        所以"非扩大的请求"在 schema 层面就不成立，撞进来的按参数错误拒绝。
        """
        if not sandbox.enabled(self.config.sandbox):
            return []
        modes_list: list[str] = []
        if not self.config.sandbox_network:
            modes_list.append("allow-network")
        modes_list.append("danger-full-access")
        return modes_list

    def _read_file(self, path: str, offset: int | None = None, limit: int | None = None) -> str:
        target, outside = self._resolve(path)
        #  spill 文件不打"工作区之外"标注：那是我们自己落盘的工具输出
        if outside and self._is_spill_path(target):
            outside = False
        if not target.exists():
            return f"ERROR: 文件不存在：{path}"
        if target.is_dir():
            return f"ERROR: {path} 是目录，不是文件。用 list_files 看目录。"
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: 读取失败 {path}: {exc}"

        prefix = "(注意：该路径在工作区之外)\n" if outside else ""
        if not text:
            self._mark_read(target)
            return f"{prefix}(文件为空：{path})"

        if offset is None and limit is None:
            #  只有完整读过，才算"读过"——部分读不足以授权覆盖整个文件
            self._mark_read(target)
            return f"{prefix}{text}"

        lines = text.splitlines()
        start = max(1, offset or 1)
        if start > len(lines):
            return f"ERROR: {path} 只有 {len(lines)} 行，offset={start} 超出范围"
        end = len(lines) if limit is None else min(len(lines), start + max(1, limit) - 1)
        chunk = "\n".join(lines[start - 1 : end])
        note = (
            f"{prefix}[{path} 第 {start}-{end} 行，共 {len(lines)} 行；"
            "这是部分内容，改动此文件前需完整读一遍]\n"
        )
        return note + chunk

    def _write_file(self, path: str, content: str) -> str:
        target, outside = self._resolve(path)
        existed = target.exists()
        if existed:
            guard = self._guard_known(target, path)
            if guard:
                return guard
        #  /rewind 快照：改前内容（新建文件记 None——回滚即删除）
        if existed:
            try:
                self.rewind.record(target, target.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
        else:
            self.rewind.record(target, None)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: 写入失败 {path}: {exc}"
        self._mark_read(target)
        action = "已覆盖" if existed else "已创建"
        note = "（工作区之外）" if outside else ""
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"{action}{note} {path}：{len(content)} 字符 / {lines} 行"

    def _str_replace(self, path: str, old_str: str, new_str: str) -> str:
        target, outside = self._resolve(path)
        if not target.exists():
            return f"ERROR: 文件不存在：{path}。新建文件请用 write_file。"
        if target.is_dir():
            return f"ERROR: {path} 是目录，不是文件。"
        if not old_str:
            return "ERROR: old_str 不能为空。要新建或整体重写文件请用 write_file。"
        if old_str == new_str:
            return "ERROR: old_str 和 new_str 完全相同，这次替换没有意义。"

        guard = self._guard_known(target, path)
        if guard:
            return guard

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: 读取失败 {path}: {exc}"

        count = text.count(old_str)
        if count == 0:
            #  精确匹配落空 → 逐级放宽的容错匹配。
            #  行尾空格 / tab-space 混用 / IDE 弯引号是三类最高频的"看着一样字节不同"，
            #  只要放宽后唯一命中就接受；多处命中仍打回——唯一性护栏不放松。
            return self._fuzzy_replace(target, path, text, old_str, new_str, outside)
        if count > 1:
            return (
                f"ERROR: old_str 在 {path} 中出现了 {count} 次，无法确定改哪一处。"
                f"请把上下文往外扩几行让它唯一。出现位置在第 "
                f"{', '.join(str(n) for n in self._match_lines(text, old_str))} 行附近。"
            )

        offset = text.index(old_str)
        #  多行替换时如果 old_str 不是从行首开始，new_str 的后续行会丢掉原有缩进。
        if "\n" in new_str and offset > 0 and text[offset - 1] not in "\n":
            return (
                f"ERROR: old_str 从行中间开始（第 {text.count(chr(10), 0, offset) + 1} 行），"
                "而 new_str 是多行的——这样替换会让新增的行丢掉缩进。"
                "请把 old_str 扩展到从行首开始（含该行的缩进）再试。"
            )
        line_no = text.count("\n", 0, offset) + 1
        updated = text.replace(old_str, new_str, 1)
        self.rewind.record(target, text)
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: 写入失败 {path}: {exc}"
        self._mark_read(target)

        note = "（工作区之外）" if outside else ""
        delta = updated.count("\n") - text.count("\n")
        action = "删除" if not new_str else "替换"
        return (
            f"已{action}{note} {path} 第 {line_no} 行处的内容，行数变化 {delta:+d}。\n"
            f"改动后该处上下文：\n{self._context_around(updated, offset)}"
        )

    def _fuzzy_replace(
        self,
        target: Path,
        path: str,
        text: str,
        old_str: str,
        new_str: str,
        outside: bool,
    ) -> str:
        """old_str 精确匹配落空后的容错路径。找不到时回显 old_str 帮模型自查。"""
        found = fuzzy_find_lines(text, old_str)
        if isinstance(found, list):
            return (
                f"ERROR: old_str 精确匹配失败；放宽空白/标点后在 {path} 的第 "
                f"{', '.join(str(n) for n in found[:8])} 行有多处近似命中，无法确定改哪一处。"
                "请把上下文往外扩几行让它唯一。"
            )
        if found is None:
            echo = old_str if len(old_str) <= 800 else old_str[:800] + "\n…（过长截断）"
            return (
                f"ERROR: 在 {path} 中找不到 old_str（连放宽空白/标点也没匹配上）。"
                "它必须照抄原文，常见原因是凭记忆写而非照抄、或文件内容与你的预期不同。"
                f"{self._near_miss_hint(text, old_str)}\n"
                f"你提供的 old_str 原文如下，请对照 read_file 的结果找差异：\n{echo}"
            )

        text_lines = text.split("\n")
        if new_str:
            new_lines = new_str.split("\n")
            #  old_str 的结尾换行哨兵在匹配前被剥掉了，new_str 的也要对称剥掉，
            #  否则会凭空多出一个空行
            if len(new_lines) > 1 and new_lines[-1] == "" and old_str.endswith("\n"):
                new_lines = new_lines[:-1]
            #  缩进修正：strip 级容错意味着 old_str 的缩进和原文对不上（tab-space
            #  混用、层级抄错），new_str 的缩进多半跟着错。用首行的缩进差把
            #  new_str 整体映射回原文的缩进，别把错的缩进写进文件。
            pattern = old_str.split("\n")
            if len(pattern) > 1 and pattern[-1] == "":
                pattern = pattern[:-1]
            original_first = text_lines[found.line_index]
            original_indent = original_first[: len(original_first) - len(original_first.lstrip())]
            pattern_indent = pattern[0][: len(pattern[0]) - len(pattern[0].lstrip())]
            if original_indent != pattern_indent:
                new_lines = [
                    original_indent + line[len(pattern_indent):]
                    if line.strip() and line.startswith(pattern_indent)
                    else line
                    for line in new_lines
                ]
        else:
            new_lines = []
        updated_lines = (
            text_lines[: found.line_index]
            + new_lines
            + text_lines[found.line_index + found.line_count :]
        )
        updated = "\n".join(updated_lines)
        self.rewind.record(target, text)
        try:
            target.write_text(updated, encoding="utf-8")
        except OSError as exc:
            return f"ERROR: 写入失败 {path}: {exc}"
        self._mark_read(target)

        offset = sum(len(line) + 1 for line in updated_lines[: found.line_index])
        note = "（工作区之外）" if outside else ""
        delta = updated.count("\n") - text.count("\n")
        action = "删除" if not new_str else "替换"
        return (
            f"已{action}{note} {path} 第 {found.line_index + 1} 行处的内容，"
            f"行数变化 {delta:+d}。\n"
            f"[注意] old_str 与原文不完全一致，已按「{found.level}」容错匹配。"
            "请核对下方上下文确认改动落点正确：\n"
            f"{self._context_around(updated, min(offset, len(updated)))}"
        )

    def _grep(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        max_matches: int = 200,
    ) -> str:
        """只读搜索。优先 rg，其次 grep -rnE（macOS 的 BSD grep 没有 -P，
        所以用 -E 不用 -P），两个都没有就走纯 Python 兜底。

        兜底不是可选项：Windows 上默认既没有 ripgrep 也没有 grep.exe，直接
        subprocess 会抛 [WinError 2]，模型每次搜索都白烧一轮、只能退化成一个个
        read_file 硬啃——explore 子 agent 的 12 轮上限就是这么被烧光的。
        """
        target, _ = self._resolve(path)
        if not target.exists():
            return f"ERROR: 路径不存在：{path}"

        if shutil.which("rg"):
            command = ["rg", "--line-number", "--no-heading", "--color", "never", "-e", pattern]
            for skip in sorted(_SKIP_DIRS):
                command += ["--glob", f"!{skip}/**"]
            if glob:
                command += ["--glob", glob]
            command.append(str(target))
        elif grep := _locate_grep():
            command = [grep, "-rnE", pattern, str(target)]
            for skip in sorted(_SKIP_DIRS):
                command.append(f"--exclude-dir={skip}")
            if glob:
                command.append(f"--include={glob}")
        else:
            return self._grep_python(pattern, target, glob, max_matches)

        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                #  Windows 默认 locale 编码（GBK/cp1252）解不了 UTF-8 输出，必须显式指定
                encoding="utf-8",
                errors="replace",
                timeout=60,
                cwd=str(self.config.workspace),
                env=_hardened_env(),
                **_subprocess_hardening(),
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: 搜索超时：{pattern}"
        except OSError:
            #  which 说有、真跑起来却没有（PATH 里挂着失效的 shim 等），照样兜底，
            #  别把一个可恢复的环境问题变成模型眼里的死路
            return self._grep_python(pattern, target, glob, max_matches)

        #  grep/rg 没匹配到时退出码是 1，这不是错误
        if result.returncode not in (0, 1):
            return f"ERROR: 搜索失败（exit {result.returncode}）：{result.stderr.strip()[:200]}"

        lines = [line for line in result.stdout.splitlines() if line.strip()]
        return self._grep_output(lines, pattern, max_matches)

    def _grep_python(
        self,
        pattern: str,
        target: Path,
        glob: str | None,
        max_matches: int,
    ) -> str:
        """没有 rg / grep 时的纯 Python 搜索，输出格式与它们保持一致。

        只求"能用"不求快：跳过 _SKIP_DIRS、二进制和超大文件，并留一个整体时间
        预算，免得在大仓库上把一轮工具调用耗死。
        """
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return f"ERROR: 正则不合法：{exc}"

        candidates = [target] if target.is_file() else sorted(target.rglob("*"))
        deadline = time.monotonic() + 60
        lines: list[str] = []
        timed_out = False
        for item in candidates:
            if time.monotonic() > deadline:
                timed_out = True
                break
            if not item.is_file() or _SKIP_DIRS & set(item.parts):
                continue
            if glob and not _glob_matches(item, target, glob):
                continue
            try:
                if item.stat().st_size > 2_000_000:
                    continue
                with item.open("rb") as raw:
                    if b"\0" in raw.read(4096):
                        continue
                with item.open("r", encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, 1):
                        if regex.search(line):
                            lines.append(f"{item}:{number}:{line.rstrip()}")
            except OSError:
                continue

        hint = (
            "\n[提示] 本机没有 ripgrep/grep，已用内置搜索兜底"
            "（装 ripgrep 会快很多：winget install BurntSushi.ripgrep.MSVC）"
        )
        if timed_out:
            hint += "\n[提示] 已达 60 秒搜索预算，结果可能不完整，请缩小 path 或 glob 范围"
        return self._grep_output(lines, pattern, max_matches) + hint

    def _grep_output(self, lines: list[str], pattern: str, max_matches: int) -> str:
        """把 路径:行号:内容 的原始行整理成给模型看的结果：路径转成工作区相对、
        分隔符统一正斜杠（Windows 的反斜杠会让模型在后续工具调用里混用两种）。"""
        if not lines:
            return f"没有匹配：{pattern}"

        root = str(self.config.workspace)
        shown = []
        for line in lines[:max_matches]:
            for prefix in (root + os.sep, root + "/"):
                if line.startswith(prefix):
                    line = line[len(prefix):]
                    break
            head, sep, rest = line.partition(":")
            if sep:
                line = head.replace("\\", "/") + sep + rest
            shown.append(line[:300])
        note = (
            f"\n... 还有 {len(lines) - max_matches} 条未显示，请缩小范围"
            if len(lines) > max_matches
            else ""
        )
        return f"{len(lines)} 条匹配：\n" + "\n".join(shown) + note

    def _list_files(self, pattern: str = "**/*", path: str = ".", limit: int = 300) -> str:
        target, _ = self._resolve(path)
        if not target.is_dir():
            return f"ERROR: 不是目录：{path}"

        found: list[str] = []
        for item in sorted(target.glob(pattern)):
            if not item.is_file():
                continue
            if _SKIP_DIRS & set(item.parts):
                continue
            try:
                #  统一正斜杠：输出给模型的路径不随平台漂移（Windows 反斜杠会让
                #  模型在后续 str_replace/read_file 里混用两种分隔符）
                found.append(item.relative_to(self.config.workspace).as_posix())
            except ValueError:
                found.append(str(item))

        if not found:
            return f"没有匹配 {pattern} 的文件"
        note = f"\n... 共 {len(found)} 个，只列出前 {limit} 个" if len(found) > limit else ""
        return f"{len(found)} 个文件：\n" + "\n".join(found[:limit]) + note

    def _command_argv(self, command: str, escalation: str | None = None) -> list[str]:
        """命令 → 实际执行的 argv（平台 shell + 需要时套沙箱）。

        前台 bash、后台任务、monitor 三条路必须同一份拼装——沙箱是否套上
        不能因执行形态而异。escalation 是**本次调用**获批的升权档位：
        allow-network = 仍套沙箱但放行网络；danger-full-access = 本次不套沙箱。
        """
        argv = _shell_argv(command)
        if escalation == "danger-full-access":
            return argv
        if sandbox.enabled(self.config.sandbox):
            allow_network = self.config.sandbox_network or escalation == "allow-network"
            argv = sandbox.wrap(argv, self.config.workspace, allow_network)
        return argv

    def _spawn_background(
        self,
        command: str,
        *,
        kind: str,
        description: str,
        timeout: float | None,
        escalation: str | None = None,
    ) -> str:
        """后台进程的统一入口：进程策略在这里拼好，生命周期交给 TaskManager。"""
        if reason := hardline_violation(command):
            return (
                f"ERROR: 命令被硬性拦截（{reason}）。"
                "这类不可撤销的破坏性操作在任何模式下都不执行，包括 --yolo。"
            )
        started = self.tasks.start(
            self._command_argv(command, escalation),
            command=command,
            kind=kind,
            description=description,
            cwd=str(self.config.workspace),
            env=_hardened_env(self.config.extra_env),
            timeout=timeout,
            popen_extra=_subprocess_hardening(),
        )
        if isinstance(started, str):
            return started
        if kind == "monitor":
            return (
                f"monitor 已启动（{started.task_id}，最长观察 {timeout:.0f}s）。\n"
                "每个事件都会自动送达，继续干别的活——不要轮询、不要 sleep 等待。\n"
                "事件可能在你等用户答复时到达——事件不是用户的回复。"
            )
        note = "" if timeout is None else f"，{timeout:.0f}s 后仍未结束会被终止"
        warn = ""
        others = [task for task in self.tasks.running() if task.task_id != started.task_id]
        if others:
            listed = "；".join(f'"{task.task_id}"（{task.description}）' for task in others[:5])
            warn = (
                f"\n注意：还有 {len(others)} 个后台任务在跑：{listed}。"
                "重复的任务请先 kill_task 再起新的。"
            )
        return (
            f"后台任务已启动：{started.task_id}{note}\n"
            f"日志：{started.log_path}\n"
            f"完成时会自动通知你；中途要看输出用 task_output(task_ids=[\"{started.task_id}\"])。"
            f"不要轮询等待，先继续别的工作。{warn}"
        )

    def _check_escalation(
        self, sandbox_permissions: str | None, justification: str | None
    ) -> str | None:
        """升权参数校验：非法/非扩大的请求直接失败，不打扰任何人。

        返回错误文本；None = 合法（含"没申请升权"）。成对约束：两个参数
        必须同现——没有理由的升权用户没法裁决，没有升权的理由是废话。
        """
        if sandbox_permissions is None and not (justification or "").strip():
            return None
        modes_list = self._escalation_modes()
        if not modes_list:
            return (
                "ERROR: 当前没有沙箱在生效，不存在可申请的升权档位。"
                "去掉 sandbox_permissions/justification 直接执行即可。"
            )
        if sandbox_permissions is None:
            return "ERROR: justification 必须与 sandbox_permissions 成对出现。"
        if not (justification or "").strip():
            return (
                "ERROR: sandbox_permissions 必须与 justification 成对出现——"
                "给用户一句解释：为什么这条命令需要升权。"
            )
        if sandbox_permissions not in modes_list:
            return (
                f"ERROR: 未知的升权档位 {sandbox_permissions!r}。"
                f"当前可申请：{', '.join(modes_list)}（只能申请尚未放行的能力）。"
            )
        return None

    def _bash(
        self,
        command: str,
        timeout: int | None = None,
        run_in_background: bool = False,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
    ) -> str:
        if error := self._check_escalation(sandbox_permissions, justification):
            return error
        escalation = sandbox_permissions
        if run_in_background:
            command = command.rstrip()
            if command.endswith("&"):
                return (
                    "ERROR: run_in_background=true 时不要在命令末尾写 &——"
                    "去掉它重试，后台化由我负责。"
                )
            #  后台任务默认不受超时约束：显式给了 timeout 才生效
            return self._spawn_background(
                command, kind="command", description=command,
                timeout=float(timeout) if timeout else None,
                escalation=escalation,
            )
        if reason := hardline_violation(command):
            return (
                f"ERROR: 命令被硬性拦截（{reason}）。"
                "这类不可撤销的破坏性操作在任何模式下都不执行，包括 --yolo。"
                "如果确有需要，请用户自己在终端里手动执行。"
            )
        limit = timeout or self.config.bash_timeout
        #  macOS 上套 Seatbelt 沙箱：写权限收到工作区+临时目录+构建缓存，
        #  其余路径只读。策略对所有子孙进程生效，模型再怎么套壳也跑不出去。
        #  升权本次生效：danger-full-access 不套沙箱；allow-network 套但放网。
        sandboxed = sandbox.enabled(self.config.sandbox) and escalation != "danger-full-access"
        argv = self._command_argv(command, escalation)
        #  自建 Popen 而不用 subprocess.run：run 超时后只杀直接子进程，然后无限期
        #  等管道关闭——孙进程握着管道时"超时"根本不生效。这里超时整树杀。
        #  字节模式读管道，解码带 GBK 兜底（见 _decode_output）。
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                #  掐断 stdin：ssh 等命令默认读取并转发 stdin，继承 tty 会偷键入
                #  ——和输入 prompt、插话线程抢字节；被中断后残留的子进程更会
                #  让"提示符在、打字没反应"（iTerm2 实测形态）。
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.config.workspace),
                env=_hardened_env(self.config.extra_env),
                **_subprocess_hardening(),
            )
        except OSError as exc:
            return f"ERROR: 无法执行命令：{exc}"
        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=limit)
        except KeyboardInterrupt:
            #  Ctrl-C 的 SIGINT 只到前台进程组（我们自己）；子进程在独立会话里
            #  收不到，不杀就成遗孤继续跑、还握着管道。中断语义必须是
            #  "真的停掉这条命令"，杀完再上抛给前端打中断提示。
            _kill_tree(proc)
            raise
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            #  整树已杀、管道随之关闭，这里只回收已缓冲的输出；再收不到就放弃，
            #  绝不能为了一点残余输出重新陷入无限期等待。
            try:
                stdout_raw, stderr_raw = proc.communicate(timeout=10)
            except (subprocess.TimeoutExpired, OSError, ValueError):
                stdout_raw, stderr_raw = b"", b""
            elapsed = time.monotonic() - started
            #  超时不是 error：超时前的输出往往已经包含答案（比如测试
            #  跑完了只是进程没退出）。提示前置 + 部分输出照样给模型，exit 124 是
            #  timeout 命令的约定退出码。
            partial = _partial_chunks(stdout_raw, stderr_raw)
            output = (
                f"命令超时（{limit}s），已终止整个进程树（含后台子进程，"
                f"实际等待 {elapsed:.0f}s）。以下是超时前已产生的输出：\n"
                f"exit_status: 124 (timeout)\n{partial or '(无输出)'}"
            )
            return output + _interactive_auth_hint(partial)

        chunks = [f"exit_status: {proc.returncode}"]
        if stdout_text := _decode_output(stdout_raw).rstrip():
            chunks.append(f"stdout:\n{stdout_text}")
        if stderr_text := _decode_output(stderr_raw).rstrip():
            chunks.append(f"stderr:\n{stderr_text}")
        if len(chunks) == 1:
            chunks.append("(无输出)")
        output = "\n".join(chunks)
        if escalation:
            #  升权执行的结果打标：用户和模型都要看得出"这次是升权跑的"
            output = f"[沙箱：本次调用已按 {escalation} 升权执行，仅本次生效]\n" + output
        if sandboxed and proc.returncode != 0:
            #  两类正交分开报：runner 自身失败 = 命令根本没跑，
            #  是沙箱问题；疑似拒绝 = 命令跑了被策略挡。混为一谈会把模型
            #  引向错误的下一步（对沙箱故障升权、或对策略拦截重试原命令）。
            if failure_line := sandbox.runner_failure(output):
                output += (
                    "\n[沙箱提示] 沙箱运行器自身失败（{line}）——**命令没有执行**。"
                    "这是沙箱环境的问题，不是命令的问题：重试原命令或升权都没有意义，"
                    "请向用户说明沙箱异常（可用 XIAOYU_SANDBOX=0 暂时关闭后重试）。"
                ).format(line=failure_line)
            elif sandbox.looks_denied(
                output, network_disabled=not self.config.sandbox_network
            ):
                #  内核只回 EPERM，模型看不出"是我越界了"还是"命令本身坏了"，
                #  不提示它就会原地重试到撞上循环护栏。
                output += "\n" + sandbox.denial_hint(
                    self.config.workspace,
                    self.config.sandbox_network,
                    escalation=bool(self._escalation_modes()),
                )
        return output

    def _task_output(self, task_ids: Any, timeout: int | None = None) -> str:
        """后台任务快照 / 有界等待。宽进：单个 id 裸字符串也收（模型常这么写）。"""
        if isinstance(task_ids, str):
            task_ids = [task_ids]
        if not isinstance(task_ids, list) or not task_ids:
            return "ERROR: task_ids 必须是非空数组（单个任务写成一元数组）。"
        ids: list[str] = []
        for item in task_ids:
            text = str(item).strip()
            if text and text not in ids:
                ids.append(text)
        wait = min(max(int(timeout or 0), 0), 600)
        deadline = time.monotonic() + wait
        sections: list[str] = []
        for task_id in ids:
            task = self.tasks.get(task_id)
            if task is None:
                known = ", ".join(self.tasks.known_ids()) or "（无）"
                sections.append(f"{task_id}：not_found。已知任务：{known}")
                continue
            if wait:
                task.done.wait(max(0.0, deadline - time.monotonic()))
            status = task.status
            head = f"{task_id}：{status}"
            if task.done.is_set():
                head += f"（exit {task.exit_code}，用时 {task.elapsed():.1f}s）"
            else:
                head += f"（已运行 {task.elapsed():.0f}s）"
                if wait:
                    head += "——等待已到上限，不必再调；任务完成时会自动通知你"
            sections.append(f"{head}\n输出：\n{self.tasks.output_of(task)}")
        return "\n\n".join(sections)

    def _kill_task(self, task_id: str) -> str:
        return self.tasks.kill(str(task_id).strip())

    def _monitor(
        self, command: str, description: str, timeout: int | None = None
    ) -> str:
        limit = float(timeout or MONITOR_DEFAULT_TIMEOUT)
        return self._spawn_background(
            command, kind="monitor", description=description.strip() or command,
            timeout=limit,
        )

    # ---------- 辅助 ----------

    def _mark_read(self, target: Path) -> None:
        """记下这个文件"已被读过"以及当时的 mtime。"""
        try:
            self._reads[target] = target.stat().st_mtime
        except OSError:
            self._reads[target] = 0.0

    def _guard_known(self, target: Path, shown: str) -> str | None:
        """改动已有文件前的两道闸：必须读过，且读过之后没被别人改。"""
        if target not in self._reads:
            return (
                f"ERROR: 还没读过 {shown}，不能直接改。"
                "请先用 read_file 读一遍（用 bash cat 看过不算，我要确保你手上是完整现状）。"
            )
        try:
            current = target.stat().st_mtime
        except OSError:
            return None
        if current > self._reads[target]:
            return (
                f"ERROR: {shown} 在你上次 read_file 之后被改动过"
                "（可能是你自己用 bash 改的，或外部编辑器改的）。"
                "请重新 read_file 拿到最新内容再改，否则会覆盖掉别人的改动。"
            )
        return None

    @staticmethod
    def _match_lines(text: str, needle: str) -> list[int]:
        """needle 每次出现所在的行号。"""
        lines: list[int] = []
        start = 0
        while (index := text.find(needle, start)) != -1:
            lines.append(text.count("\n", 0, index) + 1)
            start = index + 1
        return lines

    @staticmethod
    def _near_miss_hint(text: str, old_str: str) -> str:
        """old_str 没匹配上时，猜一下它大概想指哪几行，帮模型自愈。"""
        probe = next((line.strip() for line in old_str.splitlines() if line.strip()), "")
        if not probe:
            return ""
        hits = [
            number
            for number, line in enumerate(text.splitlines(), start=1)
            if line.strip() == probe
        ]
        if not hits:
            return ""
        preview_line = probe if len(probe) <= 60 else probe[:60] + "…"
        return (
            f"\n提示：第 {', '.join(str(n) for n in hits[:5])} 行有内容去掉首尾空白后"
            f"与你 old_str 的首行一致（{preview_line!r}），"
            "很可能是缩进或行尾空白不一致。请重新 read_file 照抄原文。"
        )

    @staticmethod
    def _context_around(text: str, offset: int, radius: int = 3) -> str:
        """给出改动位置附近的带行号上下文，方便模型确认改对了。"""
        lines = text.splitlines()
        center = text.count("\n", 0, min(offset, len(text)))
        start = max(0, center - radius)
        end = min(len(lines), center + radius + 1)
        width = len(str(end))
        return "\n".join(
            f"{number:>{width}} | {lines[number - 1]}" for number in range(start + 1, end + 1)
        )

    def _resolve(self, path: str) -> tuple[Path, bool]:
        """相对工作区解析路径，并标记是否逃出工作区。"""
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = self.config.workspace / candidate
        resolved = candidate.resolve()
        outside = not resolved.is_relative_to(self.config.workspace)
        return resolved, outside

    def _spill(self, name: str, text: str) -> tuple[Path, str] | None:
        """把完整输出落盘并登记进召回表，返回 (文件路径, 短 id)；
        写失败返回 None（退回纯截断）。"""
        try:
            if self._spill_dir is None:
                #  resolve 后再存：outside_workspace 的豁免判断用的是 resolve 过的
                #  路径，macOS 的 /var → /private/var 符号链接会让未解析形态对不上
                self._spill_dir = Path(tempfile.mkdtemp(prefix="xiaoyu-spill-")).resolve()
            self._spill_seq += 1
            spill_id = str(self._spill_seq)
            safe_name = "".join(ch if ch.isalnum() else "-" for ch in name)[:40] or "tool"
            path = self._spill_dir / f"{self._spill_seq:03d}-{safe_name}.txt"
            path.write_text(text, encoding="utf-8")
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            self._spills[spill_id] = {
                "path": path,
                "name": name,
                "chars": len(text),
                "lines": text.count("\n") + 1,
                "summary": first_line[:120],
            }
            return path, spill_id
        except OSError:
            return None

    def _bound_output(self, name: str, text: str) -> str:
        """超长输出：完整落盘 + 内联留头尾预览和取回定位符（spill）。

        纯截断会把中段永久丢掉——测试输出的中部失败、长日志的关键一段，
        模型想再看只能重跑命令。落盘后中段随时可用 read_file/grep 按需取回，
        重跑（可能有副作用、可能很慢）不再是唯一出路。落盘失败退回纯截断。
        """
        limit = self.config.max_tool_output
        if len(text) <= limit:
            return text
        result = self._spill(name, text)
        if result is None:
            return self._truncate(text)
        spilled, spill_id = result
        omitted = len(text) - limit
        head = limit // 2
        tail = limit - head
        marker = f"\n\n… [中间省略 {omitted} 字符，完整内容见召回 id {spill_id}] …\n\n"
        total_lines = text.count("\n") + 1
        return (
            f"[输出超长：原始 {len(text)} 字符 / 约 {total_lines} 行，"
            f"完整内容已存，召回 id: {spill_id}。"
            f"需要中段时用 recall(id=\"{spill_id}\", pattern=…) 按正则找，或 "
            f"recall(id=\"{spill_id}\", offset=行号, limit=行数) 取一段——不要为此重跑原命令。"
            "以下保留开头和结尾]\n"
            f"{text[:head]}{marker}{text[-tail:]}"
        )

    def _recall(
        self,
        id: str | None = None,
        pattern: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> str:
        """按 id 召回超长输出的片段（addressable recall）。

        无 id = 列表；id+pattern = 该输出内正则找行；id+offset/limit = 取行段；
        id 单独 = 取一段中部（比 inline 预览多、又不至于再次撑爆）。
        """
        if not id:
            if not self._spills:
                return "没有可召回的输出（还没有工具输出超长落盘）。"
            rows = [
                f"  id {sid}: {meta['name']}（{meta['chars']} 字符 / {meta['lines']} 行）"
                + (f" — {meta['summary']}" if meta["summary"] else "")
                for sid, meta in self._spills.items()
            ]
            return "可召回的输出：\n" + "\n".join(rows)
        meta = self._spills.get(str(id).strip())
        if meta is None:
            avail = "、".join(self._spills) or "（无）"
            return f"ERROR: 没有召回 id {id}。可用 id：{avail}"
        try:
            text = Path(meta["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"ERROR: 召回 id {id} 的文件已不可读（{exc}）——可能会话临时目录已被清理，只能重跑原命令。"
        lines = text.splitlines()
        total = len(lines)
        #  recall 的结果也会经 run() 外层的 _bound_output——若超长会再次落盘、
        #  生成新 id 又把中段丢掉。所以这里主动把结果压到预算内（从开头留，尾部
        #  截并注明），召回自己绝不触发二次 spill
        budget = max(200, self.config.max_tool_output - 200)

        def fit(header: str, body: str, tail_hint: str) -> str:
            if len(body) <= budget:
                return f"{header}\n{body}"
            return f"{header}（内容过长，只显示前 {budget} 字符，{tail_hint}）\n{body[:budget]}"

        if pattern:
            try:
                rx = re.compile(pattern)
            except re.error as exc:
                return f"ERROR: 正则有误：{exc}"
            hits = [f"{n}: {ln}" for n, ln in enumerate(lines, 1) if rx.search(ln)]
            if not hits:
                return f"召回 id {id}（{total} 行）里没有匹配 {pattern!r} 的行。"
            return fit(
                f"召回 id {id} 匹配 {pattern!r}（{len(hits)} 处）：",
                "\n".join(hits),
                "缩小范围或用 offset/limit 取具体段",
            )
        if offset is not None or limit is not None:
            start = max(1, offset or 1)
            if start > total:
                return f"ERROR: 召回 id {id} 只有 {total} 行，offset={start} 超出范围"
            end = total if limit is None else min(total, start + max(1, limit) - 1)
            chunk = "\n".join(lines[start - 1 : end])
            return fit(f"[召回 id {id} 第 {start}-{end} 行，共 {total} 行]", chunk, "减小 limit")
        #  只给 id：取中部一段（inline 预览已给头尾，中部最可能是被丢掉的）
        if len(text) <= budget:
            return f"[召回 id {id} 完整内容，{total} 行]\n{text}"
        mid = len(text) // 2
        half = budget // 2
        lo, hi = max(0, mid - half), mid + half
        return (
            f"[召回 id {id} 中段（约第 {lo}–{hi} 字符，共 {meta['chars']}）；"
            f"要头尾看原预览，要定位段用 pattern= 或 offset=]\n{text[lo:hi]}"
        )

    def _truncate(self, text: str) -> str:
        """超长输出保头保尾、砍中段。

        只留开头会把最关键的部分砍掉——测试输出的失败汇总、构建的最终错误
        都在结尾。顶部先声明原始规模，模型才能决定要不要换姿势重取。
        """
        limit = self.config.max_tool_output
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        head = limit // 2
        tail = limit - head
        marker = f"\n\n… [中间已截断 {omitted} 字符] …\n\n"
        return (
            f"[警告：输出超长已截断——原始 {len(text)} 字符 / "
            f"约 {text.count(chr(10)) + 1} 行，以下保留开头和结尾]\n"
            f"{text[:head]}{marker}{text[-tail:]}"
        )
