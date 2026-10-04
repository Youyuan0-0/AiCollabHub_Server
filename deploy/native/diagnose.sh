#!/usr/bin/env bash
# 可从安装目录或解压目录运行，应用虚拟环境损坏时仍使用独立运行时取证。
set -Eeuo pipefail
umask 077
DIAGNOSTIC_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
export LD_LIBRARY_PATH=/opt/aicollabhub/runtime/lib:/opt/aicollabhub/runtime/postgres/lib
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
exec /opt/aicollabhub/runtime/python/bin/python3.13 "$DIAGNOSTIC_DIR/diagnostics/diagnose.py" "$@"
