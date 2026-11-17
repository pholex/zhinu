"""AcpServer.serve 的 stdin 读循环：挂起信号不能被阻塞读拖住，分片到达照样按行交付。"""

from __future__ import annotations

import _thread
import io
import json
import os
import signal
import threading
import time
import unittest

from xiaoyu.acp import AcpServer


class _Fired(Exception):
    pass


@unittest.skipIf(os.name == "nt", "POSIX 的 select 读路径")
class StdinSignalWakeupTest(unittest.TestCase):
    def test_pending_signal_runs_while_waiting_on_stdin(self):
        #  interrupt_main 只挂起"信号到达"的标记、不发系统调用层面的信号——正是
        #  信号落在"离开字节码、未进 read"窗口时的样子：read 不会被 EINTR 打断
        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, encoding="utf-8")
        previous = signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(_Fired()))
        closed = threading.Event()

        def unblock():
            #  兜底：修复失效时阻塞读要等到这里才返回，测试不至于挂死
            if not closed.is_set():
                os.close(write_fd)
                closed.set()

        fire = threading.Timer(0.2, _thread.interrupt_main, args=(signal.SIGTERM,))
        rescue = threading.Timer(5.0, unblock)
        server = AcpServer(agent_factory=None, stdin=stdin, stdout=io.StringIO())
        started = time.monotonic()
        try:
            fire.start()
            rescue.start()
            with self.assertRaises(_Fired):
                server.serve()
            elapsed = time.monotonic() - started
        finally:
            fire.cancel()
            rescue.cancel()
            unblock()
            stdin.close()
            signal.signal(signal.SIGTERM, previous)
        self.assertLess(elapsed, 3.0, "挂起的信号处理器被阻塞读拖到 stdin 来数据才执行")

    def test_lines_split_across_reads_are_delivered_whole(self):
        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, encoding="utf-8")
        out = io.StringIO()
        first = json.dumps({"jsonrpc": "2.0", "id": "一", "method": "不存在的方法"}, ensure_ascii=False)
        second = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "nope"})
        payload = (first + "\r\n" + second).encode("utf-8")  # 最后一行不带换行
        #  切在多字节字符中间：「一」的 3 个字节拆到两次写入里
        cut = payload.index("一".encode("utf-8")) + 1

        def writer():
            for piece in (payload[:cut], payload[cut:]):
                os.write(write_fd, piece)
                time.sleep(0.05)
            os.close(write_fd)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            AcpServer(agent_factory=None, stdin=stdin, stdout=out).serve()
        finally:
            thread.join(timeout=5)
            stdin.close()
        ids = [json.loads(line)["id"] for line in out.getvalue().splitlines()]
        self.assertEqual(ids, ["一", 2])


if __name__ == "__main__":
    unittest.main()
