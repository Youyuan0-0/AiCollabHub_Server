"""建立五类资源、共用互动审核及已定稿身份日志的空库基线。"""

from pathlib import Path

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260920_01_resource_foundation"
down_revision = None
branch_labels = None
depends_on = None

# 迁移冻结当次字段定义，不导入可能随业务开发变化的 ORM 模型。
MODULES = (
    ("dataset", "datasets", "dataset", "数据集"),
    ("model", "models", "model", "模型"),
    ("application", "applications", "application", "场景应用"),
    ("knowledge", "knowledge_bases", "knowledge_base", "知识库"),
    ("tool", "ai_tools", "ai_tool", "AI 工具"),
)
RESOURCE_TYPES = "'dataset', 'model', 'application', 'knowledge', 'tool'"


def col(name, column_type, comment, *, nullable=False, default=None):
    """构造带中文业务说明的字段；默认值由数据库统一执行。"""
    return sa.Column(name, column_type, nullable=nullable, comment=comment, server_default=default)


def identifier():
    """平台生成 UUID；SQL 导入未传编号时也能生成独立记录。"""
    return col("id", UUID(), "平台生成的稳定 UUID", default=sa.text("gen_random_uuid()"))


def timestamp(name, comment, *, nullable=False, default=False):
    """记录带时区时间；来源时间戳仍由身份同步字段单独保存。"""
    return col(
        name,
        sa.DateTime(timezone=True),
        comment,
        nullable=nullable,
        default=sa.func.now() if default else None,
    )


def json_column(name, comment, *, array=False, nullable=False):
    """受约束的业务 JSON，后端仍须验证具体内容和大小限制。"""
    return col(
        name,
        JSONB(),
        comment,
        nullable=nullable,
        default=None if nullable else sa.text("'[]'::jsonb" if array else "'{}'::jsonb"),
    )


def check(table, name, expression):
    return sa.CheckConstraint(expression, name=sa.schema.conv(f"ck_{table}_{name}"))


def unique(table, *columns):
    return sa.UniqueConstraint(*columns, name=sa.schema.conv(f"uq_{table}_{'_'.join(columns)}"))


def foreign_key(table, columns, target, target_columns=("id",), *, deferred=False):
    """循环发布关系延迟到事务结束检查，普通用户关联立即检查。"""
    return sa.ForeignKeyConstraint(
        columns,
        [f"{target}.{name}" for name in target_columns],
        name=sa.schema.conv(f"fk_{table}_{columns[0]}_{target}"),
        ondelete="NO ACTION",
        deferrable=deferred,
        initially="DEFERRED" if deferred else "IMMEDIATE",
    )


def create(table, *items, comment):
    op.create_table(
        table, *items, sa.PrimaryKeyConstraint("id", name=f"pk_{table}"), comment=comment
    )


def index(table, *columns, suffix=None, where=None):
    op.create_index(
        f"ix_{table}_{suffix or '_'.join(columns)}",
        table,
        list(columns),
        postgresql_where=sa.text(where) if where else None,
    )


