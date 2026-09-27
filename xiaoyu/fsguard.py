"""文件读写的两道底层闸：整读前只放行普通文件；落盘一律原子写。

exists / is_dir 挡不住特殊文件：读 FIFO 会一直阻塞到有人写入（调用方永久挂死、
连超时都没有），读 /dev/zero 这类设备读不到头。stat 跟随符号链接，所以仓库里
提交一个指向 /dev/zero 的链接同样拦得下——git 能存符号链接，clone 下来的工作区
里任何"启动就读"的文件都可能是它。

放在不依赖任何内核模块的最底层：tools / rewind / media / 配置加载都要用，
谁都能导入而不绕出循环依赖。Windows 上没有 FIFO / 设备节点这类文件，
S_ISREG 对普通文件照常成立，行为不变。

`write_atomic` 是状态文件落盘的唯一写法（tests/test_atomic_writes.py 的哨兵
禁止在别处手写"临时文件 + 改名"）。各处自己写的时候，每一份都漏过点什么：
固定的 `.tmp` 名让两个会话互相踩、写完才 chmod 让密钥文件有一瞬按 umask
可读、失败时把临时文件留在原地。
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import uuid
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


def write_atomic(
    path: Path, data: str | bytes, *, private: bool = False, encoding: str = "utf-8"
) -> None:
    """把 data 原子地写到 path：同目录临时文件写完再改名，读者看不到半个文件。

    - 临时名唯一（pid + 随机串）：并发的写者——两个会话、同进程的两个线程——
      各写各的临时文件，后改名的胜出，谁也读不到对方的半截；
    - private=True 时临时文件**创建时**就是 0600：里面有密钥的文件不该有任何
      一刻按 umask 可读（写完再 chmod 留着这个窗口）。Windows 上 mode 基本
      无语义，照常写；
    - private=False 时沿用目标已有的权限位（没有目标就交给 umask）：改名换的是
      inode，不沿用的话用户手工收紧过的权限会被悄悄放宽；
    - 任何一步失败都清掉临时文件再原样抛 OSError，目标保持原样。

    父目录不存在会先建。不 fsync：防的是进程中途死掉，不是断电。
    """
    payload = data.encode(encoding) if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    mode = 0o600 if private else 0o666
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(tmp, flags, mode)
        try:
            handle = os.fdopen(fd, "wb")
        except BaseException:
            #  fdopen 没接管成功，fd 还得自己关（接管之后绝不能再关第二次：
            #  那个号码可能已经被别的线程拿去用了）
            os.close(fd)
            raise
        with handle:
            handle.write(payload)
        if not private:
            with contextlib.suppress(OSError):
                os.chmod(tmp, stat.S_IMODE(os.stat(path).st_mode))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
