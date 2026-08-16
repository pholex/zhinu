#!/usr/bin/env bash
# explore 子 agent 的 A/B 实验。
#
# 用法：
#   ./explore_ab.sh off      # 关闭 explore，主模型自己翻（基线）
#   ./explore_ab.sh on       # 开启 explore，任务措辞中立 —— 看模型会不会自己用
#   ./explore_ab.sh forced   # 开启并在任务里强制要求用 explore —— 量「用了到底省不省」
#   ./explore_ab.sh compare  # 汇总已有输出
#
# 任务是 4 层间接跳转的链路追踪，每层都有指向别处的 FALLBACK 诱饵，
# 另有完全不参与链路的 decoy.py —— 单次 grep 解决不了，必须真的多跳探索。
#
# ⚠️ 用 --yolo 跑（无人值守），工作区在 /tmp。bash 工具在 --yolo 下没有路径边界，
#    只在可丢弃目录里用。
set -uo pipefail
source "$(dirname "$0")/lib.sh"
require_bin

MODE="${1:-on}"
ROOT="/tmp/xiaoyu-explore-$MODE"
OUT="/tmp/xiaoyu-explore-$MODE.txt"

TASK_NEUTRAL="pkg/ 里 ROUTES 注册表把 entrypoint 映射到处理器，处理器之间又通过 FORWARD_TO 常量层层转交。\
请查清 entrypoint 'alpha' 的调用链最终落到哪个文件的哪个函数（注意每个模块里的 FALLBACK 是诱饵，\
实际生效的是 FORWARD_TO）。查清后新建 report.py，写一个 answer() 函数返回字符串 '文件路径:函数名'，\
文件路径用相对工作区的形式如 pkg/xxx.py。写完跑一下确认返回值。"

TASK_FORCED="pkg/ 里 ROUTES 注册表把 entrypoint 映射到处理器，处理器之间又通过 FORWARD_TO 常量层层转交。\
必须先用 explore 工具把 entrypoint 'alpha' 的调用链查清（不要自己逐个 read_file 翻，\
交给 explore 去查，并要求它给出文件和行号）。拿到结论后新建 report.py，写一个 answer() 函数返回\
字符串 '文件路径:函数名'，文件路径用相对工作区的形式如 pkg/xxx.py。写完跑一下确认返回值。"

compare () {
  echo "=== 汇总（缺失的组会跳过）==="
  python3 - <<'PY'
import re
from pathlib import Path

PRICE = {"deepseek-v4-pro": (4.411764706e-7, 8.823529412e-7),
         "deepseek-v4-flash": (1.470588235e-7, 2.941176471e-7)}

def stats(path):
    text = Path(path).read_text(errors="replace")
    main_in, cost = 0, 0.0
    for model, (pin, pout) in PRICE.items():
        m = re.search(rf"{re.escape(model)}: \d+ 次 · in (\d+) / out (\d+)", text)
        if not m:
            continue
        tin, tout = int(m.group(1)), int(m.group(2))
        cost += tin * pin + tout * pout
        if model == "deepseek-v4-pro":
            main_in = tin
    return main_in, cost, "🔍 explore（" in text

base = None
for label, path in (("off    ", "/tmp/xiaoyu-explore-off.txt"),
                    ("on     ", "/tmp/xiaoyu-explore-on.txt"),
                    ("forced ", "/tmp/xiaoyu-explore-forced.txt")):
    try:
        main_in, cost, used = stats(path)
    except OSError:
        print(f"  {label} 未跑")
        continue
    if base is None:
        base = (main_in, cost)
    dm = f"{(main_in - base[0]) / base[0]:+.0%}" if base[0] else "—"
    dc = f"{(cost - base[1]) / base[1]:+.0%}" if base[1] else "—"
    print(f"  {label} 主模型 in {main_in:>6} ({dm:>5}) · ${cost:.5f} ({dc:>5}) · explore {'用了' if used else '没用'}")
PY
}

if [ "$MODE" = "compare" ]; then
  compare
  exit 0
fi

python3 "$FIXTURES/multihop_repo.py" "$ROOT" || exit 1
test -d "$ROOT/pkg" || { echo "仓库生成失败，中止"; exit 1; }

case "$MODE" in
  off)    ENABLE=0; TASK="$TASK_NEUTRAL" ;;
  on)     ENABLE=1; TASK="$TASK_NEUTRAL" ;;
  forced) ENABLE=1; TASK="$TASK_FORCED" ;;
  *) echo "未知模式：$MODE（off / on / forced / compare）"; exit 2 ;;
esac

XIAOYU_ENABLE_EXPLORE="$ENABLE" "$XIAOYU_BIN" --yolo --workspace "$ROOT" "$TASK" >"$OUT" 2>&1

echo "### $MODE"
report_usage "$OUT"
used_explore "$OUT"
echo -n "    答案正确: "
if [ -f "$ROOT/report.py" ]; then
  (cd "$ROOT" && python3 -c "
from report import answer
got = answer()
assert 'final' in got and 'execute_payload' in got, f'错: {got}'
print('是 →', got)" 2>&1 | tail -1)
else
  echo "没生成 report.py"
fi
tool_sequence "$OUT"
echo
compare
