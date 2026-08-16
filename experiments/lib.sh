#!/usr/bin/env bash
# 实验脚本共用的小工具。
#
# macOS 注意：没有 setsid。后台跑长任务用
#   nohup bash -c "trap '' HUP; exec <命令>" >日志 2>&1 &
# 只用 nohup 不够 —— nohup 只让直接子进程忽略 SIGHUP，
# bash -l 起的孙进程会把 SIGHUP 恢复成默认处理，终端一断就被杀（踩过）。
set -uo pipefail

XIAOYU_BIN="${XIAOYU_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv/bin/xiaoyu}"
FIXTURES="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/fixtures"

#  单价（USD / token），与 eval 用的那份候选数据保持一致
declare -A PRICE_IN=(
  [deepseek-v4-pro]=0.0000004411764706
  [deepseek-v4-flash]=0.0000001470588235
)
declare -A PRICE_OUT=(
  [deepseek-v4-pro]=0.0000008823529412
  [deepseek-v4-flash]=0.0000002941176471
)

require_bin () {
  test -x "$XIAOYU_BIN" || { echo "找不到 xiaoyu：$XIAOYU_BIN（先 pip install -e .）"; exit 1; }
}

#  从一次运行的输出里抽按模型的用量，算总成本
report_usage () {
  local out="$1"
  grep -E "次模型调用|: [0-9]+ 次 · in " "$out" | sed 's/^/    /'
  python3 - "$out" <<'PY'
import re, sys
from pathlib import Path

PRICE = {"deepseek-v4-pro": (4.411764706e-7, 8.823529412e-7),
         "deepseek-v4-flash": (1.470588235e-7, 2.941176471e-7)}
text = Path(sys.argv[1]).read_text(errors="replace")
total = 0.0
main_in = 0
for model, (pin, pout) in PRICE.items():
    m = re.search(rf"{re.escape(model)}: \d+ 次 · in (\d+) / out (\d+)", text)
    if not m:
        continue
    tin, tout = int(m.group(1)), int(m.group(2))
    total += tin * pin + tout * pout
    if model == "deepseek-v4-pro":
        main_in = tin
print(f"    主模型 in {main_in} tok · 总成本 ${total:.5f}")
PY
}

tool_sequence () {
  echo -n "    工具序列: "
  grep -oE "^⚙ [a-z_]+" "$1" | awk '{print $2}' | tr '\n' ' '
  echo
}

used_explore () {
  if grep -q "🔍 explore（" "$1"; then
    echo "    用到 explore: 是（$(grep -c '🔍 explore（' "$1") 次）"
  else
    echo "    用到 explore: 否"
  fi
}
