"""补齐首页六表与知识库文档、审核、索引五表，不切换现有业务仓储。"""

from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "20260921_03_home_knowledge"
down_revision = "20260921_02_resource_support"
branch_labels = None
depends_on = None


def column(
    name: str,
    kind: sa.types.TypeEngine,
    comment: str,
    *,
    nullable: bool = False,
    default: str | sa.sql.ClauseElement | None = None,
) -> sa.Column:
    """冻结迁移字段和中文注释，避免依赖未来修改的业务模型。"""
    return sa.Column(name, kind, nullable=nullable, server_default=default, comment=comment)


def identifier() -> sa.Column:
    """允许应用传入 UUID，未传时由数据库生成。"""
    return column("id", UUID(), "平台生成的稳定 UUID", default=sa.text("gen_random_uuid()"))


def time_column(name: str, comment: str, *, nullable: bool = False) -> sa.Column:
    """仅创建和更新时间设置默认值，业务状态时间由调用方明确填写。"""
    return column(
        name,
        sa.DateTime(timezone=True),
        comment,
        nullable=nullable,
        default=sa.func.now() if name in {"created_at", "updated_at"} else None,
    )


def check(table: str, suffix: str, expression: str) -> sa.CheckConstraint:
    """显式命名约束，便于识别数据库拒绝的业务规则。"""
    return sa.CheckConstraint(expression, name=sa.schema.conv(f"ck_{table}_{suffix}"))


def fk(table: str, name: str, target: str) -> sa.ForeignKeyConstraint:
    """历史资料不随用户或资源物理删除而级联丢失。"""
    return sa.ForeignKeyConstraint(
        [name],
        [f"{target}.id"],
        name=sa.schema.conv(f"fk_{table}_{name}_{target}"),
        ondelete="NO ACTION",
    )


def create(table: str, *items: sa.schema.SchemaItem, comment: str) -> None:
    """为本次新增表统一设置 UUID 主键及表说明。"""
    op.create_table(
        table,
        identifier(),
        *items,
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        comment=comment,
    )


