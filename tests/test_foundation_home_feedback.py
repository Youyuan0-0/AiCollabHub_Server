"""问答及反馈针对新基线的隔离数据库业务回归。"""

from io import BytesIO
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from app.modules.feedback.database import FeedbackService
from app.modules.feedback.schemas import FeedbackInput, FeedbackUpdate
from app.schemas.conversations import ChatInput, NewConversationInput
from app.services.chat_provider import EchoChatProvider
from app.services.conversation_database import ConversationDatabase
from app.services.conversation_files import ConversationFiles
from app.services.database_chat import DatabaseChatService
from app.storage.files import FileStore
from tests.test_foundation_resource_storage import foundation
from tests.test_resource_foundation import (
    connection,
    flush_constraints,
    pytestmark,
    schema_connection,
)

__all__ = ["foundation", "connection", "schema_connection", "pytestmark"]


def test_chat_attachment_binding_survives_repository_restart(foundation, connection):
    """原件、消息关联和历史展示一致，附件不能跨会话绑定或随软删除丢失。"""
    from tests.test_foundation_module_flows import Storage

    store = FileStore(foundation, Storage())
    files = ConversationFiles(store)
    repository = ConversationDatabase(foundation, "writer")
    service = DatabaseChatService(repository, EchoChatProvider())
    first = service.new(NewConversationInput())
    second = service.new(NewConversationInput())
    attachment = files.save(first.id, "question.txt", BytesIO(b"question attachment"))
    payload = ChatInput(
        question="查看附件", model="Mock", knowledge_base="", attachment_ids=[attachment.id]
    )
    with pytest.raises(HTTPException, match="404"):
        service.send(payload, second.id)
    assert repository.get(second.id).messages == []
    service.send(payload, first.id)
    restored = ConversationDatabase(foundation, "writer").get(first.id)
    assert restored.messages[0].attachments == [attachment]
    assert store.get(attachment.id)["expires_at"] is None
    assert b"".join(files.chunks(first.id, attachment.id)) == b"question attachment"
    service.delete(first.id)
    with pytest.raises(HTTPException, match="404"):
        files.get(first.id, attachment.id)
    assert store.get(attachment.id)["status"] == "ready"
    flush_constraints(connection)


def test_chat_restart_idempotency_and_ownership(foundation, connection):
    repository = ConversationDatabase(foundation, "writer")
    service = DatabaseChatService(repository, EchoChatProvider())
    record = service.new(NewConversationInput(model="Mock模型"))
    data = ChatInput(
        question="测试问题", model="Mock模型", knowledge_base="", idempotency_key=uuid4()
    )
    result = service.send(data, record.id)
    assert len(result.conversation.messages) == 2
    assert service.send(data, record.id).conversation.messages == result.conversation.messages
    service.send(
        data.model_copy(update={"question": "第二个问题", "idempotency_key": uuid4()}), record.id
    )
    replay = service.send(data, record.id)
    assert replay.question == result.question and replay.text == result.text
    assert len(replay.conversation.messages) == 4
    assert len(ConversationDatabase(foundation, "writer").get(record.id).messages) == 4
    with pytest.raises(HTTPException, match="404"):
        ConversationDatabase(foundation, "reviewer").get(record.id)
    service.delete(record.id)
    assert repository.list() == []
    flush_constraints(connection)


def test_interrupted_chat_recovers_without_overwriting_late_result(
    foundation, connection, monkeypatch
):
    """服务中断遗留的回答可恢复为失败，不接受超时后的迟到结果。"""
    from datetime import timedelta

    from app.db.foundation import utc_now

    repository = ConversationDatabase(foundation, "writer")
    record = repository.create(NewConversationInput())
    _, answer_id, _ = repository.begin(
        record.id, ChatInput(question="等待超时", model="Mock", knowledge_base="")
    )
    future = utc_now() + timedelta(minutes=16)
    monkeypatch.setattr("app.services.conversation_database.utc_now", lambda: future)
    assert repository.get(record.id).messages[-1].status == "failed"
    with pytest.raises(HTTPException, match="409"):
        repository.finish(record.id, answer_id, None)
    repository.begin(record.id, ChatInput(question="重新提问", model="Mock", knowledge_base=""))
    flush_constraints(connection)


def test_chat_failure_and_late_answer(foundation, connection):
    class FailedProvider:
        def reply(self, *args, **kwargs):
            raise RuntimeError("provider failure")

    repository = ConversationDatabase(foundation, "writer")
    service = DatabaseChatService(repository, FailedProvider())
    record = service.new(NewConversationInput())
    data = ChatInput(question="失败场景", model="Mock模型", knowledge_base="")
    with pytest.raises(HTTPException, match="502"):
        service.send(data, record.id)
    assert repository.get(record.id).messages[-1].status == "failed"
    _, answer_id, _ = repository.begin(record.id, data)
    repository.delete(record.id)
    with pytest.raises(HTTPException, match="404"):
        repository.finish(record.id, answer_id, None)
    flush_constraints(connection)


def test_feedback_idempotency_revision_and_events(foundation, connection):
    files = FileStore(foundation, None)
    user = FeedbackService(foundation, files, "writer")
    admin = FeedbackService(foundation, files, "reviewer", is_admin=True)
    payload = FeedbackInput(
        type="function_error",
        module="home",
        description="测试问题",
        contact="测试联系人",
        client_request_id=uuid4(),
    )
    record, created = user.submit(payload)
    assert created
    assert not user.submit(payload)[1]
    command = FeedbackUpdate(
        status="resolved", reply="已处理", expected_version=1, client_request_id=uuid4()
    )
    changed = admin.process(record["id"], command)
    assert changed["version"] == 2 and changed["status"] == "resolved"
    assert admin.process(record["id"], command)["version"] == 2
    with pytest.raises(HTTPException, match="409"):
        admin.process(record["id"], command.model_copy(update={"client_request_id": uuid4()}))
    assert connection.scalar(sa.text("SELECT count(*) FROM feedback_events")) == 2
    flush_constraints(connection)
