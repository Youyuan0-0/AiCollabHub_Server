"""开发问答与共享历史接口；挂载时统一检查登录，生产环境不开放演示会话。"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.db.session import SessionDependency
from app.schemas.conversations import (
    ChatAnswer,
    ChatInput,
    ConversationRecord,
    ConversationSummary,
    NewConversationInput,
)
from app.services import conversations


def require_demo_environment(request: Request) -> None:
    """只允许服务端非生产配置；不创建演示身份，不放宽其他业务的鉴权。"""
    if getattr(request.app.state, "app_env", "production") not in {"development", "test"}:
        raise HTTPException(503, "conversations_not_configured")


router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    dependencies=[Depends(require_demo_environment)],
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
    return await run_in_threadpool(
        conversations.create_conversation, session, data, request.app.state.chat_provider
    )


@router.post("/new", response_model=ConversationRecord, status_code=201, summary="新建空对话")
def new_conversation(data: NewConversationInput, session: SessionDependency) -> ConversationRecord:
    """JSON 请求可传空对象，返回持久化的空会话及后续追加消息使用的 id。"""
    return conversations.service(session).new(data)


@router.get("/{identifier}/messages", response_model=ConversationRecord, summary="获取完整对话内容")
def conversation_messages(identifier: str, session: SessionDependency) -> ConversationRecord:
    """返回按时间排列的用户和助手消息，以及会话配置、版本和更新时间。"""
    return conversations.service(session).get(identifier)


@router.post("/{identifier}/messages", response_model=ChatAnswer, summary="向已有对话追加问题")
def append_message(
    identifier: str,
    data: ChatInput,
    request: Request,
    session: SessionDependency,
) -> ChatAnswer:
    """JSON 追加问题并传递完整上下文，当前默认回显；冲突返回 409，回复失败返回 502。"""
    return conversations.service(session, request.app.state.chat_provider).send(data, identifier)


@router.get("", response_model=list[ConversationSummary], summary="查询演示历史列表")
def list_history(session: SessionDependency) -> list[ConversationSummary]:
    """返回共享开发历史与固定样例，新增记录按时间倒序排列。"""
    return conversations.list_conversations(session)


@router.get("/{identifier}", response_model=ChatAnswer, summary="打开历史对话")
def get_history(identifier: str, session: SessionDependency) -> ChatAnswer:
    """identifier 来自历史列表，不存在或已删除时返回 404。"""
    return conversations.get_conversation(session, identifier)


@router.delete("/{identifier}", status_code=204, summary="删除开发历史对话")
def delete_history(identifier: str, session: SessionDependency) -> Response:
    """删除指定开发记录，成功返回空响应供前端刷新历史列表。"""
    conversations.delete_conversation(session, identifier)
    return Response(status_code=204)
