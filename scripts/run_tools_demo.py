"""运行完全隔离的工具联调实例，不读取本机环境配置或业务数据库。"""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import Settings  # noqa: E402

# 清理仅属于此测试进程的配置；不会修改系统环境、.env 或数据库。
for name in Settings.model_fields:
    os.environ.pop(name.upper(), None)
os.chdir(Path(__file__).resolve().parent)

import uvicorn  # noqa: E402

from app.main import create_app  # noqa: E402

if __name__ == "__main__":
    # 配置保存测试也使用临时目录，不能污染下一次联调或本地实际配置。
    with TemporaryDirectory(prefix="aicollabhub-tools-demo-") as demo_directory:
        uvicorn.run(
            create_app(
                Settings(
                    _env_file=None,
                    app_env="test",
                    auth_mode="disabled",
                    toolchain_tools_mode="mock",
                    toolchain_configuration_file=Path(demo_directory) / "toolchain.json",
                    toolchain_mode="mock" if "--toolchain" in sys.argv[2:] else "disabled",
                )
            ),
            host="127.0.0.1",
            port=int(sys.argv[1]) if len(sys.argv) > 1 else 8018,
        )
