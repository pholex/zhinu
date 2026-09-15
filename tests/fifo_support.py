"""特殊文件（FIFO）防挂死用例的公共件。

修复失效时读 / 写 FIFO 会永久阻塞：一律在子线程里跑、限时 join，
不能让一条回归拖垮整个套件。Windows 没有 mkfifo，用例整体跳过。
"""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from typing import Any, Callable

needs_fifo = unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO 仅 POSIX")


def call_bounded(
    test: unittest.TestCase, fn: Callable[[], Any], fifo: Path, timeout: float = 3.0
) -> Any:
    """子线程里跑 fn 并限时 join，返回 fn 的结果（异常原样抛回）。

    挂住时以非阻塞方式各开一次读端、写端再关掉：阻塞在读的拿到 EOF，
    阻塞在写（open O_WRONLY 等读者）的被放行，然后判失败。
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - 原样转交给主线程
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        for flags in (os.O_WRONLY | os.O_NONBLOCK, os.O_RDONLY | os.O_NONBLOCK):
            try:
                os.close(os.open(fifo, flags))
            except OSError:
                pass
        worker.join(timeout)
        test.fail(f"碰 FIFO 挂住了：{fifo}")
    if "error" in box:
        raise box["error"]
    return box.get("result")
