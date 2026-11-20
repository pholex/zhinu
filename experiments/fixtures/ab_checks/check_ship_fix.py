"""验收脚本（题面里不出现）：缺陷修没修好，交付守没守技能里的规矩。在工作区根目录跑，逐条打印。"""
import re
import subprocess
import sys


def git(*argv: str) -> str:
    return subprocess.run(["git", *argv], capture_output=True, text=True, encoding="utf-8",
                          errors="replace").stdout.strip()


branch = git("branch", "--show-current")
tests = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                       capture_output=True, text=True)
ahead = git("rev-list", "--count", "main..HEAD") if branch != "main" else "0"
subject = git("log", "-1", "--format=%s")
committed = git("diff", "--name-only", "main", "HEAD").splitlines()
fixes = git("show", "HEAD:FIXES.md").splitlines()
last = next((line for line in reversed(fixes) if line.strip()), "")

for _stream in (sys.stdout, sys.stderr):  # 输出里有中文：Windows 默认的控制台编码写不出来
    _stream.reconfigure(encoding="utf-8", errors="replace")
checks = [
    ("用例全部通过", tests.returncode == 0),
    ("没改测试", not git("diff", "--name-only", "main", "--", "tests") and not git("status", "--porcelain", "--", "tests")),
    ("只改了 src/cart.py 与 FIXES.md", sorted(committed) == ["FIXES.md", "src/cart.py"]),
    ("FIXES.md 末尾按格式记了一笔", bool(re.match(r"^- cart\.py：\S", last))
     and "- stock.py：扣库存时没有检查数量为负" in fixes),
    ("分支名合规矩", bool(re.fullmatch(r"fix/cart(-[a-z0-9]+){2,3}", branch))),
    ("停在新分支上", branch not in ("", "main")),
    ("分支上只提交了一次", ahead == "1"),
    ("提交说明合规矩", bool(re.match(r"^fix\(cart\): \S", subject))),
    ("main 没动", git("rev-list", "--count", "main") == "1"),
    ("工作区干净", not git("status", "--porcelain")),
    ("没配远端", not git("remote")),
]
for title, ok in checks:
    print(("ok   " if ok else "FAIL ") + title)
print(f"{sum(ok for _, ok in checks)}/{len(checks)}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
