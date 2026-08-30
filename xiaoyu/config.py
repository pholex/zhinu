"""小羽 (Xiaoyu) 运行配置。

密钥永不打印、永不写日志：只从环境变量或 macOS Keychain 取值后直接交给 SDK。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

# 端点不设默认值：这是公开发布的包，不硬编码任何具体网关地址。
# 必须由 XIAOYU_BASE_URL 或 --base-url 提供（OpenAI 兼容 /v1 端点）。

# 默认主模型：国产便宜模型优先。
# 实测（12 个候选各跑 4 个 case）所有模型都能稳定完成当前 eval 集，
# 差别只在成本 —— 同一任务最贵的比最便宜的高 57 倍。
# 所以默认取「便宜且实测无工具调用错误」的 deepseek-v4-pro。
# ⚠️ 但要清楚：当前 eval 集没有区分度（12/12 全过），这个选择的依据是
# 成本 + 简单任务通过率，不代表它能胜任难任务。硬活手动升级：
#   xiaoyu --model <更强的模型>   或 REPL 里 /model <更强的模型>
DEFAULT_MODEL = "deepseek-v4-pro"

# 摘要/检索这类有界的辅助任务用更便宜的模型：不需要工具调用，失败可回退主模型。
DEFAULT_SUMMARY_MODEL = "deepseek-v4-flash"

#  已知模型的上下文输入窗口（token），按模型名前缀匹配。**这是零往返的快路径
#  兜底**——权威值是 Models API 的 `max_input_tokens`（`/model` 探测时抓取并
#  自校正当前会话，见 providers.probe_model_caps）。启动刻意不探测（"列得出不
#  等于调得通"），所以这张表仍是默认来源，探测过才被更准的值顶掉。
#  数字来源：网关侧声明的 max_input_tokens（deepseek/glm/kimi）与厂商公布的
#  规格（fable-5、qwen3.8-max 均 1M）。
#  没命中的用保守兜底：Claude 系一般 200k，留点余量取 180k——
#  宁可早压缩，不可撑爆窗口。qwen3.7 无可靠数字，先不入表。
CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("deepseek-v4", 1_000_000),
    ("glm-5", 1_000_000),  # 5.2 / 5.3 均 1M（5.3 为现役直连型号）
    ("kimi-k3", 1_048_576),
    ("qwen3.8-max", 1_000_000),
    #  海外直连（前缀按序匹配：haiku 的 200K 例外必须排在通配 claude- 之前）
    ("claude-haiku", 200_000),
    ("claude-", 1_000_000),  # opus-5 / sonnet-5 / fable-5 均 1M（官方文档 2026-08）
    ("gpt-5", 400_000),  # GPT-5 系官方数值
    ("gemini-3.7-flash", 1_048_576),  # Models API inputTokenLimit（2026-08-24 实测）
    #  ⚠️ grok-4.6 的真实窗口是 500k，这里**故意填 280k**——这一格是压缩预算
    #  而不是规格表（兜底的 180k 同理）。xAI 对 prompt 达到 200k 的请求整单翻倍
    #  计价（$2/$6 → $4/$12，含缓存档），所以按默认 compact_at=0.7 取
    #  280k × 0.7 = 196k，压缩阈值刚好卡在 200k 悬崖之下。真要用满 500k：
    #  XIAOYU_CONTEXT_LIMIT=500000（明码标价地多花一倍钱）。
    #  改 compact_at 时记得这一格是跟着 0.7 算的。
    ("grok-4.6", 280_000),
)
FALLBACK_CONTEXT_LIMIT = 180_000


def context_window(model: str) -> int:
    """模型名 → 上下文上限。全限定名（provider/model）先剥掉 provider 前缀。"""
    bare = model.rsplit("/", 1)[-1]
    for prefix, limit in CONTEXT_WINDOWS:
        if bare.startswith(prefix):
            return limit
    return FALLBACK_CONTEXT_LIMIT

# explore 子 agent 用的模型：只做只读检索，翻代码最费 token，交给最便宜的。
DEFAULT_EXPLORE_MODEL = "deepseek-v4-flash"

#  effort 取值并集：none/minimal 是 OpenAI 线专有，xhigh/max 是 Anthropic/GPT-5 线，
#  low/medium/high 三家通吃。这里只做拼写把关，不按模型裁剪——是否支持由上游裁决
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

KEYCHAIN_SERVICE = "XIAOYU_API_KEY"

#  网关 key 的键名序列：自己的名字优先，LiteLLM 生态惯用名兜底——
#  用户机器上往往已经为别的工具配过 LITELLM_API_KEY，直接复用即开箱可用。
#  Keychain 按"service 名 = 变量名"同名规则一并覆盖。
GATEWAY_KEY_ENVS = ("XIAOYU_API_KEY", "LITELLM_API_KEY")

#  项目根目录（editable 安装时 .env 可以放这里；pip 安装后此路径指向 site-packages，无意义）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def home_dir() -> Path | None:
    """`Path.home()` 的可失败版本：推不出用户主目录时返回 None 而不是抛。

    真实会撞到的场景：Windows 服务账户/被清空的环境（`Path.home()` 在 Windows
    只能从环境变量推导）、Linux 容器里的随机 UID（无 pwd 条目，OpenShift 类
    平台的常态）。库层嵌入（daemon 常驻）恰好都在这类环境里跑——所有"锦上
    添花"的 home 派生路径（技能库、缓存可写根、用户配置兜底）都应经这里，
    拿到 None 就跳过该项，绝不能让构造 Agent 直接炸。
    """
    try:
        return Path.home()
    except RuntimeError:
        return None


def user_config_dir() -> Path:
    """按平台约定的用户配置目录。

    Windows: %APPDATA%\\xiaoyu；其余平台: $XDG_CONFIG_HOME/xiaoyu（默认 ~/.config/xiaoyu）。
    pip/pipx 安装后包目录在 site-packages 里没法放 .env，`xiaoyu config` 写到这里。

    推不出用户目录时（见 `home_dir`）退到临时目录下的固定子目录：
    读不到用户级配置是正确的降级，构造 Agent 直接炸不是。
    """
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "xiaoyu"
        home = home_dir()
        if home is not None:
            return home / "AppData" / "Roaming" / "xiaoyu"
        return Path(tempfile.gettempdir()) / "xiaoyu"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "xiaoyu"
    home = home_dir()
    if home is not None:
        return home / ".config" / "xiaoyu"
    return Path(tempfile.gettempdir()) / "xiaoyu"


def user_env_path() -> Path:
    """用户级 .env 的固定路径（`xiaoyu config` 的写入目标）。"""
    return user_config_dir() / ".env"


def save_user_env(values: dict[str, str]) -> Path:
    """把配置合并写入用户级 .env：只覆盖传入的键，其余原样保留。"""
    path = user_env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    merged = _parse_dotenv(path) if path.is_file() else {}
    merged.update(values)
    body = "\n".join(f"{key}={value}" for key, value in merged.items())
    path.write_text(body + "\n", encoding="utf-8")
    #  里面可能有 key，POSIX 上收紧到仅本人可读（Windows 无此语义，忽略失败）。
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return path


def load_dotenv(
    explicit: Path | None = None, untrusted_dir: Path | None = None
) -> list[Path]:
    """加载 .env 到 os.environ，返回实际读到的文件列表。

    优先级：真实环境变量 > 当前目录 .env > 项目根 .env > 用户级 .env。
    已存在的环境变量不会被覆盖，方便临时 `XIAOYU_MODEL=x xiaoyu ...`。

    untrusted_dir 非 None = 该目录没过 folder trust 门（见 folder_trust.py）：
    落在其中的**自动发现**候选（当前目录 .env）跳过不读——工作区 .env 能改写
    端点与开关，是"clone 即生效"的口子。显式指定（--env-file / XIAOYU_ENV_FILE）
    不受影响：那是用户亲手指的文件，不是仓库塞过来的。
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    elif override := os.environ.get("XIAOYU_ENV_FILE"):
        candidates.append(Path(override).expanduser())
    else:
        for candidate in (Path.cwd() / ".env", PROJECT_ROOT / ".env"):
            if untrusted_dir is not None and _inside(candidate, untrusted_dir):
                continue
            candidates.append(candidate)
        candidates.append(user_env_path())

    loaded: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        for key, value in _parse_dotenv(resolved).items():
            os.environ.setdefault(key, value)
        loaded.append(resolved)
    return loaded


