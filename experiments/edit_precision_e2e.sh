#!/usr/bin/env bash
# 大文件定点编辑验收：130 行文件里只改该改的那几行，不许整文件重写。
#
# 用 diff 抓「偷懒整文件重写」—— 如果模型用 write_file 全量覆盖，
# 25 个占位函数很可能被改动或丢掉，diff 会立刻暴露。
set -uo pipefail
source "$(dirname "$0")/lib.sh"
require_bin

ROOT=/tmp/xiaoyu-edit
OUT=/tmp/xiaoyu-edit.txt

rm -rf "$ROOT" && mkdir -p "$ROOT"
python3 - "$ROOT" <<'PY'
import sys
from pathlib import Path

head = '''"""内部 HTTP 客户端封装。"""

import time
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 10
MAX_RETRIES = 4


class HttpError(Exception):
    """请求失败。"""


def _build_request(url, method, headers, body):
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    return request


def fetch(url, method="GET", headers=None, body=None, timeout=DEFAULT_TIMEOUT):
    """带重试的请求。失败会指数退避后重试。"""
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            request = _build_request(url, method, headers, body)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.URLError as exc:
            last_error = exc
            time.sleep(1)
    raise HttpError(f"{url} 请求失败：{last_error}")
'''
filler = "\n\n".join(
    f'def helper_{n}(value):\n    """占位工具函数 {n}。"""\n    return value * {n}'
    for n in range(1, 26)
)
Path(sys.argv[1], "http_client.py").write_text(head + "\n\n" + filler + "\n", encoding="utf-8")
PY

cp "$ROOT/http_client.py" "$ROOT/http_client.py.orig"
echo "原始文件 $(wc -l < "$ROOT/http_client.py") 行"
echo

"$XIAOYU_BIN" --yolo --workspace "$ROOT" \
  "http_client.py 的 fetch 文档说是指数退避，但实现里 time.sleep(1) 是固定间隔。\
改成指数退避（1、2、4 秒），并且最后一次尝试失败后不要再多睡一次。只改这一处，别动别的函数。" >"$OUT" 2>&1

cat "$OUT"

echo
echo "=== 判定 1：只改了目标位置 ==="
diff "$ROOT/http_client.py.orig" "$ROOT/http_client.py" | sed 's/^/  /'
echo "  （diff 只应有 fetch 里退避那一处）"

echo
echo "=== 判定 2：用了 str_replace 而不是整文件重写 ==="
tool_sequence "$OUT"
grep -q "⚙ str_replace" "$OUT" && echo "  OK   用了 str_replace" || echo "  FAIL 没用 str_replace"
grep -q "⚙ write_file" "$OUT" && echo "  FAIL 出现了 write_file（整文件覆盖）" || echo "  OK   没有整文件覆盖"

echo
echo "=== 判定 3：退避行为正确（1、2、4 秒，最后一次不再等待）==="
python3 - "$ROOT" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
import time
import urllib.error

import http_client

slept = []
time.sleep = lambda seconds: slept.append(seconds)
http_client.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
    urllib.error.URLError("forced")
)
try:
    http_client.fetch("http://example.invalid")
except http_client.HttpError:
    pass
else:
    raise AssertionError("全部重试失败后应该抛 HttpError")
assert slept == [1, 2, 4], f"退避序列不对：{slept}"
print("  OK   退避序列 [1, 2, 4]")
PY

echo
echo "=== 判定 4：语法正确 ==="
python3 -c "import ast; ast.parse(open('$ROOT/http_client.py').read()); print('  OK   语法正常')"
