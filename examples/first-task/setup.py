#!/usr/bin/env python3
"""把新手示例项目拷到仓库外的一个目录，给小羽当第一个工作区。

    python examples/first-task/setup.py ~/xiaoyu-first-task

目标目录必须不存在（不往已有目录里混东西）。拷完打印下一步怎么跑。
只用标准库。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "project"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    target = Path(argv[1]).expanduser().resolve()
    if target.exists():
        print(f"目标目录已存在：{target}（换一个不存在的路径，示例不往已有目录里混东西）", file=sys.stderr)
        return 1
    if HERE in target.parents or target == HERE:
        print("别拷进示例自己的目录里", file=sys.stderr)
        return 1
    shutil.copytree(TEMPLATE, target)
    prompt = (target / "prompt.txt").read_text(encoding="utf-8").strip()
    print(f"已拷到 {target}\n")
    print("下一步（在那个目录里）：")
    print(f"  cd {target}")
    print("  python -m unittest -v          # 先看：3 个用例，1 个红")
    print(f"  xiaoyu \"$(cat prompt.txt)\"      # 让小羽修；prompt.txt 里写的是：{prompt}")
    print("  python -m unittest -v          # 再看：应该全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
