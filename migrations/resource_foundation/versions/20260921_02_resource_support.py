"""新增模块配置、可见范围与资源文件关系，站内通知暂不建表。"""

from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "20260921_02_resource_support"
down_revision = "20260921_01_feedback_relations"
branch_labels = None
depends_on = None

RESOURCE_TYPES = "'dataset','model','application','knowledge','tool'"


def column(
    name: str,
    column_type: sa.types.TypeEngine,
    comment: str,
    *,
    nullable: bool = False,
    default: str | sa.sql.ClauseElement | None = None,
) -> sa.Column:
    """冻结本次迁移字段及中文说明，不导入未来可能变动的业务模型。"""
    return sa.Column(name, column_type, nullable=nullable, server_default=default, comment=comment)


def identifier() -> sa.Column:
    """生成记录 UUID，保留应用显式提供稳定编号的能力。"""
    return column("id", UUID(), "平台生成的稳定 UUID", default=sa.text("gen_random_uuid()"))


def timestamp(name: str, comment: str) -> sa.Column:
    """创建和更新时间统一使用带时区的数据库时间。"""
    return column(name, sa.DateTime(timezone=True), comment, default=sa.func.now())


def check(table: str, suffix: str, expression: str) -> sa.CheckConstraint:
    """显式命名检查约束，避免重复套用命名约定。"""
    return sa.CheckConstraint(expression, name=sa.schema.conv(f"ck_{table}_{suffix}"))


def foreign_key(table: str, name: str, target: str) -> sa.ForeignKeyConstraint:
    """建立普通外键，不级联删除用户、文件和关联历史。"""
    return sa.ForeignKeyConstraint(
        [name],
        [f"{target}.id"],
        name=sa.schema.conv(f"fk_{table}_{name}_{target}"),
        ondelete="NO ACTION",
    )


