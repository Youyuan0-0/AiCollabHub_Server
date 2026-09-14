import pytest

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # 隔离环境变量和当前目录，让各用例的配置读取不受真实 .env 文件影响。
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)