def create_identity_tables():
    """沿用已实现的身份与日志字段及枚举，不重新复制 IAM 用户全量资料。"""
    table = "users"
    create(
        table,
        identifier(),
        col("iam_subject", sa.String(200), "集团 IAM 用户唯一标识"),
        col("display_name", sa.String(100), "用户显示名称；未同步资料时可空", nullable=True),
        col(
            "status", sa.String(16), "账号状态：pending/active/disabled/deleted", default="pending"
        ),
        col("role", sa.String(16), "平台角色：user/admin", default="user"),
        col("user_sync_time", sa.BigInteger(), "IAM 用户资料 syncTime 原值", nullable=True),
        col("role_sync_time", sa.BigInteger(), "IAM 角色资料 syncTime 原值", nullable=True),
        timestamp("created_at", "本地记录创建时间", default=True),
        timestamp("updated_at", "本地资料或同步状态更新时间", default=True),
        unique(table, "iam_subject"),
        check(table, "iam_subject", "length(trim(iam_subject)) > 0"),
        check(table, "display_name", "display_name IS NULL OR length(trim(display_name)) > 0"),
        check(table, "status", "status IN ('pending','active','disabled','deleted')"),
        check(table, "role", "role IN ('user','admin')"),
        check(table, "user_sync_time", "user_sync_time > 0"),
        check(table, "role_sync_time", "role_sync_time > 0"),
        comment="集团用户的最小本地映射，供业务关联稳定 UUID",
    )
    table = "auth_sessions"
    create(
        table,
        identifier(),
        col("user_id", UUID(), "登录用户；提前登出标记为空", nullable=True),
        col(
            "record_type",
            sa.String(24),
            "session 会话 / ticket_revocation 提前撤销标记",
            default="session",
        ),
        col("token_hash", sa.String(64), "浏览器会话令牌的 SHA-256 摘要", nullable=True),
        col("ticket_hash", sa.String(64), "IAM 票据的 SHA-256 摘要"),
        timestamp("created_at", "会话或撤销标记创建时间", default=True),
        timestamp("expires_at", "会话到期时间或撤销标记保留期限"),
        timestamp("revoked_at", "会话撤销时间", nullable=True),
        foreign_key(table, ["user_id"], "users"),
        unique(table, "token_hash"),
        unique(table, "ticket_hash"),
        check(table, "record_type", "record_type IN ('session','ticket_revocation')"),
        check(table, "lifetime", "expires_at > created_at"),
        check(
            table,
            "record",
            "(record_type='session' AND user_id IS NOT NULL AND token_hash IS NOT NULL) OR "
            "(record_type='ticket_revocation' AND user_id IS NULL "
            "AND token_hash IS NULL AND revoked_at IS NOT NULL)",
        ),
        check(table, "token_hash", "token_hash ~ '^[0-9a-f]{64}$'"),
        check(table, "ticket_hash", "ticket_hash ~ '^[0-9a-f]{64}$'"),
        comment="跨用户端与管理端共用登录会话，只存凭证摘要",
    )
    index(table, "expires_at")
    index(
        table,
        "user_id",
        suffix="active_user_id",
        where="record_type='session' AND revoked_at IS NULL",
    )
    table = "iam_sync_receipts"
    create(
        table,
        identifier(),
        col("payload_hash", sa.String(64), "规范化同步报文的去重摘要"),
        col("resource_type", sa.String(32), "同步对象：user/userlinkedrole"),
        col("action", sa.String(16), "同步动作：push/recycle"),
        col("sync_time", sa.BigInteger(), "IAM syncTime 原始来源时间戳"),
        timestamp("created_at", "本地成功提交同步批次的时间", default=True),
        unique(table, "payload_hash"),
        check(table, "payload_hash", "payload_hash ~ '^[0-9a-f]{64}$'"),
        check(table, "resource_type", "resource_type IN ('user','userlinkedrole')"),
        check(table, "action", "action IN ('push','recycle')"),
        check(table, "sync_time", "sync_time > 0"),
        comment="身份同步成功批次的去重回执，与用户变更在同一事务提交",
    )
    index(table, "created_at")
    table = "operation_logs"
    create(
        table,
        identifier(),
        col("actor_type", sa.String(16), "发起方：user/iam/system；沿用已实现契约"),
        col("user_id", UUID(), "实际操作者，IAM 推送不冒充被同步用户", nullable=True),
        col("action", sa.String(40), "成功业务动作"),
        col("target_type", sa.String(32), "操作对象类型"),
        col("target_id", sa.String(200), "被操作对象编号", nullable=True),
        col("request_id", UUID(), "后端请求追踪编号", nullable=True),
        json_column("data", "受控的成功操作摘要，不保存口令和令牌"),
        timestamp("created_at", "操作成功时间", default=True),
        foreign_key(table, ["user_id"], "users"),
        check(table, "actor_type", "actor_type IN ('user','iam','system')"),
        check(
            table,
            "actor",
            "(actor_type='user' AND user_id IS NOT NULL) OR "
            "(actor_type<>'user' AND user_id IS NULL)",
        ),
        check(
            table,
            "action",
            "action IN ('login','logout','single_logout','create','update','delete',"
            "'enable','disable','grant','revoke','sync','invoke','upload','download','review','configure')",
        ),
        check(
            table,
            "target_type",
            "target_type IN ('user','session','identity_sync','dataset','model',"
            "'application','knowledge_base','tool','conversation','feedback','file','platform')",
        ),
        check(table, "data", "jsonb_typeof(data)='object'"),
        comment="成功业务操作日志，包含调用和下载动作；失败进入系统日志",
    )
    index(table, "created_at")
    index(table, "user_id", "created_at", where="user_id IS NOT NULL")
    index(table, "target_type", "target_id", "created_at")
    index(table, "request_id", where="request_id IS NOT NULL")
    table = "system_logs"
    create(
        table,
        identifier(),
        col("level", sa.String(16), "日志级别：info/warning/error"),
        col("source", sa.String(64), "产生日志的模块或组件"),
        col("event", sa.String(100), "稳定的技术事件编码"),
        col("message", sa.Text(), "便于阅读的事件或异常摘要"),
        col("request_id", UUID(), "后端请求 UUID，后台事件可空", nullable=True),
        json_column("details", "已脱敏的结构化诊断上下文"),
        timestamp("created_at", "事件发生时间", default=True),
        check(table, "level", "level IN ('info','warning','error')"),
        check(table, "details", "jsonb_typeof(details)='object'"),
        comment="系统运行事件与异常日志，面向技术运维排查",
    )
    index(table, "created_at")
    index(table, "request_id", where="request_id IS NOT NULL")


