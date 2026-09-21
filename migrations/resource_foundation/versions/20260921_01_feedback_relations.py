"""新增共用文件、问题反馈和应用版本关联，不引入外部资源映射。"""

from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "20260921_01_feedback_relations"
down_revision = "20260920_01_resource_foundation"
branch_labels = None
depends_on = None


def column(
    name: str,
    column_type: sa.types.TypeEngine,
    comment: str,
    *,
    nullable: bool = False,
    default: str | sa.sql.ClauseElement | None = None,
) -> sa.Column:
    """冻结本次迁移字段，明确可空性、数据库默认值和中文说明。"""
    return sa.Column(name, column_type, nullable=nullable, server_default=default, comment=comment)


def identifier() -> sa.Column:
    """生成稳定 UUID，不将用户或供应方的外部编号当作主键。"""
    return column("id", UUID(), "平台生成的稳定 UUID", default=sa.text("gen_random_uuid()"))


def timestamp(name: str, comment: str, *, nullable: bool = False) -> sa.Column:
    """只有创建和更新时间默认取当前时间，业务事件时间由调用方显式提供。"""
    return column(
        name,
        sa.DateTime(timezone=True),
        comment,
        nullable=nullable,
        default=sa.func.now() if name in {"created_at", "updated_at"} else None,
    )


def check(table: str, name: str, expression: str) -> sa.CheckConstraint:
    """显式命名检查约束，避免项目命名约定重复添加前缀。"""
    return sa.CheckConstraint(expression, name=sa.schema.conv(f"ck_{table}_{name}"))


def unique(table: str, *columns: str) -> sa.UniqueConstraint:
    """按业务组合建立唯一约束，交由数据库处理并发重复提交。"""
    return sa.UniqueConstraint(*columns, name=sa.schema.conv(f"uq_{table}_{'_'.join(columns)}"))


def foreign_key(table: str, name: str, target: str) -> sa.ForeignKeyConstraint:
    """保留业务引用，不对用户、文件或历史记录执行级联删除。"""
    return sa.ForeignKeyConstraint(
        [name], [f"{target}.id"], name=f"fk_{table}_{name}_{target}", ondelete="NO ACTION"
    )


