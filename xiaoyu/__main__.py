"""`python -m xiaoyu` 与 console script 共用的入口。

`term log` / `term info` 在导入 cli 之前就拦下来走快路径：cli 一导入就把
agent / tools 整套带进来（几百毫秒），而这两个子命令是给 shell 钩子和提示符
用的，每条命令、每个提示符都可能调一次，等不起。其余一切照旧交给 cli.main。
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) >= 2 and argv[0] == "term" and argv[1] in ("log", "info"):
        from .term import fast_command

        return fast_command(argv[1:])
    from .cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
