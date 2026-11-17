"""生成"多个大模块 + 版本号常量"的测试仓库，用于压缩验收。

任务需要用到每个模块里的 VERSION 常量，而读完这些文件必然触发压缩 ——
所以它同时验证了「压缩发生了」和「被压掉的事实还在」。
"""

import shutil
import sys
from pathlib import Path

MODULES = (
    ("alpha", "alpha-7"),
    ("beta", "beta-19"),
    ("gamma", "gamma-33"),
    ("delta", "delta-51"),
    ("epsilon", "epsilon-88"),
)


def build(root: Path, count: int | None = None) -> None:
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    for name, version in MODULES[: count or len(MODULES)]:
        filler = "\n\n".join(
            f"def {name}_step_{n}(value):\n"
            f'    """{name} 流水线第 {n} 步，占位实现。"""\n'
            f"    return value + {n}"
            for n in range(1, 41)
        )
        (root / f"{name}.py").write_text(
            f'"""{name} 模块。"""\n\nVERSION = "{version}"\n\n\n{filler}\n', encoding="utf-8"
        )


if __name__ == "__main__":
    target = Path(sys.argv[1])
    build(target, int(sys.argv[2]) if len(sys.argv) > 2 else None)
    files = sorted(target.glob("*.py"))
    lines = sum(len(path.read_text().splitlines()) for path in files)
    print(f"已生成 {target}：{len(files)} 个模块 / {lines} 行")
