import hmac
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import APIKeyHeader
from pydantic import ValidationError

from app.db.session import SessionDependency
from app.modules.common.identity_sync import service
from app.modules.common.identity_sync.models import IdentitySyncReceipt
from app.modules.common.identity_sync.schemas import EventId, SyncBatch, SyncResult, request_schema

router = APIRouter(prefix="/auth/mock/identity-sync", tags=["mock-identity-sync"])
MOCK_IAM_KEY = APIKeyHeader(name="X-Mock-IAM-Key", auto_error=False)


def require_mock_source(
    request: Request, key: Annotated[str | None, Depends(MOCK_IAM_KEY)]
) -> None:
    """限定为非生产模拟认证，并验证同步推送或回执查询的来源密钥。"""
    settings = request.app.state.settings
    if settings.auth_mode != "mock" or settings.app_env == "production":
        raise HTTPException(404, "not_found")
    if not hmac.compare_digest(
        (key or "").encode(), settings.auth_mock_secret.get_secret_value().encode()
    ):
        raise HTTPException(403, "notification_source_rejected")


async def read_batch(
    request: Request, _: Annotated[None, Depends(require_mock_source)]
) -> SyncBatch:
    """读取有大小限制的 JSON 批次，校验失败时仅返回字段位置和原因。"""
    if request.headers.get("content-type", "").split(";")[0].lower() != "application/json":
        raise HTTPException(415, "json_required")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 65536:
            raise HTTPException(413, "sync_payload_too_large")
    try:
        return SyncBatch.model_validate_json(bytes(body))
    except ValidationError as error:
        # 只返回字段路径和原因，不回显供应方原始数据。
        raise HTTPException(
            422, error.errors(include_input=False, include_context=False, include_url=False)
        ) from None


@router.post(
    "",
    response_model=SyncResult,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": request_schema()}},
        },
    },
)
def receive(
    request: Request, batch: Annotated[SyncBatch, Depends(read_batch)], db: SessionDependency
) -> SyncResult:
    """接收平台内部模拟事件；集团的编号、状态及签名必须经正式适配后才能接入。"""
    return service.receive(db, request.app.state.settings, batch)


@router.get("/{event_id}", response_model=SyncResult, dependencies=[Depends(require_mock_source)])
def receipt(event_id: EventId, db: SessionDependency) -> SyncResult:
    """查询数据库中的处理回执；提交结果不确定时使用相同 eventId 和报文重试。"""
    value = db.get(IdentitySyncReceipt, event_id)
    if value is None:
        raise HTTPException(404, "sync_event_not_found")
    return service.result_for(value)