def create_resource_main(kind, table, prefix, label):
    """每个模块保留独立主表，只保存当前正式展示的资料。"""
    items = [
        identifier(),
        col("name", sa.String(200), f"{label}名称"),
        col("english_name", sa.String(200), "英文名称", nullable=True),
        col("summary", sa.Text(), "列表及概览简介"),
        col(
            "description_markdown",
            sa.Text(),
            "资源详细说明，Markdown 正文",
            nullable=kind != "dataset",
        ),
        col(
            "provider_name", sa.String(200), "提供单位，不表示创建人", nullable=kind == "knowledge"
        ),
        col("contact", sa.String(200), "联系人姓名", nullable=True),
        json_column("tags", "关键词标签，去空去重的字符串数组", array=True),
        col("creator_id", UUID(), "本地创建人，外部导入未映射时可空", nullable=True),
        col("current_version_id", UUID(), "本资源当前正式版本", nullable=True),
        col("status", sa.String(16), "enabled 启用 / disabled 禁用", default="disabled"),
        timestamp("created_at", "本地主表创建时间", default=True),
        timestamp("updated_at", "主表资料或启用状态更新时间", default=True),
        foreign_key(table, ["creator_id"], "users"),
        check(table, "name", "length(trim(name)) > 0"),
        check(table, "summary", "length(trim(summary)) > 0"),
        check(table, "status", "status IN ('enabled','disabled')"),
        check(table, "enabled_version", "status<>'enabled' OR current_version_id IS NOT NULL"),
    ]
    arrays = ["tags"]
    if kind == "dataset":
        arrays += ["task_types", "data_applications", "data_shapes"]
        items += [
            col("ai_purpose", sa.String(64), "AI 应用目的，按平台字典维护"),
            col("update_frequency", sa.String(100), "更新频率说明", nullable=True),
            col("record_count", sa.BigInteger(), "当前正式版本记录数，未知为空", nullable=True),
            col("data_size_bytes", sa.BigInteger(), "当前正式版本大小，单位字节", nullable=True),
            check(table, "record_count", "record_count >= 0"),
            check(table, "data_size_bytes", "data_size_bytes >= 0"),
        ]
    if kind == "application":
        arrays += ["capabilities"]
        items += [
            col("category", sa.String(64), "应用业务分类，按字典维护"),
            col("development_team", sa.String(200), "开发团队", nullable=True),
            col("sharing_agreement", sa.Text(), "共享协议说明", nullable=True),
        ]
    if kind == "model":
        arrays += ["task_types", "frameworks", "weight_formats", "precisions", "capabilities"]
        items += [
            col("architecture", sa.String(200), "当前正式版本模型架构", nullable=True),
            col("parameter_count", sa.BigInteger(), "总参数量，未知或不适用为空", nullable=True),
            col("context_length_tokens", sa.Integer(), "上下文长度，单位 Token", nullable=True),
            col("license", sa.String(200), "许可协议名称", nullable=True),
            check(table, "parameter_count", "parameter_count > 0"),
            check(table, "context_length_tokens", "context_length_tokens > 0"),
        ]
    if kind == "knowledge":
        items += [
            col("category", sa.String(64), "平台业务分类", nullable=True),
            col("knowledge_type", sa.String(64), "平台知识库类型，外部编码映射待联调确认"),
            col("security_level", sa.String(32), "集团密级编码，未知不默认公开", nullable=True),
        ]
    if kind == "tool":
        arrays += ["capabilities"]
        items += [
            col("category", sa.String(64), "工具业务分类"),
            col("source_type", sa.String(32), "self_developed 自研 / third_party 第三方"),
            col("development_team", sa.String(200), "研发团队", nullable=True),
            col("license", sa.String(200), "许可协议名称", nullable=True),
            check(table, "source_type", "source_type IN ('self_developed','third_party')"),
        ]
    descriptions = {
        "task_types": "任务类型",
        "data_applications": "数据应用领域",
        "data_shapes": "数据形态",
        "capabilities": "业务能力说明",
        "frameworks": "模型框架",
        "weight_formats": "权重格式",
        "precisions": "数值精度",
    }
    for name in arrays:
        if name != "tags":
            items.append(json_column(name, f"{descriptions[name]}，字符串数组", array=True))
        items.append(check(table, name, f"aih_is_string_array({name})"))
    create(table, *items, comment=f"{label}稳定标识、当前正式概览及启用状态")
    index(table, "status", "updated_at")
    index(table, "creator_id")
    if kind in {"application", "knowledge", "tool"}:
        index(table, "category")


