"""整读前的文件类型闸：只放行普通文件。

exists / is_dir 挡不住特殊文件：读 FIFO 会一直阻塞到有人写入（调用方永久挂死、
连超时都没有），读 /dev/zero 这类设备读不到头。stat 跟随符号链接，所以仓库里
提交一个指向 /dev/zero 的链接同样拦得下——git 能存符号链接，clone 下来的工作区
里任何"启动就读"的文件都可能是它。

放在不依赖任何内核模块的最底层：tools / rewind / media / 配置加载都要用，
谁都能导入而不绕出循环依赖。Windows 上没有 FIFO / 设备节点这类文件，
S_ISREG 对普通文件照常成立，行为不变。
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

_SPECIAL_FILE_KINDS = (
    (stat.S_ISFIFO, "FIFO（命名管道）"),
    (stat.S_ISCHR, "字符设备"),
    (stat.S_ISBLK, "块设备"),
    (stat.S_ISSOCK, "socket"),
)


class NotRegularFile(OSError):
    """路径存在但不是普通文件。继承 OSError：沿用"读失败"分支的调用方零改动即兜住。"""

    def __init__(self, path: Path, kind: str) -> None:
        super().__init__(errno.EINVAL, f"是{kind}，不是普通文件，已拒绝读取", str(path))
        self.kind = kind


def non_regular_kind(mode: int) -> str | None:
    """st_mode → 非普通文件的类别名（给人看）；普通文件返回 None。"""
    if stat.S_ISREG(mode):
        return None
    return next((name for test, name in _SPECIAL_FILE_KINDS if test(mode)), "非普通文件")


def require_regular(path: Path) -> os.stat_result:
    """stat（跟随链接）并确认是普通文件，返回 stat 结果供调用方顺手查体积。

    不存在等 stat 失败原样抛 OSError；非普通文件抛 NotRegularFile。
    """
    info = os.stat(path)
    kind = non_regular_kind(info.st_mode)
    if kind is not None:
        raise NotRegularFile(path, kind)
    return info