def create_files() -> None:
    """建立全平台共用文件元数据，文件内容继续由 MinIO 保存。"""
    table = "files"
    op.create_table(
        table,
        identifier(),
        column("uploader_id", UUID(), "上传用户；受控系统上传可空", nullable=True),
        column("file_name", sa.String(255), "下载展示文件名，不包含目录路径"),
        column("mime_type", sa.String(127), "服务端核实的 MIME 类型", nullable=True),
        column("size_bytes", sa.BigInteger(), "服务端核实的字节数，上传完成后必填", nullable=True),
        column("bucket", sa.String(63), "服务端配置选取的 MinIO 存储桶"),
        column("object_key", sa.String(1024), "服务端生成的 ASCII 对象键"),
        column("content_hash", sa.String(64), "服务端计算的 SHA-256 摘要", nullable=True),
        column(
            "status", sa.String(16), "uploading/ready/failed/deleting/deleted", default="uploading"
        ),
        timestamp("expires_at", "未关联临时文件的到期时间，正式引用后清空", nullable=True),
        timestamp("created_at", "文件记录创建时间"),
        timestamp("updated_at", "文件状态和元数据更新时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "uploader_id", "users"),
        unique(table, "bucket", "object_key"),
        check(
            table,
            "file_name",
            "length(btrim(file_name))>0 AND position('/' in file_name)=0 "
            "AND position(chr(92) in file_name)=0",
        ),
        check(table, "mime_type", "mime_type IS NULL OR length(btrim(mime_type))>0"),
        check(table, "size_bytes", "size_bytes >= 0"),
        check(table, "bucket", "length(btrim(bucket))>0"),
        check(table, "object_key", "length(btrim(object_key))>0 AND object_key !~ '[^ -~]'"),
        check(table, "content_hash", "content_hash ~ '^[0-9a-f]{64}$'"),
        check(table, "status", "status IN ('uploading','ready','failed','deleting','deleted')"),
        check(table, "ready_size", "status<>'ready' OR size_bytes IS NOT NULL"),
        comment="问答、反馈及资源资料共用的文件元数据与生命周期，文件本体存 MinIO",
    )
    op.create_index("ix_files_uploader_id_created_at", table, ["uploader_id", "created_at"])
    op.create_index(
        "ix_files_cleanup",
        table,
        ["status", "expires_at"],
        postgresql_where=sa.text("expires_at IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_resource_downloads_file_id_files",
        "resource_downloads",
        table,
        ["file_id"],
        ["id"],
        ondelete="NO ACTION",
    )
    op.create_index(
        "ix_resource_downloads_file_id",
        "resource_downloads",
        ["file_id"],
        postgresql_where=sa.text("file_id IS NOT NULL"),
    )
    op.alter_column("resource_downloads", "file_id", comment="关联共用文件元数据，非文件下载可空")


def create_feedback() -> None:
    """分别保存原始反馈、不可变处理历史及附件引用，保持提交和处理幂等。"""
    table = "feedback_records"
    op.create_table(
        table,
        identifier(),
        column("submitter_id", UUID(), "反馈提交用户"),
        column(
            "feedback_type",
            sa.String(32),
            "function_error/experience_suggestion/resource_issue/other",
        ),
        column("type_other", sa.String(200), "其他反馈类型的补充说明", nullable=True),
        column(
            "module_code", sa.String(32), "home/datasets/models/applications/knowledge/tools/other"
        ),
        column("module_other", sa.String(200), "其他模块的补充说明", nullable=True),
        column("title", sa.String(60), "反馈标题，1 至 60 字"),
        column("description", sa.Text(), "原始问题描述，1 至 5000 字"),
        column("contact_info", sa.String(200), "本次反馈的联系方式，不修改 IAM 用户资料"),
        column(
            "status", sa.String(16), "processing 处理中 / resolved 已解决", default="processing"
        ),
        column("revision", sa.Integer(), "处理修订号，每次成功处理递增", default="1"),
        column("idempotency_key", UUID(), "同一提交重试复用的 UUID"),
        timestamp("created_at", "反馈首次提交时间"),
        timestamp("updated_at", "反馈最近成功处理时间"),
        timestamp("resolved_at", "本轮解决时间，重新打开后清空", nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "submitter_id", "users"),
        unique(table, "submitter_id", "idempotency_key"),
        check(
            table,
            "feedback_type",
            "feedback_type IN ('function_error','experience_suggestion','resource_issue','other')",
        ),
        check(
            table,
            "module_code",
            "module_code IN "
            "('home','datasets','models','applications','knowledge','tools','other')",
        ),
        check(
            table,
            "type_other",
            "(feedback_type='other' AND type_other IS NOT NULL "
            "AND length(btrim(type_other))>0) OR "
            "(feedback_type<>'other' AND type_other IS NULL)",
        ),
        check(
            table,
            "module_other",
            "(module_code='other' AND module_other IS NOT NULL "
            "AND length(btrim(module_other))>0) OR "
            "(module_code<>'other' AND module_other IS NULL)",
        ),
        check(table, "title", "length(btrim(title)) BETWEEN 1 AND 60"),
        check(table, "description", "length(btrim(description)) BETWEEN 1 AND 5000"),
        check(table, "contact_info", "length(btrim(contact_info))>0"),
        check(table, "status", "status IN ('processing','resolved')"),
        check(table, "revision", "revision>=1"),
        check(table, "resolution", "(status='resolved')=(resolved_at IS NOT NULL)"),
        comment="用户提交的原始反馈及当前处理状态，处理内容保存在事件表",
    )
    for fields in (("submitter_id", "created_at", "id"), ("status", "created_at", "id")):
        op.create_index(f"ix_{table}_{'_'.join(fields)}", table, list(fields))

    table = "feedback_events"
    op.create_table(
        table,
        identifier(),
        column("feedback_id", UUID(), "所属反馈 UUID"),
        column("actor_id", UUID(), "提交用户或实际处理管理员"),
        column("action", sa.String(16), "submitted/replied/resolved/reopened"),
        column("content", sa.Text(), "用户可见的处理说明；首次提交为空字符串"),
        column("from_status", sa.String(16), "处理前状态；首次提交为空", nullable=True),
        column("to_status", sa.String(16), "处理后状态：processing/resolved"),
        column("resulting_revision", sa.Integer(), "本次处理对应的主表修订号"),
        column("idempotency_key", UUID(), "同一次处理重试复用的 UUID"),
        timestamp("created_at", "处理事件发生时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "feedback_id", "feedback_records"),
        foreign_key(table, "actor_id", "users"),
        unique(table, "feedback_id", "resulting_revision"),
        unique(table, "feedback_id", "actor_id", "idempotency_key"),
        check(table, "action", "action IN ('submitted','replied','resolved','reopened')"),
        check(table, "from_status", "from_status IN ('processing','resolved')"),
        check(table, "to_status", "to_status IN ('processing','resolved')"),
        check(table, "resulting_revision", "resulting_revision>=1"),
        check(
            table,
            "transition",
            "(action='submitted' AND from_status IS NULL AND to_status='processing' "
            "AND resulting_revision=1 AND content='') OR "
            "(action<>'submitted' AND from_status IS NOT NULL AND resulting_revision>1 "
            "AND length(btrim(content)) BETWEEN 1 AND 5000 AND "
            "((action='replied' AND from_status=to_status) OR "
            "(action='resolved' AND from_status='processing' AND to_status='resolved') OR "
            "(action='reopened' AND from_status='resolved' AND to_status='processing')))",
        ),
        comment="反馈提交、回复、解决及重新打开的不可变业务处理历史",
    )
    op.create_index("ix_feedback_events_actor_id_created_at", table, ["actor_id", "created_at"])

    table = "feedback_attachments"
    op.create_table(
        table,
        identifier(),
        column("feedback_id", UUID(), "所属反馈 UUID"),
        column("file_id", UUID(), "已就绪文件的稳定 UUID"),
        column("position", sa.Integer(), "附件展示顺序，当前一次反馈最多三个"),
        timestamp("created_at", "随反馈提交建立关联的时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "feedback_id", "feedback_records"),
        foreign_key(table, "file_id", "files"),
        unique(table, "feedback_id", "file_id"),
        unique(table, "feedback_id", "position"),
        check(table, "position", "position BETWEEN 1 AND 3"),
        comment="原始反馈与共用文件的不可变关联，不复制文件元数据",
    )
    op.create_index("ix_feedback_attachments_file_id", table, ["file_id"])


def create_application_dependencies() -> None:
    """固定应用版本所关联的资源及资料版本，不承担外部运行映射。"""
    table = "application_dependencies"
    op.create_table(
        table,
        identifier(),
        column("version_id", UUID(), "所属正式应用版本 UUID"),
        column("resource_type", sa.String(16), "关联类型：model/dataset/knowledge/tool"),
        column("resource_id", UUID(), "对应模块主表的稳定资源 UUID"),
        column("resource_version_id", UUID(), "目标资源的正式资料版本 UUID"),
        column("position", sa.Integer(), "该应用版本的关联展示顺序，从 1 开始"),
        timestamp("created_at", "应用正式发布时生成关联的时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "version_id", "application_versions"),
        unique(table, "version_id", "resource_type", "resource_id"),
        unique(table, "version_id", "position"),
        check(table, "resource_type", "resource_type IN ('model','dataset','knowledge','tool')"),
        check(table, "position", "position>=1"),
        comment="应用正式版本对模型、数据集、知识库和工具资料版本的不可变引用",
    )
    op.create_index(
        "ix_application_dependencies_target",
        table,
        ["resource_type", "resource_id", "resource_version_id"],
    )


def upgrade() -> None:
    """追加五张已确认表和跨表约束，全部 DDL 在同一迁移事务内完成。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    create_files()
    create_feedback()
    create_application_dependencies()
    op.execute(Path(__file__).with_name("20260921_01_guards.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    """业务历史和文件元数据不自动删除，回退使用迁移前备份。"""
    raise RuntimeError("Feedback relations downgrade requires restoring the pre-migration backup")
