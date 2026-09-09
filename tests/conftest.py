import pytest


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path):
    # 隔离环境变量和当前目录，让各用例的配置读取不受真实 .env 文件影响。
    for name in ("APP_NAME", "APP_ENV", "DATABASE_URL", "CORS_ORIGINS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
