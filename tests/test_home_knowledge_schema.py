"""在可回滚的独立 schema 验证问答和知识文档完整链路及失败边界。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from psycopg.types.json import Jsonb
from test_resource_foundation import (
    MODULES,
    flush_constraints,
    insert,
    published,
    ready_file,
    rejected,
    resource,
    update,
    user,
)
from test_resource_foundation import connection as connection
from test_resource_foundation import pytestmark as pytestmark
from test_resource_foundation import schema_connection as schema_connection
from test_resource_support import get_row, remove

KNOWLEDGE = MODULES[3]


def conversation(connection: sa.Connection, owner: UUID) -> UUID:
    """创建当前用例专用会话。"""
    return insert(connection, "conversations", user_id=owner)


def question(connection: sa.Connection, conversation_id: UUID, **overrides) -> UUID:
    """创建有发送幂等键的用户消息，附件可在本事务稍后关联。"""
    return insert(
        connection,
        "conversation_messages",
        **(
            {
                "conversation_id": conversation_id,
                "sequence_no": 1,
                "role": "user",
                "content": "请说明设计流程",
                "status": "completed",
                "idempotency_key": uuid4(),
                "context_snapshot": Jsonb({"capabilities": []}),
            }
            | overrides
        ),
    )


def answer(connection: sa.Connection, question_id: UUID, **overrides) -> UUID:
    """为用户消息建立唯一的助手占位。"""
    row = get_row(connection, "conversation_messages", question_id)
    return insert(
        connection,
        "conversation_messages",
        **(
            {
                "conversation_id": row["conversation_id"],
                "sequence_no": row["sequence_no"] + 1,
                "role": "assistant",
                "reply_to_id": question_id,
                "status": "pending",
                "context_snapshot": Jsonb(row["context_snapshot"]),
            }
            | overrides
        ),
    )


def attach(connection: sa.Connection, message_id: UUID, file_id: UUID, **overrides) -> UUID:
    """按展示位置关联消息文件。"""
    return insert(
        connection,
        "conversation_message_attachments",
        **(
            {
                "message_id": message_id,
                "file_id": file_id,
                "position": 1,
            }
            | overrides
        ),
    )


def knowledge_base(connection: sa.Connection, owner: UUID) -> UUID:
    """使用既有发布流程生成已经审批的知识库，不建立本地索引配置。"""
    base_id, _, _, _ = published(connection, KNOWLEDGE, owner)
    return base_id


def document(connection: sa.Connection, owner: UUID, base_id: UUID, **overrides) -> UUID:
    """先创建已就绪文件，再登记待审文档。"""
    return insert(
        connection,
        "knowledge_documents",
        **(
            {
                "knowledge_base_id": base_id,
                "uploader_id": owner,
                "file_id": ready_file(connection, owner),
                "name": f"规范-{uuid4().hex}.txt",
                "submitted_at": datetime.now(UTC),
            }
            | overrides
        ),
    )


def review_document(
    connection: sa.Connection,
    document_id: UUID,
    reviewer: UUID,
    decision: str = "approved",
) -> UUID:
    """审核结论与当前文档状态在同一事务内提交。"""
    row = get_row(connection, "knowledge_documents", document_id)
    review_id = insert(
        connection,
        "knowledge_document_reviews",
        document_id=document_id,
        submission_no=row["submission_no"],
        submitted_at=row["submitted_at"],
        reviewer_id=reviewer,
        decision=decision,
        comment="请补充说明" if decision != "approved" else None,
        submission_snapshot=Jsonb(
            {
                "documentId": str(document_id),
                "fileId": str(row["file_id"]),
                "name": row["name"],
            }
        ),
    )
    update(
        connection,
        "knowledge_documents",
        document_id,
        revision=row["revision"] + 1,
        review_status=decision,
    )
    flush_constraints(connection)
    return review_id


def test_conversation_round_trip_and_immutable_history(connection):
    """一轮提问成功后可开始下一轮；终态、归属和旧上下文均不能被覆盖。"""
    owner = user(connection)
    session = conversation(connection, owner)
    message = question(connection, session)
    reply = answer(connection, message)
    update(connection, "conversation_messages", reply, status="completed", content="回答内容")
    flush_constraints(connection)
    next_question = question(connection, session, sequence_no=3)
    answer(connection, next_question)
    with rejected(connection):
        update(connection, "conversation_messages", reply, content="覆盖历史")
    with rejected(connection):
        update(connection, "conversation_messages", message, content="修改已发送问题")
    with rejected(connection):
        update(connection, "conversations", session, user_id=user(connection))


def test_message_idempotency_and_active_round(connection):
    """发送幂等、消息序号和生成占位分别唯一；unknown 也占用当前生成轮次。"""
    session = conversation(connection, user(connection))
    send_key = uuid4()
    message = question(connection, session, idempotency_key=send_key)
    reply = answer(connection, message)
    with rejected(connection):
        question(connection, session, sequence_no=3, idempotency_key=send_key)
    with rejected(connection):
        answer(connection, message, sequence_no=3)
    update(connection, "conversation_messages", reply, status="unknown")
    with rejected(connection):
        update(connection, "conversation_messages", reply, status="pending")
    with rejected(connection):
        second = question(connection, session, sequence_no=3)
        answer(connection, second)
    update(connection, "conversation_messages", reply, status="cancelled")
    second = question(connection, session, sequence_no=3)
    answer(connection, second)
    flush_constraints(connection)


def test_answer_cannot_cross_users_or_reply_to_assistant(connection):
    """不得引用另一用户的提问，也不得把助手消息当成用户提问。"""
    first = question(connection, conversation(connection, user(connection)))
    reply = answer(connection, first)
    other = conversation(connection, user(connection))
    with rejected(connection):
        answer(connection, first, conversation_id=other)
    with rejected(connection):
        answer(connection, reply, sequence_no=3)


def test_deleted_conversation_rejects_late_results(connection):
    """删除历史后，迟到的流式完成或新增请求不能恢复会话。"""
    session = conversation(connection, user(connection))
    reply = answer(connection, question(connection, session))
    update(connection, "conversations", session, deleted_at=datetime.now(UTC))
    with rejected(connection):
        update(connection, "conversation_messages", reply, status="completed", content="迟到回答")
    with rejected(connection):
        question(connection, session, sequence_no=3)
    with rejected(connection):
        update(connection, "conversations", session, deleted_at=None)


def test_attachment_only_question_and_file_protection(connection):
    """纯附件提问可以发送，引用清除临时期限，并阻止原文件清理和改作预览。"""
    owner = user(connection)
    session = conversation(connection, owner)
    file_id = ready_file(connection, owner, expires_at=datetime.now(UTC) + timedelta(hours=1))
    message = question(connection, session, content="")
    relation = attach(connection, message, file_id)
    answer(connection, message)
    flush_constraints(connection)
    assert get_row(connection, "files", file_id)["expires_at"] is None
    for status in ("deleting", "deleted"):
        with rejected(connection):
            update(connection, "files", file_id, status=status)
    with rejected(connection):
        remove(connection, "conversation_message_attachments", relation)
    with rejected(connection):
        insert(
            connection,
            "file_previews",
            source_file_id=ready_file(connection, owner),
            preview_file_id=file_id,
        )


def test_question_requires_text_or_ready_owned_file(connection):
    """空提问、跨用户附件和上传未完成附件均被拒绝。"""
    owner = user(connection)
    session = conversation(connection, owner)
    with rejected(connection):
        question(connection, session, content=" ")
    message = question(connection, session)
    with rejected(connection):
        attach(connection, message, ready_file(connection, user(connection)))
    unready = insert(
        connection,
        "files",
        uploader_id=owner,
        file_name="pending.txt",
        bucket="test",
        object_key=uuid4().hex,
    )
    with rejected(connection):
        attach(connection, message, unready)
    answer(connection, message)
    with rejected(connection):
        attach(connection, message, ready_file(connection, owner))


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"capabilities": "web"},
        {"capabilities": ["web", "web"]},
        {"capabilities": [], "requestedModelId": "bad"},
    ],
)
def test_context_snapshot_validation(connection, value):
    """请求上下文限定明确结构，不能塞入任意对象或伪造资源编号。"""
    with rejected(connection):
        question(
            connection, conversation(connection, user(connection)), context_snapshot=Jsonb(value)
        )


@pytest.mark.parametrize(
    "value", [{}, [{"title": "来源", "score": 2}], [{"title": "来源", "url": "temporary"}], [{}]]
)
def test_sources_validation(connection, value):
    """引用限定数组、规范分数及来源定位字段，不接收预签名链接。"""
    message = question(connection, conversation(connection, user(connection)))
    with rejected(connection):
        answer(connection, message, sources=Jsonb(value))


def test_home_recommendations_resolve_and_retain_resources(connection):
    """资源推荐保持真实引用，场景模块入口允许没有具体资源。"""
    owner = user(connection)
    item = resource(connection, MODULES[0], owner)
    values = {
        "placement": "featured_resource",
        "resource_type": "dataset",
        "resource_id": item,
        "creator_id": owner,
    }
    insert(connection, "home_recommendations", **values)
    with rejected(connection):
        insert(connection, "home_recommendations", **values)
    with rejected(connection):
        remove(connection, "datasets", item)
    with rejected(connection):
        insert(connection, "home_recommendations", **(values | {"resource_id": uuid4()}))
    with rejected(connection):
        insert(connection, "home_recommendations", **(values | {"resource_id": None}))
    insert(
        connection,
        "home_recommendations",
        creator_id=owner,
        placement="application_scenario",
        resource_type="application",
        title="设计优化",
        summary="设计类应用入口",
    )


def test_announcements_require_withdraw_before_edit(connection):
    """发布必须有正文和时间，已发布公告必须先撤回才能编辑。"""
    owner = user(connection)
    item = insert(connection, "announcements", creator_id=owner, title="维护公告")
    with rejected(connection):
        update(
            connection, "announcements", item, status="published", published_at=datetime.now(UTC)
        )
    update(
        connection,
        "announcements",
        item,
        content_markdown="维护内容",
        status="published",
        published_at=datetime.now(UTC),
    )
    with rejected(connection):
        update(connection, "announcements", item, content_markdown="直接修改")
    update(connection, "announcements", item, status="withdrawn")
    update(connection, "announcements", item, content_markdown="调整后内容")
    update(connection, "announcements", item, status="published", published_at=datetime.now(UTC))


def test_document_approval_without_local_indexing(connection):
    """上传和逐篇审核保留，审批成功无需任何本地索引任务或分段记录。"""
    owner = user(connection)
    base = knowledge_base(connection, owner)
    doc = document(connection, owner, base)
    review_id = review_document(connection, doc, owner)
    flush_constraints(connection)
    assert get_row(connection, "knowledge_documents", doc)["review_status"] == "approved"
    assert get_row(connection, "knowledge_document_reviews", review_id)["decision"] == "approved"


def test_document_reviews_are_atomic_and_immutable(connection):
    """不能跳过审核记录更新状态，也不能覆盖历次退回和重提记录。"""
    owner = user(connection)
    base = knowledge_base(connection, owner)
    doc = document(connection, owner, base)
    with rejected(connection):
        update(connection, "knowledge_documents", doc, revision=2, review_status="approved")
    review_id = review_document(connection, doc, owner, "needs_changes")
    with rejected(connection):
        update(connection, "knowledge_document_reviews", review_id, comment="覆盖意见")
    update(
        connection,
        "knowledge_documents",
        doc,
        revision=3,
        review_status="pending",
        submission_no=2,
        submitted_at=datetime.now(UTC),
    )
    review_document(connection, doc, owner)
    assert get_row(connection, "knowledge_documents", doc)["submission_no"] == 2
    assert (
        get_row(connection, "knowledge_document_reviews", review_id)["decision"] == "needs_changes"
    )


def test_document_file_name_and_retention_rules(connection):
    """活动文档同库不重名，原文件与审核历史不会被替换或误清理。"""
    owner = user(connection)
    base = knowledge_base(connection, owner)
    doc = document(connection, owner, base, name="Spec.txt")
    file_id = get_row(connection, "knowledge_documents", doc)["file_id"]
    with rejected(connection):
        document(connection, owner, base, name="SPEC.txt")
    with rejected(connection):
        update(
            connection,
            "knowledge_documents",
            doc,
            revision=2,
            file_id=ready_file(connection, owner),
        )
    with rejected(connection):
        update(connection, "files", file_id, status="deleting")
    update(connection, "knowledge_documents", doc, revision=2, deleted_at=datetime.now(UTC))
    document(connection, owner, base, name="Spec.txt")
    with rejected(connection):
        remove(connection, "knowledge_documents", doc)
    with rejected(connection):
        update(connection, "files", file_id, expires_at=datetime.now(UTC))


def test_unpublished_base_cannot_approve_documents(connection):
    """知识库资源尚未审核发布时，文档不能提前审批通过。"""
    owner = user(connection)
    base = resource(connection, KNOWLEDGE, owner)
    doc = document(connection, owner, base)
    with rejected(connection):
        review_document(connection, doc, owner)


def test_preview_mapping_is_retained_for_knowledge_file(connection):
    """知识库引用原文件后，已有预览关系也必须保留。"""
    owner = user(connection)
    base = knowledge_base(connection, owner)
    source = ready_file(connection, owner)
    preview = ready_file(connection, owner)
    relation = insert(connection, "file_previews", source_file_id=source, preview_file_id=preview)
    document(connection, owner, base, file_id=source)
    with rejected(connection):
        remove(connection, "file_previews", relation)
    with rejected(connection):
        document(connection, owner, base, file_id=preview)


def test_local_indexing_schema_is_removed(connection):
    """最终结构不再保留本地索引表、文档运行字段或它们的触发函数。"""
    inspector = sa.inspect(connection)
    assert not {
        "knowledge_index_states",
        "knowledge_index_jobs",
        "knowledge_document_chunks",
    }.intersection(inspector.get_table_names())
    columns = {item["name"] for item in inspector.get_columns("knowledge_documents")}
    assert not {"index_job_id", "processing_status", "processed_at", "error_message"}.intersection(
        columns
    )
    functions = set(
        connection.execute(
            sa.text(
                "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname=current_schema()"
            )
        ).scalars()
    )
    assert not {
        "aih_guard_knowledge_chunk",
        "aih_guard_knowledge_index_job",
        "aih_guard_knowledge_index_state",
        "aih_check_knowledge_document_result",
        "aih_valid_document_ids",
        "aih_valid_embedding",
        "aih_index_configuration",
    }.intersection(functions)
