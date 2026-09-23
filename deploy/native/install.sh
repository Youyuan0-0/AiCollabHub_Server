#!/usr/bin/env bash
# 校验光盘内容，并用随包的 Python 执行安装；不替换麒麟自带 Python。
set -Eeuo pipefail
umask 077
BUNDLE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if [[ $EUID -ne 0 ]]; then
    printf '请使用 sudo bash install.sh --config /绝对路径/site.json\n' >&2
    exit 1
fi
if [[ $(uname -m) != x86_64 ]] || [[ ! -f /etc/kylin-release ]]; then
    printf '本安装包仅支持麒麟 V10 SP3 x86_64。\n' >&2
    exit 1
fi
cd "$BUNDLE_DIR"
printf '正在校验离线包完整性……\n'
sha256sum --check --strict --quiet SHA256SUMS
BOOTSTRAP_DIR=$(mktemp -d /var/tmp/aih-native-install.XXXXXXXX)
trap 'rm -rf -- "$BOOTSTRAP_DIR"' EXIT
tar -xzf runtime.tar.gz -C "$BOOTSTRAP_DIR"
export LD_LIBRARY_PATH="$BOOTSTRAP_DIR/runtime/lib"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
"$BOOTSTRAP_DIR/runtime/python/bin/python3.13" "$BUNDLE_DIR/installer.py" \
    --bundle "$BUNDLE_DIR" --runtime-source "$BOOTSTRAP_DIR/runtime" "$@"
