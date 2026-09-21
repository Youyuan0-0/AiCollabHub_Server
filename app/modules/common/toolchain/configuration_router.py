"""工具链接入配置管理入口，复用平台登录和 CSRF 校验。"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.modules.common.identity import current_actor
from app.modules.common.toolchain.configuration import MAX_CONFIGURATION_BYTES, parse_save_payload

router = APIRouter(prefix="/admin/toolchain-configuration", tags=["toolchain configuration"])


def administrator(request: Request) -> str:
    """全局服务凭据仅限系统管理员维护，不向单位管理员开放。"""
    actor = current_actor(request, True)
    if actor.role != "admin":
        raise HTTPException(403, "仅系统管理员可以管理工具链接入配置")
    return actor.id


@router.get("")
def configuration(request: Request) -> dict:
    """读取脱敏配置及当前进程的生效状态。"""
    administrator(request)
    return request.app.state.toolchain_configuration.view()


@router.put("")
async def save_configuration(request: Request) -> dict:
    """验证身份、大小及字段后保存配置，不调用供应方接口或数据库。"""
    subject = administrator(request)
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_CONFIGURATION_BYTES:
            raise HTTPException(413, "配置请求不能超过 1 MiB")
    payload = parse_save_payload(bytes(body))
    return await run_in_threadpool(request.app.state.toolchain_configuration.save, payload, subject)
