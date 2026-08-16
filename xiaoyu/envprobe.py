"""启动时的环境画像：常用工具链 + 网络区域，探测一次、静态注入 system prompt。

动机来自真实用户会话：Windows 机器上没有 git / node，模型选了依赖 Git 的
开发路线，二十多轮 yak-shaving 之后用户放弃整条路线；第一版网页用了 unpkg
CDN，在中国大陆网络下直接黑屏。这两类信息启动时花几毫秒就能拿到——
提前告诉模型，选型阶段就避开，而不是走到一半才撞上。

约束：内容在会话内必须静态（system prompt 是 prompt cache 的前缀资产），
所以只在构建 system prompt 时探测一次；探测本身只做 which / 读环境变量，
不跑子进程、不碰网络。
"""

from __future__ import annotations

import locale
import os
import shutil
import time

#  只探测"缺了会改变技术选型"的工具；探测用 which（毫秒级），不查版本。
_COMMON_TOOLS = ("git", "node", "npm", "curl", "rg")
_POSIX_TOOLS = ("python3", "pip3")
_WINDOWS_TOOLS = ("python", "pip", "pwsh")


def probe_tools() -> tuple[list[str], list[str]]:
    """返回 (可用工具, 未找到的工具)。"""
    names = list(_COMMON_TOOLS) + list(
        _WINDOWS_TOOLS if os.name == "nt" else _POSIX_TOOLS
    )
    present: list[str] = []
    missing: list[str] = []
    for name in names:
        (present if shutil.which(name) else missing).append(name)
    return present, missing


def china_network_likely() -> bool:
    """本机是否很可能处于中国大陆网络（启发式，只影响一条提示的有无）。

    依据（任一命中即判定）：locale / LANG 是 zh_CN；TZ 指向大陆时区；
    系统时区为 UTC+8 且无夏令时。UTC+8 会把新加坡/香港等也算进来——
    提示措辞是"可能不可达"，多看一句提示无害，漏掉才是真实事故。
    """
    lang = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    if "zh_cn" in lang:
        return True
    if os.environ.get("TZ", "") in (
        "Asia/Shanghai",
        "Asia/Chongqing",
        "Asia/Urumqi",
        "Asia/Harbin",
        "PRC",
    ):
        return True
    try:
        name = locale.getlocale()[0] or ""
    except ValueError:
        name = ""
    #  Windows 的 locale 名形如 "Chinese (Simplified)_China"
    if "zh_CN" in name or "China" in name:
        return True
    #  time.timezone 是本地时间落后 UTC 的秒数：UTC+8 即 -28800
    return time.timezone == -28800 and not time.daylight


def block() -> str:
    """拼进 system prompt 的环境画像段。"""
    present, missing = probe_tools()
    lines = ["", "环境画像（启动时探测，选型前先看一眼）："]
    lines.append(f"- 已可用：{'、'.join(present) or '（探测到的常用工具为空）'}")
    if missing:
        lines.append(
            f"- 未检测到：{'、'.join(missing)}。"
            "需要这些工具的方案，要么先装好（并告知用户），要么换不依赖它们的路线——"
            "不要走到一半才发现缺。"
        )
    if china_network_likely():
        lines.append(
            "- 网络环境很可能在中国大陆：unpkg / jsdelivr / fonts.googleapis / "
            "raw.githubusercontent 等境外源可能不可达。生成网页/应用时，"
            "静态资源（JS 库、字体）优先内联进产物或用国内可达源；"
            "安装依赖失败时优先考虑镜像源，不要对着原源反复重试。"
        )
    return "\n".join(lines) + "\n"
