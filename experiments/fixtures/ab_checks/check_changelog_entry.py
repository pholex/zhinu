"""验收脚本（题面里不出现）：发版条目守没守技能里的每条规则。在工作区根目录跑，逐条打印。"""
import subprocess
import sys
from pathlib import Path

text = Path("CHANGELOG.md").read_text(encoding="utf-8", errors="replace") if Path("CHANGELOG.md").is_file() else ""
lines = [line.rstrip() for line in text.splitlines()]
start = next((i for i, line in enumerate(lines) if line.startswith("## 0.3.0")), None)
end = next((i for i, line in enumerate(lines) if start is not None and i > start and line.startswith("## ")), len(lines))
entry = [line for line in lines[start + 1:end] if line.strip()] if start is not None else []
want = ["- 新增：导出支持选择列（#38）", "- 新增：列表页可以保存筛选条件（#52）",
        "- 修复：重复提交同一张表单时生成两条记录（#47）", "- 修复：时区为负偏移时日期差一天（#55）"]
pending = Path("CHANGES/pending")
changed = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True).stdout.splitlines()
outside = [row for row in changed
           if not (row[3:].startswith("CHANGES/pending") or row[3:] == "CHANGELOG.md")]
commits = subprocess.run(["git", "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()

for _stream in (sys.stdout, sys.stderr):  # 输出里有中文：Windows 默认的控制台编码写不出来
    _stream.reconfigure(encoding="utf-8", errors="replace")
checks = [
    ("标题用给定的版本号与 RELEASE_DATE 里的日期", start is not None and lines[start] == "## 0.3.0 — 2026-11-20"),
    ("插在标题行之后、上一个版本之前", start is not None and "## 0.2.1" in "\n".join(lines[end:end + 1])
     and lines[0] == "# 变更记录"),
    ("内部改动没写进条目", start is not None and not any("#49" in line or "请求封装" in line for line in entry)),
    ("四条都在，说明照抄原文", all(line in entry for line in want)),
    ("先新增后修复，同类按编号从小到大", entry == want),
    ("以前的条目没动", "- 修复：导出时文件名里的空格被截断（#31）" in lines
     and "- 新增：支持按标签筛选（#24）" in lines),
    ("片段全部删掉（含没写进条目的）", not pending.is_dir() or not list(pending.glob("*.md"))),
    ("没动别的文件", not outside),
    ("没有提交", commits == "1"),
]
for title, ok in checks:
    print(("ok   " if ok else "FAIL ") + title)
print(f"{sum(ok for _, ok in checks)}/{len(checks)}")
sys.exit(0 if all(ok for _, ok in checks) else 1)
