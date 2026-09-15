"""开发问答与附件接口；新会话按登录用户隔离，旧共享样例仍保留，生产环境不开放。"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from app.db.session import SessionDependency
from app.schemas.conversations import (
    ChatAnswer,
    ChatInput,
    ConversationAttachment,
    ConversationRecord,
    ConversationSendResult,
    ConversationSummary,
    NewConversationInput,
)
from app.services import conversations
from app.services.attachment_store import MAX_ATTACHMENT_BYTES


def require_demo_environment(request: Request) -> None:
    """只允许服务端非生产配置；不创建演示身份，不放宽其他业务的鉴权。"""
    if getattr(request.app.state, "app_env", "production") not in {"development", "test"}:
        raise HTTPException(503, "conversations_not_configured")


router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_demo_environment)],
)


def request_service(request: Request, session) -> conversations.ConversationService:
    """从可信登录状态构造服务；显式匿名开发沿用共享历史，不接受客户端传入所属用户。"""
    identity = getattr(request.state, "business_identity", None)
    return conversations.service(
        session,
        request.app.state.chat_provider,
        request.app.state.attachment_store,
        identity["id"] if identity else "",
    )


@router.post("/{identifier}/attachments", response_model=ConversationAttachment, status_code=201)
async def upload_attachment(identifier: str, request: Request, session: SessionDependency):
    """每次上传一个原文件；总请求体限额包含表单开销，成功返回可用于发送消息的编号。"""
    record = await run_in_threadpool(request_service(request, session).get, identifier)
    # 登录模式不允许在无主旧共享记录上放置私人文件，应先新建自己的对话。
    if getattr(request.state, "business_identity", None) and not record.owner_id:
        raise HTTPException(409, "attachment_requires_owned_conversation")
    received = 0

    async def limited_receive():
        # 在 multipart 写入临时磁盘前限制字节数，不能仅信任 Content-Length 或 UploadFile.size。
        nonlocal received
        message = await request.receive()
        received += len(message.get("body", b""))
        if received > MAX_ATTACHMENT_BYTES + 64 * 1024:
            # 使用解析器异常触发其临时文件清理，再在外层转为明确的 413。
            raise MultiPartException("attachment_too_large")
        return message

    bounded_request = Request(request.scope, limited_receive)
    try:
        async with bounded_request.form(max_files=1, max_fields=0) as form:
            items = form.multi_items()
            if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
                raise HTTPException(422, "attachment_file_required")
            file = items[0][1]
            return await run_in_threadpool(
                request.app.state.attachment_store.save, identifier, file.filename or "", file.file
            )
    except StarletteHTTPException:
        if received > MAX_ATTACHMENT_BYTES + 64 * 1024:
            raise HTTPException(413, "attachment_too_large") from None
        raise


@router.get("/{identifier}/attachments/{attachment_id}/content")
def download_attachment(
    identifier: str,
    attachment_id: str,
    request: Request,
    session: SessionDependency,
):
    """检查会话访问权后分块下载；强制下载和 nosniff 防止用户文件被作为页面执行。"""
    request_service(request, session).get(identifier)
    store = request.app.state.attachment_store
    attachment = store.get(identifier, attachment_id)
    disposition = f"attachment; filename*=UTF-8''{quote(attachment.name, safe='')}"
    return StreamingResponse(
        store.chunks(identifier, attachment_id),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": disposition,
            "Content-Length": str(attachment.size),
            "X-Content-Type-Options": "nosniff",
        },
    )


async def chat_input(request: Request) -> ChatInput:
    """兼容前端 multipart input+files；附件仅接收后关闭，不解析、不保存、不参与回答。"""
    async with request.form(max_files=10, max_fields=1, max_part_size=65536) as form:
        raw = form.get("input")
        if not isinstance(raw, str):
            raise HTTPException(422, "conversation_input_required")
        if any(key not in {"input", "files"} for key in form):
            raise HTTPException(422, "invalid_conversation_fields")
        attachments = form.getlist("files")
        if any(not isinstance(item, UploadFile) for item in attachments):
            raise HTTPException(422, "invalid_conversation_files")
        if sum(item.size or 0 for item in attachments) > 20 * 1024 * 1024:
            raise HTTPException(413, "conversation_files_too_large")
        try:
            return ChatInput.model_validate_json(raw)
        except ValidationError:
            raise HTTPException(422, "invalid_conversation_input") from None


@router.post("", response_model=ChatAnswer, summary="提交问题并原样回显内容")
async def ask_question(
    request: Request,
    data: Annotated[ChatInput, Depends(chat_input)],
    session: SessionDependency,
) -> ChatAnswer:
    """表单解析完成后在线程池执行同步数据库操作，避免阻塞事件循环。"""
    return await run_in_threadpool(request_service(request, session).send, data)


@router.post("/new", response_model=ConversationRecord, status_code=201, summary="新建空对话")
def new_conversation(
    data: NewConversationInput, request: Request, session: SessionDependency
) -> ConversationRecord:
    """JSON 请求可传空对象，返回持久化的空会话及后续追加消息使用的 id。"""
    return request_service(request, session).new(data)


@router.get("/{identifier}/messages", response_model=ConversationRecord, summary="获取完整对话内容")
def conversation_messages(
    identifier: str, request: Request, session: SessionDependency
) -> ConversationRecord:
    """返回按时间排列的用户和助手消息，以及会话配置、版本和更新时间。"""
    return request_service(request, session).get(identifier)


@router.post(
    "/{identifier}/messages", response_model=ConversationSendResult, summary="向已有对话追加问题"
)
def append_message(
    identifier: str,
    data: ChatInput,
    request: Request,
    session: SessionDependency,
) -> ConversationSendResult:
    """追加问题并返回已保存会话供立即展示；当前默认回显，冲突返回 409，回复失败返回 502。"""
    return request_service(request, session).send(data, identifier)


@router.get("", response_model=list[ConversationSummary], summary="查询演示历史列表")
def list_history(request: Request, session: SessionDependency) -> list[ConversationSummary]:
    """返回当前用户的新会话及旧共享样例，新增记录按时间倒序排列。"""
    return request_service(request, session).list()


@router.get("/{identifier}", response_model=ChatAnswer, summary="打开历史对话")
def get_history(identifier: str, request: Request, session: SessionDependency) -> ChatAnswer:
    """identifier 来自历史列表，不存在或已删除时返回 404。"""
    instance = request_service(request, session)
    return instance.latest_answer(instance.get(identifier))


@router.delete("/{identifier}", status_code=204, summary="删除开发历史对话")
def delete_history(identifier: str, request: Request, session: SessionDependency) -> Response:
    """删除指定开发记录，成功返回空响应供前端刷新历史列表。"""
    request_service(request, session).delete(identifier)
    return Response(status_code=204)
