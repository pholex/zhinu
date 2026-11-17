"""生成多跳链路测试仓库。独立成文件，避免从 shell 脚本里 sed 抽函数（上次因此截断失败）。

真链路：ROUTES["alpha"] → stage_one.begin → stage_two.middle
        → stage_three.finish → final.execute_payload
每层都有指向别处的 FALLBACK 诱饵，另有完全不参与链路的 decoy.py。
"""

import shutil
import sys
from pathlib import Path


def pad(name: str, count: int = 26) -> str:
    return "\n\n".join(
        f"def {name}_noop_{n}(payload):\n"
        f'    """{name} 占位处理器 {n}。"""\n'
        f"    return payload"
        for n in range(1, count + 1)
    )


def build(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
    pkg = root / "pkg"
    pkg.mkdir(parents=True)

    files = {
        "registry.py": f'"""入口注册表。"""\n\n{pad("registry")}\n\n\n'
        'ROUTES = {\n'
        '    "beta": "pkg.stage_two:middle",\n'
        '    "gamma": "pkg.decoy:handle",\n'
        '    "alpha": "pkg.stage_one:begin",\n'
        '    "delta": "pkg.stage_three:finish",\n'
        "}\n",
        "stage_one.py": f'"""第一跳。"""\n\n'
        '#  FALLBACK 是诱饵，实际生效的是 FORWARD_TO\n'
        'FALLBACK = "pkg.decoy:handle"\n'
        'FORWARD_TO = "pkg.stage_two:middle"\n\n'
        f'{pad("stage_one")}\n\n\n'
        "def begin(payload):\n"
        '    """把 payload 转交给 FORWARD_TO 指向的目标。"""\n'
        '    return {"forward": FORWARD_TO, "payload": payload}\n',
        "stage_two.py": f'"""第二跳。"""\n\n'
        'FALLBACK = "pkg.final:legacy_entry"\n'
        'FORWARD_TO = "pkg.stage_three:finish"\n\n'
        f'{pad("stage_two")}\n\n\n'
        "def middle(payload):\n"
        '    """继续转交给 FORWARD_TO。"""\n'
        '    return {"forward": FORWARD_TO, "payload": payload}\n',
        "stage_three.py": f'"""第三跳，最后一跳。"""\n\n'
        'FALLBACK = "pkg.decoy:handle"\n'
        'FORWARD_TO = "pkg.final:execute_payload"\n\n'
        f'{pad("stage_three")}\n\n\n'
        "def finish(payload):\n"
        '    """转交给最终执行者。"""\n'
        '    return {"forward": FORWARD_TO, "payload": payload}\n',
        "final.py": f'"""终点。"""\n\n{pad("final")}\n\n\n'
        "def legacy_entry(payload):\n"
        '    """已废弃的旧入口，不要用。"""\n'
        '    raise RuntimeError("deprecated")\n\n\n'
        "def execute_payload(payload):\n"
        '    """真正干活的地方。"""\n'
        '    return {"done": True, "payload": payload}\n',
        "decoy.py": f'"""诱饵模块，链路不会走到这里。"""\n\n{pad("decoy")}\n\n\n'
        "def handle(payload):\n"
        '    """诱饵处理器。"""\n'
        "    return payload\n",
    }
    for name, content in files.items():
        (pkg / name).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    target = Path(sys.argv[1])
    build(target)
    count = len(list((target / "pkg").glob("*.py")))
    lines = sum(len(p.read_text().splitlines()) for p in (target / "pkg").glob("*.py"))
    print(f"已生成 {target}：{count} 个模块 / {lines} 行")
