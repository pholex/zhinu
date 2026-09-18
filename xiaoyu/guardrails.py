"""无护栏预设（--unguarded）与可单独关闭的护栏层：一张表定义"哪几层能关、关成什么、怎么单独关"。

小羽的护栏几乎不约束用户，约束的是模型的动作与外部内容——内置提示里没有任何
"拒绝某类请求"的内容策略。但内部场景（一次性容器、一任务一 VM 的无人值守活）
里，"用户在安全沙箱里跑、后端模型自带内容约束"这个前提成立时，端侧护栏只剩
摩擦：镜像烧录被硬红线拦、无人值守卡在必问点、每个 MCP 发版都得重批。
预设把这些一次放开，而不是让人在启动行上堆六个旗标。

三条纪律：
1. **只关"关掉能换来灵活性"的层**。终端控制字符剥离、特殊文件闸、宿主侧 git
   加固、出网口私有键净化，关掉换不来任何能力、只会让自己更脆——不在表里，
   也不该有开关。`<untrusted_content>` 标记同理：它是来源标注不是限制，模型
   照样看到全文；想让内部 server 的结果当指令用，逐 server 加 `trustContent`。
   deny 规则是用户自己写的明确意志，预设不覆盖（想放行就删规则）。
2. **"安全沙箱"是契约不是口头承诺**：旗标只在真实环境变量 `XIAOYU_UNGUARDED=1`
   存在时生效——它该由容器 / VM 的编排脚本注入，而不是在自己笔记本上顺手敲出来。
   只认 os.environ，不读任何 .env：工作区 .env 能被仓库带进来，用户级 .env 会被
   "上次设过"遗忘——两种都不是"这次运行确实在沙箱里"的证据。
3. **每层护栏查表决定开关，不散落 `if args.unguarded`**：新加一层护栏时它默认
   不在表里 = 预设下仍然开着，不会因为忘了接开关而漏关；也不会因为写错而在正常
   模式下漏开。表里每个字段都是 Config 字段，测试锁死两边一致。

预设的名字刻意叫 unguarded（无护栏）而不是 advance / pro：读启动命令的人一眼
要看出它做了什么——这类开关恰恰不该藏在一个中性的词后面。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

FLAG = "--unguarded"
#  预设的同意开关：只认真实环境变量（见模块 docstring 第 2 条）
CONSENT_ENV = "XIAOYU_UNGUARDED"
#  会话前言里的事件名（agent._log_guardrails 写，审计脚本按它找）
EVENT = "guardrails"
_ON_VALUES = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Layer:
    """一层可关闭的护栏。"""

    field: str  # Config 字段名
    arg: str  # argparse dest（主命令与 resume 解析器共用）
    off_value: Any  # 无护栏预设下的取值
    label: str  # 给用户看的名字
    switch: str  # 单独关掉它的写法（横幅与文档共用）


#  每一层都是 Config 字段，且关值≠出厂值（测试锁死）。工作区信任门不在表里：
#  Config.workspace_trusted 正常过门后也是 True，按值看不出"是不是预设放的行"，
#  所以它作为预设的附加动作单独处理（TRUST_GATE，CLI 在过门处按 args.unguarded 放行）
LAYERS: tuple[Layer, ...] = (
    Layer("auto_approve", "yolo", True, "逐条审批", "--yolo"),
    Layer("sandbox", "sandbox", False, "bash 沙箱", "--no-sandbox"),
    Layer("hardline", "hardline", False, "bash 硬红线", "XIAOYU_HARDLINE=0"),
    Layer(
        "unattended", "unattended", True,
        "--yolo 下仍必问的两项（退出 plan、沙箱升权）", "--unattended",
    ),
    Layer(
        "mcp_trust_changes", "mcp_trust_changes", True,
        "MCP 工具变更隔离", "XIAOYU_MCP_TRUST_CHANGES=1",
    ),
)
TRUST_GATE = "工作区信任门（本次放行，不记入信任表）"

#  预设下**仍然生效**的层：文档与横幅共用一份，别在两处各写一遍然后漂移
KEPT: tuple[str, ...] = (
    "deny 权限规则（用户自己写的，想放行就删规则）",
    "<untrusted_content> 来源标注（逐 server 用 trustContent 解除）",
    "终端控制字符剥离、特殊文件闸、宿主侧 git 加固、出网口私有键净化",
)


def consented() -> bool:
    """编排环境是否声明了"这次运行在安全沙箱里"。"""
    return os.environ.get(CONSENT_ENV, "").strip().lower() in _ON_VALUES


def overrides() -> dict[str, Any]:
    """预设翻成 Config 字段 → 取值。"""
    return {layer.field: layer.off_value for layer in LAYERS}


def apply_args(args: Any) -> None:
    """把预设落到 argparse 结果上：每个经 args 走的层都设成关。

    CLI 各处 Config.from_env 站点照旧从 args 取值，不必知道预设的存在；
    工作区信任门不经 args，由 CLI 在过门处按 args.unguarded 放行。
    """
    for layer in LAYERS:
        setattr(args, layer.arg, layer.off_value)


def missing_consent() -> str:
    """给了旗标却没有环境同意时的报错（面向用户）。"""
    return (
        f"{FLAG} 只在环境变量 {CONSENT_ENV}=1 存在时生效——它声明的是"
        "「这次运行在安全沙箱里」，应由容器 / VM 的编排脚本注入，不读 .env。"
        "确认在隔离环境里就 export 它再跑；不是的话请改用单项开关"
        "（--yolo / --no-sandbox / --unattended / XIAOYU_HARDLINE=0 …）。"
    )


def relaxed(config: Any) -> list[Layer]:
    """当前配置下已经关掉的层（横幅与会话日志共用）。"""
    return [layer for layer in LAYERS if getattr(config, layer.field, None) == layer.off_value]


def snapshot(config: Any) -> dict[str, Any]:
    """记进会话前言的形状：事后审计能看出这一跑关了什么。"""
    unguarded = bool(getattr(config, "unguarded", False))
    return {
        "unguarded": unguarded,
        "off": [layer.field for layer in relaxed(config)],
        #  信任门只有预设会放（--trust 走的是持久信任表，不算"放开护栏"）
        "trust_gate": unguarded,
    }


def notice(config: Any) -> str:
    """开场那行警告：关了哪些、还留着哪些。"""
    labels = [layer.label for layer in relaxed(config)]
    if getattr(config, "unguarded", False):
        labels.append(TRUST_GATE)
    off = "、".join(labels) or "（无）"
    return f"{FLAG} 已开启：端侧护栏已放开——{off}。仍生效：{'；'.join(KEPT)}。"
