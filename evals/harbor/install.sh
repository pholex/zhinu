#!/bin/sh
# 在任务容器里装小羽（以 root 运行）。用法：install.sh <wheel 路径> [extras 如 "[bedrock]"]
#
# 顺序：已有 uv → 用它；没有就 curl/wget 装一个到 /installed-agent 下；
# 两样都没有再退回系统 python3（要 >= 3.11 且带 venv + pip）。uv 路线下
# 系统解释器够新就直接复用，不够就由 uv 下载一份——任务镜像里 Python 版本
# 五花八门，这是唯一不用逐镜像适配的办法。
set -eu

WHEEL="$1"
EXTRAS="${2:-}"
ROOT=/installed-agent
PYSPEC="${XIAOYU_HARBOR_PYTHON:->=3.11,<3.15}"

export UV_INSTALL_DIR="$ROOT/uv-bin"
export UV_PYTHON_INSTALL_DIR="$ROOT/python"
export UV_CACHE_DIR="$ROOT/uv-cache"
export INSTALLER_NO_MODIFY_PATH=1
export UV_NO_PROGRESS=1

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
    elif [ -x "$ROOT/uv-bin/uv" ]; then
        echo "$ROOT/uv-bin/uv"
    fi
}

fetch_uv() {
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        return 1
    fi
}

install_with_uv() {
    UV="$(find_uv)"
    if [ -z "$UV" ]; then
        fetch_uv || return 1
        UV="$(find_uv)"
    fi
    [ -n "$UV" ] || return 1
    "$UV" venv --python "$PYSPEC" "$ROOT/venv"
    "$UV" pip install --python "$ROOT/venv/bin/python" "$WHEEL$EXTRAS"
}

install_with_system_python() {
    command -v python3 >/dev/null 2>&1 || return 1
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || return 1
    python3 -m venv "$ROOT/venv" || return 1
    "$ROOT/venv/bin/python" -m pip install --quiet "$WHEEL$EXTRAS"
}

rm -rf "$ROOT/venv"
if ! install_with_uv; then
    echo "uv 路线不可用，退回系统 python3" >&2
    rm -rf "$ROOT/venv"
    install_with_system_python || {
        echo "装不上：既取不到 uv（缺 curl/wget 或无网络），系统 python3 也不满足 >= 3.11 + venv + pip" >&2
        exit 1
    }
fi

ln -sf "$ROOT/venv/bin/xiaoyu" /usr/local/bin/xiaoyu
chmod -R a+rX "$ROOT"
"$ROOT/venv/bin/xiaoyu" --version
