"""验证演示身份查询只容忍过期会话，不把权限或服务故障降级为成功。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request

from app.modules.applications.workflow_router import demo_session
from app.modules.common.identity import ACTORS


@pytest.mark.parametrize("audience", ["user", "admin"])
def test_expired_demo_session_keeps_account_choices(audience: str) -> None:
    """过期令牌返回空身份和可选账号，并保留原入口的身份解析参数。"""
    provider = Mock()
    provider.resolve.side_effect = HTTPException(401, "演示会话已失效")
    request = session_request(provider)

    result = demo_session(request, audience)

    assert result["current"] is None
    assert result["actors"] == [actor.public() for actor in ACTORS]
    provider.resolve.assert_called_once_with(request, audience)


@pytest.mark.parametrize(
    "error",
    [HTTPException(403, "权限不足"), HTTPException(503, "身份服务不可用"), RuntimeError("故障")],
)
def test_demo_session_propagates_unexpected_errors(error: Exception) -> None:
    """权限拒绝和内部故障必须保留原异常，由统一错误处理决定接口状态。"""
    provider = Mock()
    provider.resolve.side_effect = error

    with pytest.raises(type(error)) as caught:
        demo_session(session_request(provider))

    assert caught.value is error


def session_request(provider: Mock) -> Request:
    """创建关闭正式登录的隔离请求，无需启动服务或访问本机配置。"""
    application = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(auth_mode="disabled"), application_identity=provider
        )
    )
    return Request({"type": "http", "app": application})