def create_publication_and_version(kind, main, prefix, label):
    """循环引用先建表再补外键，支持首次发布的一次事务写入。"""
    publication, version, resource_id = (
        f"{prefix}_publications",
        f"{prefix}_versions",
        f"{prefix}_id",
    )
    create(
        publication,
        identifier(),
        col(resource_id, UUID(), "所属资源，首次发布前为空", nullable=True),
        col("applicant_id", UUID(), "实际发布申请人"),
        col("base_version_id", UUID(), "更新申请基于的正式版本，首次发布为空", nullable=True),
        col("version", sa.String(80), "拟发布版本号，提交审核时必填", nullable=True),
        col("version_notes", sa.Text(), "本次版本说明", nullable=True),
        json_column("data", "本模块发布资料，概览与专属内容由模块校验"),
        col("review_status", sa.String(16), "草稿为空；pending/approved/rejected", nullable=True),
        col("submission_no", sa.Integer(), "提交轮次，草稿为 0，重提递增", default="0"),
        col("revision", sa.BigInteger(), "本地修改版本，保存与状态变更递增", default="1"),
        timestamp("submitted_at", "最近一轮提交时间", nullable=True),
        col("published_version_id", UUID(), "正式发布结果编号", nullable=True),
        timestamp("published_at", "正式发布时间", nullable=True),
        timestamp("created_at", "申请创建时间", default=True),
        timestamp("updated_at", "申请最近修改时间", default=True),
        foreign_key(publication, [resource_id], main, deferred=True),
        foreign_key(publication, ["applicant_id"], "users"),
        unique(publication, "id", resource_id),
        check(publication, "review_status", "review_status IN ('pending','approved','rejected')"),
        check(publication, "data", "jsonb_typeof(data)='object'"),
        check(publication, "revision", "revision >= 1"),
        check(
            publication,
            "submission",
            "(review_status IS NULL AND submission_no=0 AND submitted_at IS NULL) OR "
            "(review_status IS NOT NULL AND submission_no>=1 AND submitted_at IS NOT NULL "
            "AND version IS NOT NULL AND length(trim(version))>0)",
        ),
        check(
            publication, "base_resource", f"base_version_id IS NULL OR {resource_id} IS NOT NULL"
        ),
        check(
            publication,
            "publication",
            "(published_version_id IS NULL AND published_at IS NULL) OR "
            "(published_version_id IS NOT NULL AND published_at IS NOT NULL "
            "AND review_status IS NOT NULL AND review_status='approved' "
            f"AND {resource_id} IS NOT NULL)",
        ),
        comment=f"{label}草稿、待审资料和发布结果；驳回重提保留同一申请编号",
    )
    index(publication, "applicant_id", "updated_at")
    index(publication, "review_status", "submitted_at")
    index(publication, resource_id)
    create(
        version,
        identifier(),
        col(resource_id, UUID(), "所属稳定资源 UUID"),
        col("publication_id", UUID(), "来源发布申请，每份申请只产生一个正式版本"),
        col("version", sa.String(80), "正式版本号，不按字符串大小判定新旧"),
        col("version_notes", sa.Text(), "正式版本说明", nullable=True),
        json_column("snapshot", "正式资料快照与不可变附件引用"),
        col("publisher_id", UUID(), "执行正式发布的管理员"),
        timestamp("published_at", "正式发布时间", default=True),
        foreign_key(version, [resource_id], main, deferred=True),
        foreign_key(
            version,
            ["publication_id", resource_id],
            publication,
            ["id", resource_id],
            deferred=True,
        ),
        foreign_key(version, ["publisher_id"], "users"),
        unique(version, resource_id, "version"),
        unique(version, "publication_id"),
        unique(version, "id", resource_id),
        unique(version, "id", resource_id, "publication_id"),
        check(version, "version", "length(trim(version)) > 0"),
        check(version, "snapshot", "jsonb_typeof(snapshot)='object'"),
        comment=f"{label}已正式发布的不可变历史版本",
    )
    for source, source_columns, target_columns in (
        (main, ["current_version_id", "id"], ["id", resource_id]),
        (publication, ["base_version_id", resource_id], ["id", resource_id]),
        (
            publication,
            ["published_version_id", resource_id, "id"],
            ["id", resource_id, "publication_id"],
        ),
    ):
        op.create_foreign_key(
            op.f(f"fk_{source}_{source_columns[0]}_{version}"),
            source,
            version,
            source_columns,
            target_columns,
            deferrable=True,
            initially="DEFERRED",
        )


