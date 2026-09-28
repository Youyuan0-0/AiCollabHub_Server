#!/usr/bin/env bash
# 更新已安装的测试环境；复用现场配置，安装器在变更前生成一致性备份。
set -Eeuo pipefail
BUNDLE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec bash "$BUNDLE_DIR/install.sh" --config /etc/aicollabhub/site.json --upgrade "$@"
