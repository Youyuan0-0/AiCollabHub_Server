"""读取本套部署的配置文件；不执行 shell，不输出配置值。"""

import json
import re
from pathlib import Path


def effective_environment(directory: Path) -> dict[str, str]:
    """以 systemd 使用的 app.env 为准，兼容历史 JSON 和单行手工修改。"""
    saved = directory / "app-env.json"
    values = json.loads(saved.read_text(encoding="utf-8")) if saved.exists() else {}
    environment = directory / "app.env"
    if not environment.exists():
        return values
    for number, line in enumerate(environment.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"app.env 第 {number} 行不是有效的单行配置")
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except ValueError:
                raise ValueError(f"app.env 第 {number} 行引号格式无效") from None
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        elif "\\" in value or value.startswith("'"):
            raise ValueError(f"app.env 第 {number} 行请使用单行值或 JSON 双引号")
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError(f"app.env 第 {number} 行包含不支持的值")
        values[key] = value
    return values