def create_configurations() -> None:
    """保存模块的表单和流程配置，详细配置内容仍由服务类型模型校验。"""
    table = "module_configurations"
    op.create_table(
        table,
        identifier(),
        column("module_code", sa.String(32), "模块：datasets/models/applications/knowledge/tools"),
        column("config_key", sa.String(32), "配置类别：form 表单与管理列 / workflow 流程策略"),
        column("value", JSONB(), "通过对应模块类型模型校验的配置对象，不存凭证"),
        column("revision", sa.Integer(), "修改修订号；保存时须匹配旧值并递增", default="1"),
        column("updater_id", UUID(), "最近修改人，系统初始化可空", nullable=True),
        timestamp("created_at", "配置记录创建时间"),
        timestamp("updated_at", "最近成功保存配置的时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "updater_id", "users"),
        sa.UniqueConstraint("module_code", "config_key", name=f"uq_{table}_module_key"),
        check(
            table,
            "module_code",
            "module_code IN ('datasets','models','applications','knowledge','tools')",
        ),
        check(table, "config_key", "config_key IN ('form','workflow')"),
        check(table, "value", "jsonb_typeof(value)='object'"),
        check(table, "revision", "revision>=1"),
        comment="各模块共用的表单、管理展示及流程策略配置，不承接凭证或业务主数据",
    )


def create_visibility() -> None:
    """分别保存资源可见规则和指定用户清单，不固化 Mock 组织或项目目录。"""
    table = "resource_visibility_policies"
    op.create_table(
        table,
        identifier(),
        column(
            "resource_type", sa.String(16), "资源类型：dataset/model/application/knowledge/tool"
        ),
        column("resource_id", UUID(), "对应模块主表的资源 UUID"),
        column("scope", sa.String(16), "all 全部已登录用户 / members 指定用户，须显式设置"),
        column("revision", sa.Integer(), "规则或清单修改时统一递增的修订号", default="1"),
        column("updater_id", UUID(), "最近修改人，系统初始化可空", nullable=True),
        timestamp("created_at", "可见规则创建时间"),
        timestamp("updated_at", "最近一次规则或清单调整时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "updater_id", "users"),
        sa.UniqueConstraint("resource_type", "resource_id", name=f"uq_{table}_resource"),
        check(table, "resource_type", f"resource_type IN ({RESOURCE_TYPES})"),
        check(table, "scope", "scope IN ('all','members')"),
        check(table, "revision", "revision>=1"),
        comment="正式资源的用户可见范围；不代替启用状态、管理权限或外部授权",
    )
    table = "resource_visibility_targets"
    op.create_table(
        table,
        identifier(),
        column("policy_id", UUID(), "所属可见范围规则 UUID"),
        column("user_id", UUID(), "允许查看的本地用户 UUID"),
        timestamp("created_at", "加入指定可见清单的时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "policy_id", "resource_visibility_policies"),
        foreign_key(table, "user_id", "users"),
        sa.UniqueConstraint("policy_id", "user_id", name=f"uq_{table}_policy_user"),
        comment="指定可见范围的用户清单，一名用户一条记录",
    )
    op.create_index("ix_resource_visibility_targets_user_policy", table, ["user_id", "policy_id"])


def create_file_references() -> None:
    """记录申请、审核和正式版本的文件归属，不复制文件元数据。"""
    table = "resource_file_references"
    op.create_table(
        table,
        identifier(),
        column(
            "resource_type", sa.String(16), "资源类型：dataset/model/application/knowledge/tool"
        ),
        column("owner_type", sa.String(16), "归属：publication 申请 / version 版本 / review 审核"),
        column("owner_id", UUID(), "对应申请、版本或审核记录的 UUID"),
        column("file_id", UUID(), "已就绪的原始文件 UUID，不直接引用预览产物"),
        column("purpose", sa.String(32), "用途：tutorial 使用说明 / review_report 审核报告"),
        column(
            "submission_no", sa.Integer(), "仅暂存审核报告必填，匹配当前待审轮次", nullable=True
        ),
        column("creator_id", UUID(), "关联建立人；暂存报告必须是实际上传审核员"),
        timestamp("created_at", "文件引用建立时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "file_id", "files"),
        foreign_key(table, "creator_id", "users"),
        sa.UniqueConstraint(
            "resource_type",
            "owner_type",
            "owner_id",
            "purpose",
            "file_id",
            name=f"uq_{table}_owner_purpose_file",
        ),
        check(table, "resource_type", f"resource_type IN ({RESOURCE_TYPES})"),
        check(table, "owner_type", "owner_type IN ('publication','version','review')"),
        check(table, "purpose", "purpose IN ('tutorial','review_report')"),
        check(table, "version_purpose", "owner_type<>'version' OR purpose='tutorial'"),
        check(
            table,
            "submission_no",
            "(owner_type='publication' AND purpose='review_report' "
            "AND submission_no IS NOT NULL AND submission_no>=1) OR "
            "((owner_type<>'publication' OR purpose<>'review_report') AND submission_no IS NULL)",
        ),
        comment="申请、正式版本及审核记录的文件引用，保护使用说明和审核报告的历史归属",
    )
    op.create_index("ix_resource_file_references_file_id", table, ["file_id"])
    op.create_index(
        "ix_resource_file_references_staged_reports",
        table,
        ["resource_type", "owner_id", "submission_no", "creator_id"],
        postgresql_where=sa.text("owner_type='publication' AND purpose='review_report'"),
    )
    op.create_index(
        "uq_resource_file_references_review_report",
        table,
        ["file_id"],
        unique=True,
        postgresql_where=sa.text("owner_type='review' AND purpose='review_report'"),
    )


def create_previews() -> None:
    """关联原文件与一个可用预览产物，双方元数据仍复用 files。"""
    table = "file_previews"
    op.create_table(
        table,
        identifier(),
        column("source_file_id", UUID(), "被转换的原始文件 UUID"),
        column("preview_file_id", UUID(), "平台生成且已就绪的预览文件 UUID"),
        timestamp("created_at", "预览文件关系建立时间"),
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        foreign_key(table, "source_file_id", "files"),
        foreign_key(table, "preview_file_id", "files"),
        sa.UniqueConstraint("source_file_id", name=f"uq_{table}_source_file_id"),
        sa.UniqueConstraint("preview_file_id", name=f"uq_{table}_preview_file_id"),
        check(table, "different_files", "source_file_id<>preview_file_id"),
        comment="原文件与生成预览的关系，预览访问权限继承原文件的业务归属",
    )


def upgrade() -> None:
    """追加已确认的五张表及历史保护；DDL 在一个迁移事务内完成。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    create_configurations()
    create_visibility()
    create_file_references()
    create_previews()
    op.execute(Path(__file__).with_name("20260921_02_guards.sql").read_text(encoding="utf-8"))


def downgrade() -> None:
    """配置、授权范围和文件历史不自动删除；需要回退时使用迁移前备份。"""
    raise RuntimeError("Resource support downgrade requires restoring the pre-migration backup")
