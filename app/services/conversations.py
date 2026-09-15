"""会话业务编排：管理完整消息历史，通过可替换存储和回复提供器执行对话。"""

import json
from datetime import UTC, datetime
from importlib.resources import files
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.schemas.conversations import (
    ChatAnswer,
    ChatInput,
    ChatReply,
    ConversationMessage,
    ConversationRecord,
    ConversationSendResult,
    ConversationSummary,
    NewConversationInput,
)
from app.services.attachment_store import MAX_MESSAGE_ATTACHMENT_BYTES, AttachmentStore
from app.services.chat_provider import ChatProvider, EchoChatProvider
from app.services.conversation_store import (
    ConversationStore,
    SqlConversationStore,
    record_from_answer,
)


def demo_data() -> dict:
    """读取后端固定内容；读取失败返回明确错误，不生成貌似真实的模型答案。"""
    try:
        return json.loads(
            files("app.data").joinpath("conversations_demo.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        raise HTTPException(503, "conversations_demo_invalid") from None


def fixed_answer(
    data: dict, identifier: str, question: str, model: str, knowledge_base: str
) -> ChatAnswer:
    """将用户问题代入固定模板，保留前端选择项以兼容回答展示。"""
    return ChatAnswer(
        id=identifier,
        question=question,
        model=model,
        knowledge_base=knowledge_base,
        text=data["answerTemplate"].replace("{question}", question),
        sources=data["sources"],
    )


def fixed_history() -> dict[str, ChatAnswer]:
    """固定历史只作为缺省内容，不通过 GET 自动写入数据库。"""
    data = demo_data()
    return {
        item["id"]: fixed_answer(
            data, item["id"], item["question"], item["model"], item["knowledgeBase"]
        )
        for item in data["history"]
    }


class ConversationService:
    """存储和回复提供器由外部传入，第三方协议与数据库细节不进入路由。"""

    def __init__(
        self,
        store: ConversationStore,
        provider: ChatProvider,
        attachments: AttachmentStore | None = None,
        owner_id: str = "",
    ):
        # owner_id 由已验证身份提供；附件仓库与会话存储分别注入，便于独立迁移。
        self.store = store
        self.provider = provider
        self.attachments = attachments
        self.owner_id = owner_id

    def get(self, identifier: str) -> ConversationRecord:
        """读取完整会话；旧固定样例只在没有实际记录或删除标记时补充。"""
        return self._load(identifier)[0]

    def _load(self, identifier: str) -> tuple[ConversationRecord, int | None]:
        """返回会话和持久化版本；固定样例版本为 None，真实会话无需再查询是否存在。"""
        record = self.store.get(identifier)
        if record is not None:
            if record.owner_id and record.owner_id != self.owner_id:
                raise HTTPException(404, "conversation_not_found")
            return record, record.version
        if not self.store.contains(identifier) and identifier in (samples := fixed_history()):
            return record_from_answer(samples[identifier], datetime(2026, 9, 14, tzinfo=UTC)), None
        raise HTTPException(404, "conversation_not_found")

    def new(self, data: NewConversationInput) -> ConversationRecord:
        """立即保存空对话；用户未发送问题也可以在历史中看到或删除。"""
        now = datetime.now(UTC)
        record = ConversationRecord(
            id=f"chat-{uuid4().hex}",
            title=data.title,
            model=data.model,
            knowledge_base=data.knowledge_base,
            tools=data.tools,
            created_at=now,
            updated_at=now,
            owner_id=self.owner_id,
        )
        self.store.save(record, expected_version=None)
        return record

    def send(self, data: ChatInput, identifier: str | None = None) -> ConversationSendResult:
        """追加并保存完整一轮后返回会话快照；identifier 为空时新建，失败或冲突不返回成功结果。"""
        if identifier is None:
            now = datetime.now(UTC)
            record = ConversationRecord(
                id=f"chat-{uuid4().hex}",
                title=data.question[:200],
                model=data.model,
                knowledge_base=data.knowledge_base,
                tools=data.tools,
                created_at=now,
                updated_at=now,
                owner_id=self.owner_id,
            )
            expected = None
        else:
            record, expected = self._load(identifier)
        if len(record.messages) >= 200:
            raise HTTPException(409, "conversation_message_limit")
        attachments = []
        if data.attachment_ids:
            if self.attachments is None:
                raise HTTPException(503, "attachment_storage_unavailable")
            if len(set(data.attachment_ids)) != len(data.attachment_ids):
                raise HTTPException(422, "duplicate_attachment_ids")
            # 附件先归属于会话，再随成功的一轮写入消息；失败重试可以复用已上传编号。
            attachments = [self.attachments.get(record.id, item) for item in data.attachment_ids]
            if sum(item.size for item in attachments) > MAX_MESSAGE_ATTACHMENT_BYTES:
                raise HTTPException(413, "conversation_files_too_large")
        user_message = ConversationMessage(
            id=f"message-{uuid4().hex}",
            role="user",
            content=data.question,
            created_at=datetime.now(UTC),
            model=data.model,
            knowledge_base=data.knowledge_base,
            tools=list(data.tools),
            attachments=attachments,
        )
        # 传入独立副本，供应方适配器不能修改已保存历史；密钥与供应方地址不接受前端传入。
        context = [message.model_copy(deep=True) for message in record.messages] + [
            user_message.model_copy(deep=True)
        ]
        try:
            reply = ChatReply.model_validate(
                self.provider.reply(
                    context,
                    model=data.model,
                    knowledge_base=data.knowledge_base,
                    tools=list(data.tools),
                )
            )
        except Exception:
            # 供应方错误可能包含密钥或内部 URL，对外统一错误码，不输出异常正文。
            raise HTTPException(502, "chat_provider_unavailable") from None
        if not record.messages:
            record.title = data.question[:200]
        now = datetime.now(UTC)
        record.messages.extend(
            [
                user_message,
                ConversationMessage(
                    id=f"message-{uuid4().hex}",
                    role="assistant",
                    content=reply.text,
                    created_at=now,
                    model=data.model,
                    knowledge_base=data.knowledge_base,
                    tools=list(data.tools),
                    sources=reply.sources,
                ),
            ]
        )
        record.model = data.model
        record.knowledge_base = data.knowledge_base
        record.tools = list(data.tools)
        record.updated_at = now
        record.version += 1
        self.store.save(record, expected_version=expected)
        # 复用已提交的内存记录，避免前端为展示回复额外往返查询；保留旧摘要字段兼容旧客户端。
        return ConversationSendResult(
            **self.latest_answer(record).model_dump(), conversation=record
        )

    @staticmethod
    def latest_answer(record: ConversationRecord) -> ChatAnswer:
        """旧前端只消费最近一轮；空会话返回空文本，完整消息由专用接口提供。"""
        question = next(
            (item.content for item in reversed(record.messages) if item.role == "user"), ""
        )
        reply = next((item for item in reversed(record.messages) if item.role == "assistant"), None)
        return ChatAnswer(
            id=record.id,
            question=question,
            model=record.model,
            knowledge_base=record.knowledge_base,
            text=reply.content if reply else "",
            sources=reply.sources if reply else [],
        )

    def list(self) -> list[ConversationSummary]:
        """数据库先按可信用户过滤并返回最近 100 条摘要，再补充尚未删除的固定样例。"""
        summaries = self.store.list(self.owner_id)
        summaries.extend(
            ConversationSummary(
                id=sample.id, title=sample.question, model=sample.model, time="固定演示历史"
            )
            for sample in fixed_history().values()
            if not self.store.contains(sample.id)
        )
        return summaries[:100]

    def delete(self, identifier: str) -> None:
        """删除会话及全部消息；先确认存在，未知或已删除记录返回 404。"""
        self.get(identifier)
        self.store.delete(identifier)


def service(
    session: Session,
    provider: ChatProvider | None = None,
    attachments: AttachmentStore | None = None,
    owner_id: str = "",
) -> ConversationService:
    """默认使用关系型会话/消息存储和回显回复，第三方提供器仍可独立替换。"""
    return ConversationService(
        SqlConversationStore(session),
        provider if provider is not None else EchoChatProvider(),
        attachments,
        owner_id,
    )


def create_conversation(
    session: Session, data: ChatInput, provider: ChatProvider | None = None
) -> ChatAnswer:
    """兼容现有前端：一次提交同时新建会话并保存第一轮回显消息。"""
    return service(session, provider).send(data)


def list_conversations(session: Session) -> list[ConversationSummary]:
    """兼容原历史列表响应字段。"""
    return service(session).list()


def get_conversation(session: Session, identifier: str) -> ChatAnswer:
    """兼容原详情响应，返回指定会话最近一轮问答。"""
    instance = service(session)
    return instance.latest_answer(instance.get(identifier))


def delete_conversation(session: Session, identifier: str) -> None:
    """复用统一删除逻辑，清除完整会话内容。"""
    service(session).delete(identifier)
