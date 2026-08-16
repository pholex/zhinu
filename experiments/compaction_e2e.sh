#!/usr/bin/env bash
# 上下文压缩 + 便宜模型摘要 的端到端验收。
#
# 设计要点（前两版都因为这个失败）：
#   任务必须用到「被压掉的那段历史里的事实」——读完 5 个模块拿到各自 VERSION，
#   压缩发生后再把它们写进 report.py。摘要没保住事实，任务就完不成。
#   判定必须显式确认「压缩真的成功了」，而不只是「结果对」：
#     v1 上限设成 20000，实际历史只到 4727 tok，压缩从未触发，任务却做对了 → 假通过
#     v2 keep_recent=8 覆盖了全部历史 → 一直「跳过」，唯一成功那次把原始任务压掉了还变大
set -uo pipefail
source "$(dirname "$0")/lib.sh"
require_bin

ROOT=/tmp/xiaoyu-compaction
OUT=/tmp/xiaoyu-compaction.txt

python3 "$FIXTURES/versioned_modules.py" "$ROOT" || exit 1

#  按实测：读完 1 个模块 ≈ 2340 tok、2 个 ≈ 3520 tok。
#  上限 6000 + keep_recent=4 → 中途必然触发且能压出实际收益。
XIAOYU_CONTEXT_LIMIT=6000 XIAOYU_KEEP_RECENT=4 \
  "$XIAOYU_BIN" --yolo --workspace "$ROOT" \
  "依次读 alpha.py、beta.py、gamma.py、delta.py、epsilon.py 五个文件，然后新建 report.py，\
写一个 build_report() 函数，返回把五个模块的 VERSION 常量按上述顺序用 ' | ' 连起来的字符串。\
不要 import 那五个模块，直接把版本号字面量写进去。写完跑一下确认输出正确。" >"$OUT" 2>&1

echo "=== 判定 1：压缩是否真的成功（不是跳过）==="
if grep -q "已压缩" "$OUT"; then
  grep -n "已压缩" "$OUT" | sed 's/^/  OK   /'
else
  echo "  MISS 没有一次成功压缩 —— 本次对压缩无效"
  grep -n "压缩历史\|跳过：\|压缩失败" "$OUT" | sed 's/^/       /'
fi

echo
echo "=== 判定 2：摘要是否走了便宜模型 ==="
if grep -q "摘要由 .* 生成" "$OUT"; then
  grep -o "摘要由 .* 生成" "$OUT" | head -1 | sed 's/^/  OK   /'
elif grep -q "摘要模型 .* 失败" "$OUT"; then
  grep -o "摘要模型 .* 失败" "$OUT" | head -1 | sed 's/^/  FAIL 回退了主模型：/'
else
  echo "  n/a  没触发摘要（见判定 1）"
fi

echo
echo "=== 判定 3：按模型分开的用量 ==="
report_usage "$OUT"

echo
echo "=== 判定 4：五个版本号是否都活过了压缩 ==="
for v in alpha-7 beta-19 gamma-33 delta-51 epsilon-88; do
  if grep -q "$v" "$ROOT/report.py" 2>/dev/null; then echo "  OK   $v"; else echo "  MISS $v"; fi
done

echo
echo "=== 判定 5：产物实际可运行 ==="
(cd "$ROOT" && python3 -c "
from report import build_report
out = build_report()
want = 'alpha-7 | beta-19 | gamma-33 | delta-51 | epsilon-88'
assert out == want, f'不对:\n  得到 {out}\n  期望 {want}'
print('  OK   build_report() 输出正确')" 2>&1 | sed 's/^/  /')

echo
echo "=== 判定 6：没有 API 400 / 孤儿 tool_calls / 连续 user 消息 ==="
if grep -qiE "400|BadRequest|invalid_request|roles must alternate|请求失败" "$OUT"; then
  echo "  FAIL 疑似 API 错误："
  grep -niE "400|BadRequest|invalid_request|roles must alternate|请求失败" "$OUT" | sed 's/^/       /'
else
  echo "  OK   无 API 错误"
fi
