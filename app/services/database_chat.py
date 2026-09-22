"""持久化问答服务，平台历史真实保存，模型调用仍支持 Mock 提供方。"""

import logging

from fastapi import HTTPException

from app.schemas.conversations import (
    ChatInput,
    ChatReply,
    ConversationRecord,
    ConversationSendResult,
    ConversationSummary,
    NewConversationInput,
)
from app.services.chat_provider import ChatProvider
from app.services.conversation_database import ConversationDatabase
from app.services.conversations import ConversationService

logger = logging.getLogger(__name__)


class DatabaseChatService:
    """用已确认的会话、消息及附件表管理问答生命周期。"""

    latest_answer = staticmethod(ConversationService.latest_answer)

    def __init__(self, repository: ConversationDatabase, provider: ChatProvider) -> None:
        self.repository = repository
        self.provider = provider

    def get(self, identifier: str) -> ConversationRecord:
        """读取属于当前登录用户的完整历史。"""
        return self.repository.get(identifier)

    def list(self) -> list[ConversationSummary]:
        """返回数据库历史摘要。"""
        return self.repository.list()

    def new(self, data: NewConversationInput) -> ConversationRecord:
        """新建持久会话，刷新或切换进程后仍可读取。"""
        return self.repository.create(data)

    def delete(self, identifier: str) -> None:
        """软删除并阻止未完成调用覆盖删除状态。"""
        self.repository.delete(identifier)

    def send(self, data: ChatInput, identifier: str | None = None) -> ConversationSendResult:
        """提问先落库；供应方调用不占数据库事务，失败状态同样保存。"""
        if identifier is None:
            identifier = self.new(
                NewConversationInput(
                    title=data.question[:200],
                    model=data.model,
                    knowledge_base=data.knowledge_base,
                    tools=data.tools,
                )
            ).id
        record, answer_id, should_call = self.repository.begin(identifier, data)
        if should_call:
            try:
                reply = ChatReply.model_validate(
                    self.provider.reply(
                        [
                            message.model_copy(deep=True)
                            for message in record.messages
                            if message.status == "completed"
                        ],
                        model=data.model,
                        knowledge_base=data.knowledge_base,
                        tools=data.tools,
                    )
                )
            except Exception as error:
                logger.warning(
                    "Chat provider failed (%s)",
                    type(error).__name__,
                    extra={
                        "source": "conversations",
                        "event": "chat_provider_failed",
                    },
                )
                self.repository.finish(identifier, answer_id, None)
                raise HTTPException(502, "chat_provider_unavailable") from None
            record = self.repository.finish(identifier, answer_id, reply)
        answer_index = next(
            index for index, item in enumerate(record.messages) if item.id == answer_id
        )
        answer = record.messages[answer_index]
        return ConversationSendResult(
            id=record.id,
            question=record.messages[answer_index - 1].content,
            model=answer.model,
            knowledge_base=answer.knowledge_base,
            text=answer.content,
            sources=answer.sources,
            conversation=record,
        )
