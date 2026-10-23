#!/usr/bin/env bash
# 一键本地演示（Linux / macOS）：建虚拟环境 -> 装依赖 -> 以演示模式启动，仅监听 127.0.0.1。
# 用法：./run-demo.sh [端口，默认 8000] [--synthetic]
#   默认使用 2026-10-09 历史公开样本的私有副本；--synthetic 改用带【演示】标记的合成数据。
# 采集器默认关闭，不会访问外部网站（首次安装依赖需要联网访问 PyPI）。
set -euo pipefail
cd "$(dirname "$0")"
PORT=8000
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --synthetic) EXTRA+=("--synthetic") ;;
    ''|*[!0-9]*) echo "未知参数: $arg"; exit 2 ;;
    *) PORT="$arg" ;;
  esac
done
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "请先安装 Python 3.12+（找不到 $PYTHON）"; exit 1; }
if [ ! -x .venv/bin/python ]; then
  echo ">> 创建虚拟环境 .venv"
  "$PYTHON" -m venv .venv
fi
if [ ! -f .venv/.demo-deps-installed ] || [ requirements-campus.lock.txt -nt .venv/.demo-deps-installed ]; then
  echo ">> 安装依赖（锁定版本）"
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
  touch .venv/.demo-deps-installed
fi
export PYTHONUTF8=1
exec .venv/bin/python demo_server.py --port "$PORT" ${EXTRA[@]+"${EXTRA[@]}"}
