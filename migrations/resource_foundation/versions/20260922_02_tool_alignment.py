"""扩展可信单位、项目范围，并校正明确的工具来源映射。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY

revision = "20260922_02_tool_alignment"
down_revision = "20260922_01_review_needs_changes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """保留指定用户清单，单位与项目使用可信目录编号。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    table = "resource_visibility_policies"
    op.add_column(
        table,
        sa.Column(
            "directory_targets",
            ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
            comment="可信身份目录的单位或项目编号；all/members 时为空",
        ),
    )
    op.drop_constraint("ck_resource_visibility_policies_scope", table, type_="check")
    op.create_check_constraint(
        "ck_resource_visibility_policies_scope",
        table,
        "scope IN ('all','members','organizations','projects')",
    )
    op.execute("""
        CREATE FUNCTION aih_valid_directory_targets(items text[]) RETURNS boolean
        LANGUAGE sql IMMUTABLE AS $$
            SELECT cardinality(items) BETWEEN 1 AND 100
                AND array_ndims(items)=1
                AND NOT EXISTS (SELECT 1 FROM unnest(items) value
                                WHERE value IS NULL OR length(trim(value)) NOT BETWEEN 1 AND 200)
        $$;
    """)
    op.create_check_constraint(
        "ck_resource_visibility_policies_directory_targets",
        table,
        "(scope IN ('all','members') AND cardinality(directory_targets)=0) OR "
        "(scope IN ('organizations','projects') "
        "AND aih_valid_directory_targets(directory_targets))",
    )
    # 仅修正当前不可变快照能明确证明的映射错误，不推测未知来源。
    op.execute("""
        UPDATE ai_tools tool SET source_type='third_party'
        FROM ai_tool_versions version
        WHERE tool.current_version_id=version.id AND tool.source_type='self_developed'
          AND version.snapshot->>'sourceType' IN ('第三方接入','开源集成')
    """)


def downgrade() -> None:
    """存在单位或项目规则时拒绝回退，避免扩大资源访问范围。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("LOCK TABLE resource_visibility_policies IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM resource_visibility_policies
                       WHERE scope IN ('organizations','projects')) THEN
                RAISE EXCEPTION 'Cannot downgrade while directory visibility policies exist';
            END IF;
        END $$;
    """)
    table = "resource_visibility_policies"
    op.drop_constraint("ck_resource_visibility_policies_directory_targets", table, type_="check")
    op.drop_constraint("ck_resource_visibility_policies_scope", table, type_="check")
    op.create_check_constraint(
        "ck_resource_visibility_policies_scope", table, "scope IN ('all','members')"
    )
    op.drop_column(table, "directory_targets")
    op.execute("DROP FUNCTION aih_valid_directory_targets(text[])")