def create_conversations() -> None:
    """建立线性问答及附件关系，发送幂等与单轮生成在数据库内约束。"""
    table = "conversations"
    create(
        table,
        column("user_id", UUID(), "会话所属用户"),
        column("title", sa.String(200), "会话标题", default="新对话"),
        column("model_id", UUID(), "默认模型；为空时使用默认路由", nullable=True),
        column("knowledge_base_id", UUID(), "默认知识库；为空表示未选择", nullable=True),
        column("capabilities", JSONB(), "默认能力代码，去空去重字符串数组", default="[]"),
        time_column("created_at", "创建时间"),
        time_column("updated_at", "最近活动时间；不按流式分片更新"),
        time_column("deleted_at", "用户删除时间；删除后不能恢复或接收迟到回答", nullable=True),
        fk(table, "user_id", "users"),
        fk(table, "model_id", "models"),
        fk(table, "knowledge_base_id", "knowledge_bases"),
        check(table, "title", "length(trim(title))>0"),
        check(table, "capabilities", "aih_is_string_array(capabilities)"),
        comment="用户的线性多轮问答及下次提问默认选项，不保存登录状态",
    )
    op.create_index(
        "ix_conversations_user_activity",
        table,
        ["user_id", sa.text("updated_at DESC"), "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    table = "conversation_messages"
    create(
        table,
        column("conversation_id", UUID(), "所属会话"),
        column("sequence_no", sa.Integer(), "会话内从 1 递增的消息序号"),
        column("role", sa.String(16), "user 用户提问 / assistant 助手回答"),
        column("reply_to_id", UUID(), "助手对应的同会话用户提问", nullable=True),
        column("content", sa.Text(), "问题或回答正文；生成中可为空", default=""),
        column("status", sa.String(16), "pending/completed/failed/cancelled/unknown"),
        column("idempotency_key", UUID(), "用户发送幂等键；助手为空", nullable=True),
        column("context_snapshot", JSONB(), "本轮模型、知识库及能力快照，实际模型可后补"),
        column("sources", JSONB(), "最多 20 项引用来源，最多 64KiB；不保存临时链接", default="[]"),
        column("upstream_request_id", sa.String(200), "供应方实际返回的请求编号", nullable=True),
        column("error_code", sa.String(64), "归一化失败代码", nullable=True),
        column(
            "error_message", sa.String(500), "可向用户展示的失败原因，不保存堆栈", nullable=True
        ),
        time_column("created_at", "消息或回答占位创建时间"),
        time_column("updated_at", "内容与调用状态持久化时间"),
        fk(table, "conversation_id", "conversations"),
        sa.UniqueConstraint("id", "conversation_id", name=f"uq_{table}_id_conversation"),
        sa.UniqueConstraint("conversation_id", "sequence_no", name=f"uq_{table}_sequence"),
        sa.UniqueConstraint("conversation_id", "idempotency_key", name=f"uq_{table}_send_key"),
        sa.UniqueConstraint("reply_to_id", name=f"uq_{table}_reply_to_id"),
        sa.ForeignKeyConstraint(
            ["reply_to_id", "conversation_id"],
            [f"{table}.id", f"{table}.conversation_id"],
            name=f"fk_{table}_reply_conversation",
        ),
        check(table, "sequence_no", "sequence_no>=1"),
        check(table, "status", "status IN ('pending','completed','failed','cancelled','unknown')"),
        check(
            table,
            "role_fields",
            "(role='user' AND reply_to_id IS NULL AND idempotency_key IS NOT NULL "
            "AND status='completed' AND sources='[]'::jsonb) OR "
            "(role='assistant' AND reply_to_id IS NOT NULL AND idempotency_key IS NULL)",
        ),
        check(table, "context", "aih_valid_message_context(context_snapshot)"),
        check(table, "sources", "aih_valid_message_sources(sources)"),
        comment="每条用户提问及对应助手回答，保留调用状态与实际上下文快照",
    )
    op.create_index(
        "uq_conversation_messages_active_round",
        table,
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("role='assistant' AND status IN ('pending','unknown')"),
    )
    table = "conversation_message_attachments"
    create(
        table,
        column("message_id", UUID(), "所属用户消息"),
        column("file_id", UUID(), "已就绪原始文件"),
        column("position", sa.Integer(), "从 1 开始的附件展示位置"),
        time_column("created_at", "随消息保存附件关系的时间"),
        fk(table, "message_id", "conversation_messages"),
        fk(table, "file_id", "files"),
        sa.UniqueConstraint("message_id", "file_id", name=f"uq_{table}_message_file"),
        sa.UniqueConstraint("message_id", "position", name=f"uq_{table}_message_position"),
        check(table, "position", "position>=1"),
        comment="已发送提问与文件的稳定关联；文件属性及本体复用统一文件服务",
    )
    op.create_index("ix_conversation_message_attachments_file_id", table, ["file_id"])


def create_home_configuration() -> None:
    """首页配置独立于资源统计及个人站内消息，不添加推荐排序号。"""
    table = "home_recommendations"
    create(
        table,
        column(
            "placement", sa.String(32), "featured_resource 热门资源 / application_scenario 场景"
        ),
        column("resource_type", sa.String(16), "dataset/model/application/knowledge/tool"),
        column("resource_id", UUID(), "具体资源；场景模块入口可空", nullable=True),
        column("title", sa.String(200), "展示标题；模块入口必填", nullable=True),
        column("summary", sa.String(500), "展示简介；模块入口必填", nullable=True),
        column("status", sa.String(16), "enabled 启用 / disabled 禁用", default="disabled"),
        column("creator_id", UUID(), "创建推荐的管理员"),
        time_column("created_at", "推荐创建时间"),
        time_column("updated_at", "配置更新时间"),
        fk(table, "creator_id", "users"),
        sa.UniqueConstraint("placement", "resource_type", "resource_id", name=f"uq_{table}_target"),
        check(table, "placement", "placement IN ('featured_resource','application_scenario')"),
        check(
            table,
            "resource_type",
            "resource_type IN ('dataset','model','application','knowledge','tool')",
        ),
        check(table, "status", "status IN ('enabled','disabled')"),
        check(
            table,
            "target",
            "resource_id IS NOT NULL OR (placement='application_scenario' "
            "AND title IS NOT NULL AND length(trim(title))>0 "
            "AND summary IS NOT NULL AND length(trim(summary))>0)",
        ),
        comment="首页热门资源和典型场景入口配置，访问时仍须执行资源权限校验",
    )
    op.create_index("ix_home_recommendations_placement_status", table, ["placement", "status"])
    table = "home_question_suggestions"
    create(
        table,
        column("question", sa.String(1000), "点击后填入输入框的问题，不自动发送"),
        column("status", sa.String(16), "enabled 启用 / disabled 禁用", default="disabled"),
        column("creator_id", UUID(), "创建推荐问题的管理员"),
        time_column("created_at", "创建时间"),
        time_column("updated_at", "更新时间"),
        fk(table, "creator_id", "users"),
        check(table, "question", "length(trim(question))>0 AND question=trim(question)"),
        check(table, "status", "status IN ('enabled','disabled')"),
        comment="首页示例问题，沿用用户当前模型和知识库选项",
    )
    table = "announcements"
    create(
        table,
        column("title", sa.String(200), "公告标题"),
        column("summary", sa.String(500), "首页摘要；为空时由服务从正文提取", nullable=True),
        column("content_markdown", sa.Text(), "公告正文；草稿可空字符串", default=""),
        column(
            "status",
            sa.String(16),
            "draft 草稿 / published 已发布 / withdrawn 已撤回",
            default="draft",
        ),
        column("creator_id", UUID(), "创建公告的管理员"),
        time_column("published_at", "实际发布时间；撤回后保留", nullable=True),
        time_column("created_at", "创建时间"),
        time_column("updated_at", "更新时间"),
        fk(table, "creator_id", "users"),
        check(table, "title", "length(trim(title))>0"),
        check(table, "status", "status IN ('draft','published','withdrawn')"),
        check(
            table,
            "published",
            "status='draft' OR (published_at IS NOT NULL AND length(trim(content_markdown))>0)",
        ),
        comment="面向已登录用户的平台公告；不包含个人通知或已读回执",
    )
    op.create_index(
        "ix_announcements_published_at",
        table,
        [sa.text("published_at DESC"), "id"],
        postgresql_where=sa.text("status='published'"),
    )


def create_knowledge_index_states() -> None:
    """保存每库独立索引配置及运行统计，不污染资源概览或版本快照。"""
    table = "knowledge_index_states"
    create(
        table,
        column("knowledge_base_id", UUID(), "所属知识库；每库一条"),
        column("mode", sa.String(16), "economic 经济 / high_quality 高质量", default="economic"),
        column("embedding_model", sa.String(120), "嵌入模型代码；高质量模式必填", nullable=True),
        column(
            "chunk_mode",
            sa.String(16),
            "auto/paragraph/fixed/intelligent/semantic/rule/recursive",
            default="auto",
        ),
        column("chunk_size", sa.Integer(), "分段长度，单位字符，100 至 2000", default="500"),
        column("chunk_overlap", sa.Integer(), "分段重叠字符数，须小于分段长度", default="50"),
        column(
            "retrieval_mode", sa.String(16), "full_text/vector/hybrid/rerank", default="full_text"
        ),
        column("top_k", sa.Integer(), "召回条数，1 至 20", default="5"),
        column("similarity_threshold", sa.Numeric(6, 5), "归一化召回阈值，0 至 1", default="0.2"),
        column("revision", sa.Integer(), "仅配置修改时递增；任务保存对应配置修订号", default="1"),
        column("needs_rebuild", sa.Boolean(), "配置或文档改变后是否需要重建", default=sa.true()),
        column("retrieval_count", sa.BigInteger(), "实际检索尝试总次数，原子累加", default="0"),
        column("retrieval_success_count", sa.BigInteger(), "实际检索成功次数", default="0"),
        column("retrieval_elapsed_ms", sa.Numeric(24, 3), "检索累计耗时，单位毫秒", default="0"),
        column("updater_id", UUID(), "配置最近修改人；初始化可空", nullable=True),
        time_column("created_at", "索引状态创建时间"),
        time_column("updated_at", "配置或运行数据更新时间"),
        fk(table, "knowledge_base_id", "knowledge_bases"),
        fk(table, "updater_id", "users"),
        sa.UniqueConstraint("knowledge_base_id", name=f"uq_{table}_knowledge_base_id"),
        check(table, "mode", "mode IN ('economic','high_quality')"),
        check(
            table,
            "embedding_model",
            "mode<>'high_quality' OR "
            "(embedding_model IS NOT NULL AND length(trim(embedding_model))>0)",
        ),
        check(
            table,
            "chunk_mode",
            "chunk_mode IN "
            "('auto','paragraph','fixed','intelligent','semantic','rule','recursive')",
        ),
        check(table, "chunk_size", "chunk_size BETWEEN 100 AND 2000"),
        check(
            table, "chunk_overlap", "chunk_overlap BETWEEN 0 AND 500 AND chunk_overlap<chunk_size"
        ),
        check(
            table,
            "retrieval_mode",
            "retrieval_mode IN ('full_text','vector','hybrid','rerank') "
            "AND (mode<>'economic' OR retrieval_mode='full_text')",
        ),
        check(table, "top_k", "top_k BETWEEN 1 AND 20"),
        check(table, "threshold", "similarity_threshold BETWEEN 0 AND 1"),
        check(table, "revision", "revision>=1"),
        check(
            table,
            "metrics",
            "retrieval_count>=0 AND retrieval_success_count BETWEEN 0 AND retrieval_count "
            "AND retrieval_elapsed_ms>=0 AND retrieval_elapsed_ms< 'Infinity'::numeric",
        ),
        comment="知识库索引与召回参数及运行统计；统计修改不改变配置修订号",
    )


def create_knowledge_documents() -> None:
    """文档独立上传和审核，文件内容不可覆盖；删除保留审核历史。"""
    table = "knowledge_documents"
    create(
        table,
        column("knowledge_base_id", UUID(), "所属知识库"),
        column("file_id", UUID(), "不可覆盖的原始文件 UUID"),
        column("name", sa.String(255), "文档名称；上传时确定，活动文档同库不重名"),
        column("uploader_id", UUID(), "提交文档的用户"),
        column(
            "review_status",
            sa.String(16),
            "pending/approved/rejected/needs_changes",
            default="pending",
        ),
        column("submission_no", sa.Integer(), "文档送审轮次，从 1 开始", default="1"),
        column("revision", sa.Integer(), "文档状态修改时递增，用于拒绝旧请求", default="1"),
        column(
            "processing_status",
            sa.String(16),
            "awaiting_review/pending/parsing/ready/failed/stale",
            default="awaiting_review",
        ),
        column("index_job_id", UUID(), "最近一次处理该文档的索引任务", nullable=True),
        column("error_message", sa.String(2000), "本次解析或索引失败说明", nullable=True),
        time_column("submitted_at", "本轮送审时间"),
        time_column("processed_at", "最近处理结束时间", nullable=True),
        time_column("deleted_at", "文档删除时间；删除后不可再召回", nullable=True),
        time_column("created_at", "上传登记时间"),
        time_column("updated_at", "状态更新时间"),
        fk(table, "knowledge_base_id", "knowledge_bases"),
        fk(table, "file_id", "files"),
        fk(table, "uploader_id", "users"),
        sa.UniqueConstraint("id", "knowledge_base_id", name=f"uq_{table}_id_base"),
        check(table, "name", "length(trim(name))>0"),
        check(
            table,
            "review_status",
            "review_status IN ('pending','approved','rejected','needs_changes')",
        ),
        check(table, "revision", "revision>=1 AND submission_no>=1"),
        check(
            table,
            "processing_status",
            "processing_status IN ('awaiting_review','pending','parsing','ready','failed','stale')",
        ),
        check(
            table,
            "review_processing",
            "review_status='approved' OR processing_status='awaiting_review'",
        ),
        check(
            table,
            "job_required",
            "processing_status NOT IN ('parsing','ready','failed') OR index_job_id IS NOT NULL",
        ),
        comment="知识库独立文档及其当前审核、解析状态，不代替资源发布申请",
    )
    op.create_index(
        "uq_knowledge_documents_active_name",
        table,
        ["knowledge_base_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_knowledge_documents_file_id", table, ["file_id"])
    op.create_index(
        "ix_knowledge_documents_review_queue", table, ["review_status", "submitted_at", "id"]
    )
    table = "knowledge_document_reviews"
    create(
        table,
        column("document_id", UUID(), "被审核文档"),
        column("submission_no", sa.Integer(), "被审核的送审轮次"),
        column("reviewer_id", UUID(), "执行审核的管理员"),
        column(
            "decision", sa.String(16), "approved/rejected/needs_changes；保留现有文档退回补充流程"
        ),
        column("comment", sa.String(2000), "审核意见；退回或驳回必填", nullable=True),
        column("submission_snapshot", JSONB(), "冻结 documentId/fileId/name，不能套用其他文档"),
        time_column("submitted_at", "被审核轮次的提交时间"),
        time_column("created_at", "审核结论产生时间"),
        fk(table, "document_id", "knowledge_documents"),
        fk(table, "reviewer_id", "users"),
        sa.UniqueConstraint("document_id", "submission_no", name=f"uq_{table}_document_round"),
        check(table, "submission_no", "submission_no>=1"),
        check(table, "decision", "decision IN ('approved','rejected','needs_changes')"),
        check(
            table,
            "comment",
            "decision='approved' OR (comment IS NOT NULL AND length(trim(comment))>0)",
        ),
        check(table, "snapshot", "jsonb_typeof(submission_snapshot)='object'"),
        comment="独立文档逐轮审核的不可变结论；资源发布仍使用 resource_reviews",
    )
    op.create_index("ix_knowledge_document_reviews_created_at", table, ["created_at"])


def create_knowledge_jobs_and_chunks() -> None:
    """任务固化配置与文档集合，分段保留生成任务以拒绝旧任务覆盖。"""
    table = "knowledge_index_jobs"
    create(
        table,
        column("knowledge_base_id", UUID(), "所属知识库"),
        column("knowledge_base_version_id", UUID(), "启动时已发布的知识库资料版本"),
        column("configuration_revision", sa.Integer(), "任务创建时的索引配置修订"),
        column("configuration_snapshot", JSONB(), "冻结本次配置；字段使用小驼峰，与索引状态匹配"),
        column("document_ids", ARRAY(UUID()), "本任务处理的已审批文档 UUID 列表，最多 500 篇"),
        column("mode", sa.String(16), "full 完整重建 / incremental 增量 / retry 重试"),
        column(
            "status", sa.String(16), "queued/running/completed/failed/cancelled", default="queued"
        ),
        column("requester_id", UUID(), "发起任务的用户"),
        column(
            "worker_token", UUID(), "运行工作进程的租约令牌；回写同时校验任务与令牌", nullable=True
        ),
        time_column("lease_expires_at", "工作租约到期时间；超时由任务恢复流程处理", nullable=True),
        time_column("started_at", "开始处理时间", nullable=True),
        time_column("finished_at", "任务结束时间", nullable=True),
        column("error_message", sa.String(2000), "任务失败原因；堆栈进入系统日志", nullable=True),
        time_column("created_at", "任务登记时间"),
        time_column("updated_at", "状态或心跳更新时间"),
        fk(table, "knowledge_base_id", "knowledge_bases"),
        fk(table, "requester_id", "users"),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_index_states.knowledge_base_id"],
            name="fk_knowledge_index_jobs_base_state",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_version_id", "knowledge_base_id"],
            ["knowledge_base_versions.id", "knowledge_base_versions.knowledge_base_id"],
            name="fk_knowledge_index_jobs_base_version",
        ),
        sa.UniqueConstraint("id", "knowledge_base_id", name=f"uq_{table}_id_base"),
        check(
            table,
            "configuration",
            "configuration_revision>=1 AND jsonb_typeof(configuration_snapshot)='object'",
        ),
        check(table, "documents", "aih_valid_document_ids(document_ids)"),
        check(table, "mode", "mode IN ('full','incremental','retry')"),
        check(table, "status", "status IN ('queued','running','completed','failed','cancelled')"),
        check(
            table,
            "lifecycle",
            "(status='queued' AND started_at IS NULL AND finished_at IS NULL "
            "AND worker_token IS NULL AND lease_expires_at IS NULL) OR "
            "(status='running' AND started_at IS NOT NULL AND finished_at IS NULL "
            "AND worker_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND lease_expires_at>started_at) OR "
            "(status IN ('completed','failed','cancelled') AND finished_at IS NOT NULL "
            "AND (started_at IS NULL OR finished_at>=started_at))",
        ),
        comment="索引任务、配置快照与租约；每库最多一个排队或运行任务",
    )
    op.create_index(
        "uq_knowledge_index_jobs_active_base",
        table,
        ["knowledge_base_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued','running')"),
    )
    op.create_index(
        "ix_knowledge_index_jobs_base_created", table, ["knowledge_base_id", "created_at"]
    )
    op.create_foreign_key(
        "fk_knowledge_documents_job_base",
        "knowledge_documents",
        table,
        ["index_job_id", "knowledge_base_id"],
        ["id", "knowledge_base_id"],
    )
    table = "knowledge_document_chunks"
    create(
        table,
        column("knowledge_base_id", UUID(), "所属知识库，与文档及任务保持一致"),
        column("document_id", UUID(), "分段来源文档"),
        column("index_job_id", UUID(), "生成该分段的索引任务"),
        column("position", sa.Integer(), "文档内从 1 开始的分段序号"),
        column("content", sa.Text(), "实际解析得到的分段文本"),
        column(
            "embedding", JSONB(), "真实模型返回的数值向量；全文模式为空，不伪造向量", nullable=True
        ),
        time_column("created_at", "分段持久化时间"),
        sa.ForeignKeyConstraint(
            ["document_id", "knowledge_base_id"],
            ["knowledge_documents.id", "knowledge_documents.knowledge_base_id"],
            name="fk_knowledge_document_chunks_document_base",
        ),
        sa.ForeignKeyConstraint(
            ["index_job_id", "knowledge_base_id"],
            ["knowledge_index_jobs.id", "knowledge_index_jobs.knowledge_base_id"],
            name="fk_knowledge_document_chunks_job_base",
        ),
        sa.UniqueConstraint(
            "document_id", "index_job_id", "position", name=f"uq_{table}_job_position"
        ),
        check(table, "position", "position>=1"),
        check(table, "content", "length(trim(content))>0"),
        check(table, "embedding", "embedding IS NULL OR aih_valid_embedding(embedding)"),
        comment="可重建的文档分段与可选真实向量，不将分段当成资源资料历史版本",
    )
    op.create_index(
        "ix_knowledge_document_chunks_base_document",
        table,
        ["knowledge_base_id", "document_id", "position"],
    )


def upgrade() -> None:
    """在同一事务内追加十一表及跨表保护，不迁移或导入旧 Mock 数据。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    directory = Path(__file__).parent
    op.execute((directory / "20260921_03_validation.sql").read_text(encoding="utf-8"))
    create_conversations()
    create_home_configuration()
    create_knowledge_index_states()
    create_knowledge_documents()
    create_knowledge_jobs_and_chunks()
    op.execute((directory / "20260921_03_home_guards.sql").read_text(encoding="utf-8"))
    op.execute((directory / "20260921_03_knowledge_guards.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    """问答、审核与文件历史不自动删除，回退使用本次迁移前备份。"""
    raise RuntimeError("Home and knowledge downgrade requires restoring the pre-migration backup")
