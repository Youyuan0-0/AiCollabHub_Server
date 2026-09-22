"""为五类资源保留独立的退回修改结论，不改写历史拒绝记录。"""

from pathlib import Path

from alembic import op

revision = "20260922_01_review_needs_changes"
down_revision = "20260921_04_knowledge_external"
branch_labels = None
depends_on = None

PREFIXES = ("dataset", "model", "application", "knowledge_base", "ai_tool")


def _configure(allow_needs_changes: bool) -> None:
    """同步状态、结论、意见约束及审核事务触发器。"""
    decisions = "'approved','rejected'" + (",'needs_changes'" if allow_needs_changes else "")
    for prefix in PREFIXES:
        table = prefix + "_publications"
        name = "ck_" + table + "_review_status"
        op.drop_constraint(name, table, type_="check")
        op.create_check_constraint(name, table, f"review_status IN ('pending',{decisions})")
    op.drop_constraint("ck_resource_reviews_decision", "resource_reviews", type_="check")
    op.create_check_constraint(
        "ck_resource_reviews_decision", "resource_reviews", f"decision IN ({decisions})"
    )
    op.drop_constraint("ck_resource_reviews_comment", "resource_reviews", type_="check")
    op.create_check_constraint(
        "ck_resource_reviews_comment",
        "resource_reviews",
        "decision='approved' OR (comment IS NOT NULL AND length(trim(comment))>0)",
    )
    guards = Path(__file__).with_name("20260922_01_review_guards.sql").read_text(encoding="utf-8")
    if not allow_needs_changes:
        guards = guards.replace(",'needs_changes'", "")
    op.execute(guards)


def upgrade() -> None:
    """扩展允许的审核结论，既有申请和历史记录保持原值。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    _configure(True)


def downgrade() -> None:
    """仅在没有退回修改数据时回退，避免把业务结论改写为拒绝。"""
    op.execute("SET LOCAL lock_timeout='5s'")
    op.execute("SET LOCAL statement_timeout='60s'")
    tables = [prefix + "_publications" for prefix in PREFIXES] + ["resource_reviews"]
    op.execute("LOCK TABLE " + ", ".join(tables) + " IN ACCESS EXCLUSIVE MODE")
    checks = [
        f"EXISTS (SELECT 1 FROM {prefix}_publications WHERE review_status='needs_changes')"
        for prefix in PREFIXES
    ] + ["EXISTS (SELECT 1 FROM resource_reviews WHERE decision='needs_changes')"]
    op.execute(
        "DO $$ BEGIN IF "
        + " OR ".join(checks)
        + " THEN RAISE EXCEPTION 'Cannot downgrade while needs_changes history exists' "
        "USING ERRCODE='23514'; END IF; END $$;"
    )
    _configure(False)