def create_shared_tables():
    """跨模块用户互动使用稳定资源编号，审核关联申请和提交轮次。"""
    for table in (
        "resource_favorites",
        "resource_follows",
        "resource_ratings",
        "resource_downloads",
    ):
        items = [
            identifier(),
            col(
                "resource_type", sa.String(16), "资源类型：dataset/model/application/knowledge/tool"
            ),
            col("resource_id", UUID(), "对应模块的稳定资源编号"),
            col("user_id", UUID(), "实际操作用户"),
            foreign_key(table, ["user_id"], "users"),
            check(table, "resource_type", f"resource_type IN ({RESOURCE_TYPES})"),
            timestamp("created_at", "记录创建时间", default=True),
        ]
        if table != "resource_downloads":
            items.append(unique(table, "resource_type", "resource_id", "user_id"))
        if table == "resource_ratings":
            items += [
                col("rating", sa.SmallInteger(), "用户评分，1 至 5 的整数"),
                col("comment", sa.Text(), "用户评价内容", nullable=True),
                timestamp("updated_at", "评价最近更新时间", default=True),
                check(table, "rating", "rating BETWEEN 1 AND 5"),
            ]
        if table == "resource_downloads":
            items += [
                col("version_id", UUID(), "下载所属版本，非版本资料可空", nullable=True),
                col(
                    "file_id", UUID(), "文件编号；文件元数据表未定稿，本轮不设置外键", nullable=True
                ),
                col("idempotency_key", sa.String(128), "同一用户同一次下载重试共用的幂等键"),
                col(
                    "status",
                    sa.String(16),
                    "pending/link_issued/completed/failed",
                    default="pending",
                ),
                col("error_code", sa.String(64), "失败错误码", nullable=True),
                timestamp("updated_at", "下载状态更新时间", default=True),
                timestamp("completed_at", "可信传输证据确认的完成时间", nullable=True),
                unique(table, "user_id", "idempotency_key"),
                check(table, "idempotency_key", "length(trim(idempotency_key))>0"),
                check(table, "status", "status IN ('pending','link_issued','completed','failed')"),
                check(table, "completion", "(status='completed') = (completed_at IS NOT NULL)"),
            ]
        label = {
            "resource_favorites": "收藏",
            "resource_follows": "更新关注",
            "resource_ratings": "用户评价",
            "resource_downloads": "下载请求和实际完成证据",
        }[table]
        create(table, *items, comment=f"五类资源共用的{label}")
        index(table, "user_id", "created_at")
        index(table, "resource_type", "resource_id")
    table = "resource_reviews"
    create(
        table,
        identifier(),
        col("resource_type", sa.String(16), "资源类型"),
        col("publication_id", UUID(), "对应模块发布申请编号"),
        col("submission_no", sa.Integer(), "审核的提交轮次"),
        col("reviewer_id", UUID(), "执行审核的管理员"),
        col("decision", sa.String(16), "approved 通过 / rejected 驳回"),
        col("comment", sa.Text(), "审核意见，驳回时必填", nullable=True),
        json_column("quality_scores", "按模块维度校验的质量评分", nullable=True),
        json_column("submission_snapshot", "本轮版本号、说明及提交资料的完整快照"),
        timestamp("submitted_at", "被审核轮次的提交时间"),
        timestamp("created_at", "审核结论产生时间", default=True),
        foreign_key(table, ["reviewer_id"], "users"),
        unique(table, "resource_type", "publication_id", "submission_no"),
        check(table, "resource_type", f"resource_type IN ({RESOURCE_TYPES})"),
        check(table, "submission_no", "submission_no >= 1"),
        check(table, "decision", "decision IN ('approved','rejected')"),
        check(
            table,
            "comment",
            "decision<>'rejected' OR (comment IS NOT NULL AND length(trim(comment))>0)",
        ),
        check(
            table,
            "quality_scores",
            "quality_scores IS NULL OR jsonb_typeof(quality_scores)='object'",
        ),
        check(table, "submission_snapshot", "jsonb_typeof(submission_snapshot)='object'"),
        comment="五类资源共用的不可变审核历史，每次提交只允许一个最终结论",
    )
    index(table, "reviewer_id", "created_at")
    index(table, "created_at")


def upgrade() -> None:
    """只对空 schema 建立基线；存在旧业务表时停止，避免误当升级执行。"""
    if not context.is_offline_mode():
        tables = set(sa.inspect(op.get_bind()).get_table_names()) - {"alembic_version"}
        if tables:
            raise RuntimeError(
                "Foundation baseline requires an empty schema; existing tables found"
            )
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    sql_directory = Path(__file__).resolve().parent
    op.execute((sql_directory / "20260920_01_functions.sql").read_text(encoding="utf-8"))
    create_identity_tables()
    for module in MODULES:
        create_resource_main(*module)
        create_publication_and_version(*module)
    create_shared_tables()
    op.execute((sql_directory / "20260920_01_triggers.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    """有业务数据的基线不执行自动删库，回退使用已校验的备份。"""
    raise RuntimeError("Foundation downgrade is destructive; restore the pre-migration backup")