def _inside(path: Path, directory: Path) -> bool:
    """path 是否位于 directory 之内（解析失败按"在里面"算：fail-secure）。"""
    try:
        return path.resolve().is_relative_to(directory.resolve())
    except OSError:
        return True


def _parse_dotenv(path: Path) -> dict[str, str]:
    """极简 .env 解析：KEY=VALUE，支持 # 注释、export 前缀、引号包裹。"""
    result: dict[str, str] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            result[key] = value
    return result


@dataclass
class Config:
    """一次会话的全部可调参数。"""

    base_url: str
    model: str
    workspace: Path
    #  备用模型降级链：主模型重试耗尽（持续限流/持续瞬时错误）后依次自动切换。
    #  默认空 = 不降级，行为与从前一致。来源 XIAOYU_FALLBACK_MODELS（逗号分隔）。
    fallback_models: list[str] = field(default_factory=list)
    #  摘要等辅助任务用的便宜模型；留空表示跟主模型一致。
    summary_model: str = DEFAULT_SUMMARY_MODEL
    #  explore 子 agent 用的便宜模型
    explore_model: str = DEFAULT_EXPLORE_MODEL
    #  代读模型：当前模型看不了图时，把图交给它换成一段文字（见 agent.read_images）。
    #  **默认空 = 不代读**，行为与从前一致。不预置默认值是刻意的：代读是有损的，
    #  默认打开等于让用户以为主模型看了图，而它看的是一段转述——与
    #  Registry.sees_images 的 fail-closed 同一条诚实纪律；何况默认值要点名某一家
    #  厂商的型号（还得是用户配了 key 的那家），猜错就是又一次静默失败。
    #  来源 XIAOYU_VISION_FALLBACK，值是模型名（如 deepseek-v4-flash-vision-exp）。
    vision_fallback_model: str = ""
    #  是否给主 agent 挂 explore 工具（子 agent 自己一定关掉，避免套娃）
    enable_explore: bool = True
    #  是否扫描 SKILL.md 技能（explore 子 agent 与 eval 关掉，保持行为确定）
    enable_skills: bool = True
    #  是否挂 update_plan 计划工具（explore 子 agent 关掉：检索任务不需要计划）
    enable_plan: bool = True
    #  是否挂 web_search 联网搜索工具（借厂商 Responses 接口的内置搜索；
    #  对应厂商的 key 没配时即便开着也不会出现。explore 子 agent 与 eval 关掉：
    #  前者只查代码不上网，后者要行为确定）
    enable_web_search: bool = True
    #  web_search 用哪家的内置搜索（websearch.SEARCH_BACKENDS 的键）。
    #  默认 deepseek：单次约 2 分钱；xai（grok-4.6）搜索质量更强、引用更全，
    #  但单次约 35 倍价（token 贵 + 每次搜索按次收费）——2026-08 对比实测见 playbook。
    search_provider: str = "deepseek"
    #  是否挂 browser 浏览器工具（依赖可选 extra `[browser]` 的 playwright，
    #  没装时即便开着也不出现，见 tools.py 的 check_fn 门控）。单独给开关是因为
    #  可用性绑在「宿主装没装某个包」上：装了 playwright 又用不到浏览器的机器
    #  白付 schema token，e2e golden 的工具表也不许随环境漂移。
    enable_browser: bool = True
    #  是否加载 entry_points 插件工具（组 "xiaoyu.tools"）。
    #  explore 子 agent 与 eval 关掉：受限子集用不上，且要保持行为确定。
    enable_plugins: bool = True
    #  是否挂载 mcp.json 里声明的 MCP server 工具（后台懒加载，见 mcp.py）。
    #  explore 子 agent 与 eval 关掉，理由同插件。只 gate 配置发现：宿主给
    #  Toolbox 显式注入的 mcp_view 不受它约束（显式注入=明确要）。
    enable_mcp: bool = True
    #  是否加载用户级 hooks.toml 生命周期钩子（见 hooks.py；只认用户级不认
    #  工作区级——hook 是任意代码执行，仓库级配置等于 clone 即种命令）。
    enable_hooks: bool = True
    #  MCP 工具检索模式（search_tool/use_tool 二段式）：MCP 工具不进模型的
    #  schema，改由 search_tool 按需检索 + use_tool 调用——工具集会话内稳定
    #  （prompt cache 不再被中途连上的 server 作废），几百个工具也不吃上下文。
    #  关掉（XIAOYU_MCP_TOOL_SEARCH=0）回到"全量进 schema"的旧行为。
    mcp_tool_search: bool = True
    #  是否加载声明式 subagent（agents/*.toml，见 agents.py；工作区级安全——
    #  spec 能声明的最大权限=用户逐次批准的权限，见该模块 docstring）。
    enable_agents: bool = True
    #  子 agent 嵌套深度：当前深度（主会话=0，每委托一层 +1）与最大深度。
    #  默认 max=1 = 不套娃（与既有行为一致，"单写者"纪律的显式化）；
    #  宸枢多层编排要嵌套时 XIAOYU_SUBAGENT_MAX_DEPTH=2/3 显式放开，有界不失控。
    subagent_depth: int = 0
    subagent_max_depth: int = 1
    #  七襄（qixiang，批量并行委托）的并发上限。默认 4：单机直连场景的保守值
    #  （上限式并发，不做自适应爬坡——理由见 qixiang.py docstring）。
    qixiang_concurrency: int = 4
    #  七襄单项任务的墙钟超时（秒，从实际启动起算，排队不计）。0 = 不限时，
    #  项的运行时长由 spec 的 max_iterations 自然封顶。
    qixiang_task_timeout: int = 0
    #  是否挂载宸枢（chenshu，编排总控模式，见 chenshu.py）。
    enable_chenshu: bool = True
    #  宸枢同时在跑的成员上限（worker + reviewer 合计）。
    chenshu_max_workers: int = 4
    #  是否登记进本机会话表、并接收其它会话投来的消息（见 peers.py）。
    #  只在交互模式生效。**--yolo 下默认关闭**：无人值守 + 可被本机任意进程
    #  投喂指令，两者叠加才是真风险；要开就显式 XIAOYU_ENABLE_PEERS=1。
    enable_peers: bool = True
    #  explore 子 agent 单次检索最多几轮工具调用。
    #  大仓库/复杂问题嫌 12 轮不够可用 XIAOYU_EXPLORE_ITERATIONS 放宽（上限 100）。
    explore_iterations: int = 12
    #  单轮用户输入内最多允许模型连续调多少轮工具，防跑飞。
    max_iterations: int = 50
    #  单个工具输出的字符上限，超出截断（一次 grep 就能吐爆上下文）。
    max_tool_output: int = 30_000
    #  bash 工具默认超时（秒）。
    bash_timeout: int = 120
    #  上下文窗口上限（token）的显式覆写；None = 按 CONTEXT_WINDOWS 表
    #  跟随当前 model 自动取（读 context_limit 属性即得生效值）。
    context_limit_override: int | None = None
    #  上面这个 override 是不是探测（Models API）写进来的：True = 可被后续探测更新，
    #  False = 用户显式设的（XIAOYU_CONTEXT_LIMIT / --）绝不被探测覆盖
    context_limit_probed: bool = False
    #  追加到内置 system prompt 末尾的自定义内容；None = 不追加，行为不变。
    #  供宿主进程把 xiaoyu 当执行引擎嵌入时注入身份/人格，
    #  不是项目指令——项目级规范走 AGENTS.md 那条路。
    append_system_prompt: str | None = None
    #  推理深度（effort）。空 = 不传，上游按自家默认。取值见 EFFORT_LEVELS；
    #  三条协议各自翻译（chat: reasoning_effort / Responses: reasoning.effort /
    #  Anthropic: output_config.effort），不认的上游会 400——宁可报错也不静默丢。
    #  子 agent 可在 spec 里单独声明（只读探索给 low，总控给 high/xhigh）。
    effort: str = ""
    #  服务端压缩透传：走 Anthropic Messages 协议且型号支持时，把压缩交给服务端
    #  （compact_20260112：模型自己写摘要，返回 compaction 块，下轮回传即可）。
    #  本地摘要压缩降为兜底（阈值抬高 SERVER_COMPACTION_FALLBACK）。关掉
    #  XIAOYU_SERVER_COMPACTION=0 回到纯本地压缩。
    server_compaction: bool = True
    #  会话级 token 软预算（prompt+completion 累计，与 serve 的 budget.tokens 同一把尺）。
    #  None = 不限。模型**知情**：按 BUDGET_NOTICE_STEPS 收到倒计时提示，到线前一步
    #  优雅收尾（交代现场），而不是被硬闸中途砍断。serve 的硬闸仍在，是兜底
    budget_tokens: int | None = None
    #  轮数预算可申请延期：撞 max_iterations 时给模型一步机会申请追加，
    #  总追加量 ≤ max_iterations × 这个系数（0 = 不许延期，撞顶即收尾）
    turn_extension: float = 1.0
    #  估算用量占到上限的这个比例时触发压缩。
    compact_at: float = 0.7
    #  压缩时至少保留最近这么多条消息（实际切点会往后找到 user 消息边界）。
    keep_recent: int = 8
    #  连续 read_file 达到这个次数时，在结果后追加提示，引导改用 explore
    read_streak_warn: int = 3
    #  连续 read_file 达到这个次数时直接拦截。用任何其它工具即重置计数。
    #  为什么要硬拦：实测靠 prompt 引导的采用率只有 1/3，而 explore 用起来能省
    #  一半主模型上下文 —— 光靠措辞劝不动，得在 harness 层面卡。
    read_streak_block: int = 5
    #  单次模型请求超时（秒）。eval 横向扫模型时会调小，避免某个模型卡死拖垮整轮。
    request_timeout: float = 600.0
    #  注入给 bash 工具的额外环境变量。
    #  eval 用它设 PIP_REQUIRE_VIRTUALENV：无人值守 + 全放行时，
    #  模型可能 pip install 到系统 Python 里去（已经真实发生过一次）。
    extra_env: dict[str, str] = field(default_factory=dict)
    #  True = 不再逐个确认写文件/执行命令（--yolo）。与 mode 正交：
    #  它是"什么都不问"，mode 的 auto 档是"沙箱兜得住的才不问"。
    auto_approve: bool = False
    #  起始交互模式（"default" / "auto" / "plan"，见 modes.py）。出厂是 auto
    #  （= modes.INITIAL，测试锁死两边一致；这里不 import modes 是为了不把配置层
    #  和模式层缠成环）。会话里可以 Shift+Tab 或 /mode 改，改的是 Agent.mode，
    #  不回写这里。
    mode: str = "auto"
    #  bash 命令是否套 macOS Seatbelt 沙箱（只在 macOS 生效，其它平台自动跳过）。
    #  拦的是"写工作区之外"这类不可逆破坏，读与网络默认放行——详见 sandbox.py。
    sandbox: bool = True
    #  沙箱内是否允许联网。默认允许：断网会静默打断 pip/npm/git push 这些日常动作。
    sandbox_network: bool = True
    #  工作区是否通过了 folder trust 门（见 folder_trust.py）。False 时工作区级
    #  可执行配置（.mcp.json / .xiaoyu/permissions.txt）不生效。门是 CLI 启动期
    #  的关卡，这里只是判决的载体；库层嵌入默认 True（宿主要门自己过 evaluate）。
    workspace_trusted: bool = True

    @property
    def context_limit(self) -> int:
        """生效的上下文上限：显式覆写 > 按当前模型查表。

        做成属性而非启动时算死：/model 切换、粘性降级都会改 self.model，
        1M 窗口的模型不该继续背 180k 的上限（反之亦然）。
        """
        if self.context_limit_override is not None:
            return self.context_limit_override
        return context_window(self.model)

    @context_limit.setter
    def context_limit(self, value: int) -> None:
        self.context_limit_override = value

    def apply_probed_limit(self, limit: int | None) -> bool:
        """把 Models API 探到的 max_input_tokens 写进 override——但绝不覆盖用户
        显式设的值。返回是否实际改动（供打印"已自校正"）。

        判据：当前 override 为空（还在用硬编码表），或它本来就是探测写进去的。
        用户设过（context_limit_probed=False 且 override 非空）则纹丝不动。
        """
        if not limit or limit <= 0:
            return False
        if self.context_limit_override is not None and not self.context_limit_probed:
            return False
        if self.context_limit_override == limit:
            self.context_limit_probed = True
            return False
        self.context_limit_override = limit
        self.context_limit_probed = True
        return True

    @classmethod
    def from_env(cls, workspace: Path | None = None, **overrides) -> "Config":
        cfg = cls(
            base_url=os.environ.get("XIAOYU_BASE_URL", ""),
            model=os.environ.get("XIAOYU_MODEL", DEFAULT_MODEL),
            summary_model=os.environ.get("XIAOYU_SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL),
            explore_model=os.environ.get("XIAOYU_EXPLORE_MODEL", DEFAULT_EXPLORE_MODEL),
            vision_fallback_model=os.environ.get("XIAOYU_VISION_FALLBACK", "").strip(),
            workspace=(workspace or Path.cwd()).resolve(),
        )
        if raw := os.environ.get("XIAOYU_FALLBACK_MODELS"):
            cfg.fallback_models = [name.strip() for name in raw.split(",") if name.strip()]
        if limit := os.environ.get("XIAOYU_CONTEXT_LIMIT"):
            with contextlib.suppress(ValueError):
                cfg.context_limit = int(limit)
        if rounds := os.environ.get("XIAOYU_EXPLORE_ITERATIONS"):
            with contextlib.suppress(ValueError):
                cfg.explore_iterations = max(1, min(int(rounds), 100))
        if keep := os.environ.get("XIAOYU_KEEP_RECENT"):
            with contextlib.suppress(ValueError):
                cfg.keep_recent = int(keep)
        if level := os.environ.get("XIAOYU_EFFORT", "").strip().lower():
            cfg.effort = level
        if raw := os.environ.get("XIAOYU_BUDGET_TOKENS"):
            with contextlib.suppress(ValueError):
                cfg.budget_tokens = int(raw) or None
        if raw := os.environ.get("XIAOYU_TURN_EXTENSION"):
            with contextlib.suppress(ValueError):
                cfg.turn_extension = max(0.0, float(raw))
        if (flag := os.environ.get("XIAOYU_SERVER_COMPACTION")) is not None:
            cfg.server_compaction = flag.strip().lower() not in ("0", "false", "no", "off")
        if ratio := os.environ.get("XIAOYU_COMPACT_AT"):
            with contextlib.suppress(ValueError):
                cfg.compact_at = float(ratio)
        if (flag := os.environ.get("XIAOYU_ENABLE_EXPLORE")) is not None:
            cfg.enable_explore = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_ENABLE_SKILLS")) is not None:
            cfg.enable_skills = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_ENABLE_WEB_SEARCH")) is not None:
            cfg.enable_web_search = flag.strip().lower() not in ("0", "false", "no", "off")
        if backend := os.environ.get("XIAOYU_SEARCH_PROVIDER", "").strip():
            cfg.search_provider = backend
        #  个人默认交互模式（default/auto/plan）。命令行 --mode 经 overrides
        #  在后面覆盖它；写错的值由 Agent 侧 modes.get 归一回确认档（配置
        #  写错不炸主循环的既有纪律），这里不重复校验
        if mode := os.environ.get("XIAOYU_MODE", "").strip():
            cfg.mode = mode
        if (flag := os.environ.get("XIAOYU_ENABLE_BROWSER")) is not None:
            cfg.enable_browser = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_ENABLE_PLUGINS")) is not None:
            cfg.enable_plugins = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_ENABLE_MCP")) is not None:
            cfg.enable_mcp = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_ENABLE_HOOKS")) is not None:
            cfg.enable_hooks = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_MCP_TOOL_SEARCH")) is not None:
            cfg.mcp_tool_search = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_ENABLE_AGENTS")) is not None:
            cfg.enable_agents = flag.strip().lower() not in ("0", "false", "no", "off")
        if raw := os.environ.get("XIAOYU_SUBAGENT_MAX_DEPTH"):
            with contextlib.suppress(ValueError):
                cfg.subagent_max_depth = max(1, int(raw))
        if raw := os.environ.get("XIAOYU_QIXIANG_CONCURRENCY"):
            with contextlib.suppress(ValueError):
                cfg.qixiang_concurrency = max(1, min(int(raw), 16))
        if raw := os.environ.get("XIAOYU_QIXIANG_TIMEOUT"):
            with contextlib.suppress(ValueError):
                cfg.qixiang_task_timeout = max(0, int(raw))
        if (flag := os.environ.get("XIAOYU_ENABLE_CHENSHU")) is not None:
            cfg.enable_chenshu = flag.strip().lower() not in ("0", "false", "no", "off")
        if raw := os.environ.get("XIAOYU_CHENSHU_MAX_WORKERS"):
            with contextlib.suppress(ValueError):
                cfg.chenshu_max_workers = max(1, min(int(raw), 16))
        peers_flag = os.environ.get("XIAOYU_ENABLE_PEERS")
        if peers_flag is not None:
            cfg.enable_peers = peers_flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_SANDBOX")) is not None:
            cfg.sandbox = flag.strip().lower() not in ("0", "false", "no", "off")
        if (flag := os.environ.get("XIAOYU_SANDBOX_NETWORK")) is not None:
            cfg.sandbox_network = flag.strip().lower() not in ("0", "false", "no", "off")
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        #  yolo 默认不收信（理由见 enable_peers 字段注释）。必须放在 overrides
        #  之后：auto_approve 是 --yolo 经 overrides 进来的，前面读还是 False。
        #  显式表过态（env 或 overrides 里点名 enable_peers）就尊重用户。
        if cfg.auto_approve and peers_flag is None and "enable_peers" not in overrides:
            cfg.enable_peers = False
        #  这里不再校验 base_url：多 provider 之后「有没有可用端点」不等于
        #  「网关配没配」——只填一个直连厂商的 key 也能跑。校验挪到
        #  providers.build()，它是唯一知道全部 provider 的地方。
        return cfg


class MissingConfig(RuntimeError):
    """缺少必需配置。"""


class MissingApiKey(MissingConfig):
    """没找到可用的 API key。"""


def find_api_key(env_names: Sequence[str]) -> str | None:
    """按给定名字序列找 key：先环境变量（.env 已合并进来），再拿同名当
    Keychain service 查；找不到返回 None（不抛错）。

    键名只有一套——`.env`、环境变量、Keychain 三处同名（如 DEEPSEEK_API_KEY），
    用户不用记"存 Keychain 时要换个名字"。网关的 XIAOYU_API_KEY 恰好也是
    历史上的 Keychain service 名，老用户存的 key 天然继续有效。

    多 provider 之后"找不到"是常态（没配直连就不该报错，跳过即可），
    所以取值与报错分成两个函数：这里只找，load_api_key 才抛。
    """
    for name in env_names:
        if value := os.environ.get(name, "").strip():
            return value
    for name in env_names:
        if value := _read_from_keychain(name):
            return value
    return None


def load_api_key() -> str:
    """网关的 key：按 环境变量 → Keychain 顺序取；取不到就抛错。绝不回显。"""
    if key := find_api_key(GATEWAY_KEY_ENVS):
        return key

    hints = [
        "未找到 API key。提供方式任选：",
        "  1. 运行 `xiaoyu config` 走配置向导（推荐，全平台）",
        f"  2. 在 {user_env_path()} 或当前目录 .env 里填 XIAOYU_API_KEY=<你的-key>",
    ]
    hints += [f"  {i}. {detail}" for i, (_, detail) in enumerate(key_fallback_sources(), start=3)]
    raise MissingApiKey("\n".join(hints))


def key_fallback_sources() -> list[tuple[str, str]]:
    """`.env` 之外 key 的备选提供方式：(短名, 详细写法)，按平台裁剪。

    向导提示用短名、MissingApiKey 报错用详细写法——平台差异和变量名只在这里维护，
    避免两处文案各改各的（v0.5.3 就是向导漏掉了平台判断）。
    """
    sources = [
        (
            "环境变量 XIAOYU_API_KEY",
            "设置环境变量 XIAOYU_API_KEY=<你的-key>",
        ),
    ]
    if sys.platform == "darwin":
        sources.append(
            (
                "macOS Keychain",
                f'macOS Keychain（不落盘）：security add-generic-password '
                f'-a "{os.environ.get("USER", "")}" -s "{KEYCHAIN_SERVICE}" -U -w',
            )
        )
    return sources


def _read_from_keychain(service: str = KEYCHAIN_SERVICE) -> str | None:
    """从 macOS login keychain 读取 key，失败一律返回 None（不打印任何值）。"""
    account = os.environ.get("USER", "")
    if not account:
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-a",
                account,
                "-s",
                service,
                "-w",
            ],
            capture_output=True,
            text=True,
            #  security 输出理论上是 ASCII，但 text=True 按 locale 严格解码，
            #  异常字节会把 UnicodeDecodeError 一路抛到启动路径（tests_ai
            #  编码审计抓出来的）——replace 兜底，坏值顶多是取不到 key
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None
