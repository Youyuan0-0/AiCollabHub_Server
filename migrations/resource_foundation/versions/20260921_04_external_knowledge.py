"""知识库索引交由外部接口处理，移除本地索引三表及文档运行字段。"""

from pathlib import Path

from alembic import op

revision = "20260921_04_knowledge_external"
down_revision = "20260921_03_home_knowledge"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """仅撤销未使用的本地索引结构，保留文档、审核及原文件引用。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    # 检查和删表使用同一事务锁，防止检查后有工作进程写入待删除的数据。
    op.execute(
        "LOCK TABLE knowledge_index_states, knowledge_index_jobs, "
        "knowledge_document_chunks, knowledge_documents IN ACCESS EXCLUSIVE MODE"
    )
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM knowledge_index_states)
               OR EXISTS (SELECT 1 FROM knowledge_index_jobs)
               OR EXISTS (SELECT 1 FROM knowledge_document_chunks)
               OR EXISTS (
                   SELECT 1 FROM knowledge_documents
                   WHERE index_job_id IS NOT NULL OR processed_at IS NOT NULL
                      OR error_message IS NOT NULL
                      OR processing_status NOT IN ('awaiting_review','pending')
               ) THEN
                RAISE EXCEPTION 'Local knowledge indexing has data; archive it before removal'
                    USING ERRCODE='23514';
            END IF;
        END $$;
    """)
    op.execute("DROP TRIGGER check_document_result ON knowledge_documents")
    op.drop_constraint("fk_knowledge_documents_job_base", "knowledge_documents", type_="foreignkey")
    for suffix in ("job_required", "review_processing", "processing_status"):
        op.drop_constraint(f"ck_knowledge_documents_{suffix}", "knowledge_documents", type_="check")
    for name in ("index_job_id", "processing_status", "processed_at", "error_message"):
        op.drop_column("knowledge_documents", name)
    # 已执行迁移保持原样，新版本替换文档约束，不让旧触发器引用已撤回的字段。
    op.execute(
        Path(__file__).with_name("20260921_04_document_guards.sql").read_text(encoding="utf-8")
    )
    for table in ("knowledge_document_chunks", "knowledge_index_jobs", "knowledge_index_states"):
        op.drop_table(table)
    for signature in (
        "aih_guard_knowledge_chunk()",
        "aih_guard_knowledge_index_job()",
        "aih_guard_knowledge_index_state()",
        "aih_check_knowledge_document_result()",
        "aih_valid_document_ids(uuid[])",
        "aih_valid_embedding(jsonb)",
        "aih_index_configuration(jsonb)",
    ):
        op.execute(f"DROP FUNCTION {signature}")
    op.execute(
        "COMMENT ON TABLE knowledge_documents IS "
        "'知识库文档与当前审核状态；索引、分段和检索由外部知识库接口负责'"
    )


def downgrade() -> None:
    """职责收缩不自动恢复本地索引体系，回退需使用迁移前备份。"""
    raise RuntimeError("Restore the pre-migration backup to recover local knowledge indexing")
