"""正式 IAM-02 无签名推送入口，按可信连接来源及应用编号校验。"""

from ipaddress import ip_address

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import SessionDependency
from app.modules.common.identity_sync.iam_payload import normalize_iam_payload
from app.modules.common.identity_sync.service import receive

router = APIRouter(prefix="/auth/iam/identity-sync", tags=["identity-sync"])


def verify_sync_source(request: Request) -> None:
    """只使用 ASGI 连接来源；代理头由受信任的 Uvicorn 代理配置解析。"""
    try:
        address = ip_address(request.client.host if request.client else "")
    except ValueError:
        raise HTTPException(403, "notification_source_rejected") from None
    networks = request.app.state.settings.auth_iam_sync_allowed_networks
    if not any(address in network for network in networks):
        raise HTTPException(403, "notification_source_rejected")


@router.post("")
async def receive_iam(request: Request, db: SessionDependency) -> JSONResponse:
    """先限制推送来源，再映射用户/管理员授权并原子提交；不要求应用层签名。"""
    try:
        verify_sync_source(request)
        if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
            raise HTTPException(415, "json_required")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 262144:
                raise HTTPException(413, "sync_payload_too_large")
        settings = request.app.state.settings
        batch = normalize_iam_payload(
            bytes(body), settings.auth_iam_app_id, settings.auth_iam_admin_role_code
        )
        await run_in_threadpool(receive, db, settings, batch, request_id=request.state.request_id)
    except HTTPException as error:
        # 文档规定无论成功或失败都返回 resultCode/resultMsg，不回显集团个人资料。
        return JSONResponse(
            status_code=error.status_code,
            content={"resultCode": error.status_code, "resultMsg": "sync_rejected"},
        )
    except SQLAlchemyError:
        return JSONResponse(
            status_code=503, content={"resultCode": 500, "resultMsg": "sync_unavailable"}
        )
    return JSONResponse({"resultCode": 200, "resultMsg": "success"})
