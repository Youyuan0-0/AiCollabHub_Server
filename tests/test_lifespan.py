"""合并后的日志与工具链共享启动流程，失败退出也必须回收已创建的资源。"""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.resources import ApplicationResources
from app.core.system_logging import SystemLogWriter
from app.main import create_app
from app.modules.applications import runtime


@pytest.mark.parametrize("fail_startup", [False, True])
def test_toolchain_and_logs_close_before_shared_resources(monkeypatch, fail_startup):
    """正常关闭及运行提供器初始化失败时，客户端和日志都先于共享连接池关闭。"""
    events = []
    resources = ApplicationResources()
    resources._cleanup.callback(lambda: events.append("resources_closed"))
    original_writer_close = SystemLogWriter.close
    original_provider = runtime.AgentDevProvider

    def resources_factory(settings):
        events.append("resources_created")
        return resources

    def close_writer(writer):
        assert all(client.http.is_closed for client in application.state.toolchain_clients.values())
        original_writer_close(writer)
        events.append("writer_closed")

    def runtime_provider(provider, settings):
        assert application.state.system_log_writer is not None
        assert len(application.state.toolchain_clients) == 3
        if fail_startup:
            raise RuntimeError("runtime_startup_failed")
        return original_provider(provider, settings)

    monkeypatch.setattr(SystemLogWriter, "close", close_writer)
    monkeypatch.setattr(runtime, "AgentDevProvider", runtime_provider)
    application = create_app(
        Settings(applications_runtime="agent_dev", toolchain_mode="disabled"),
        resources_factory=resources_factory,
    )
    if fail_startup:
        with pytest.raises(RuntimeError, match="runtime_startup_failed"), TestClient(application):
            pytest.fail("启动失败不应进入请求处理阶段")
    else:
        with TestClient(application) as client:
            assert client.get("/api/v1/health/live").status_code == 200
            assert not any(
                adapter.http.is_closed for adapter in application.state.toolchain_clients.values()
            )
    assert events == ["resources_created", "writer_closed", "resources_closed"]
    assert application.state.system_log_writer is None
    assert application.state.database_engine is None
    assert application.state.resources is None
