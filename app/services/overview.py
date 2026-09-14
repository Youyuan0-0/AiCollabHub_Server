"""看板快照读写与聚合；非生产固定样例来自独立数据文件，已发布快照优先。"""

import logging
from datetime import UTC, datetime

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.overview import OverviewSnapshot
from app.schemas.overview import (
    Budget,
    BudgetInput,
    ComputeAnalysis,
    HomeOverview,
    HomeOverviewResponse,
    OperationsOverviewResponse,
    OverviewModel,
    Rankings,
    SnapshotMetadata,
    SnapshotReceipt,
    TokenAnalysis,
)
from app.services.overview_demo import demo_snapshot

logger = logging.getLogger(__name__)


def read_snapshots(session: Session, keys: dict[str, str]) -> dict[str, OverviewSnapshot]:
    """keys 为板块到筛选键的映射；一次查询返回已有快照，未录入板块不补造数据。"""
    try:
        rows = session.scalars(
            select(OverviewSnapshot).where(
                # 使用普通逻辑条件，避免依赖特定数据库的元组 IN 语法。
                or_(
                    *(
                        and_(
                            OverviewSnapshot.section == section, OverviewSnapshot.variant == variant
                        )
                        for section, variant in keys.items()
                    )
                )
            )
        ).all()
    except SQLAlchemyError as exc:
        logger.warning("Overview snapshot read failed (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "overview_unavailable") from None
    return {row.section: row for row in rows}


def snapshot_data[T: OverviewModel](
    rows: dict[str, OverviewSnapshot],
    section: str,
    model: type[T],
    empty: T,
    *,
    demo_variant: str | None = None,
) -> T:
    """已发布快照优先；仅缺失时按 demo_variant 读取样例，损坏快照仍返回 503。"""
    row = rows.get(section)
    if row is None:
        if demo_variant is not None:
            return demo_snapshot(section, model, demo_variant)
        return empty
    try:
        return model.model_validate_json(row.payload)
    except ValidationError:
        logger.warning("Invalid overview snapshot (%s)", section)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "overview_snapshot_invalid"
        ) from None


def metadata(
    rows: dict[str, OverviewSnapshot], keys: dict[str, str], *, use_demo: bool = False
) -> SnapshotMetadata:
    """样例板块单独标注且不伪造发布时间；有展示内容时不再计入缺失清单。"""
    missing = [section for section in keys if section not in rows]
    return SnapshotMetadata(
        updated_at={
            section: rows[section].updated_at.replace(tzinfo=UTC) if section in rows else None
            for section in keys
        },
        missing_sections=[] if use_demo else missing,
        demo_sections=missing if use_demo else [],
    )


def get_home(session: Session, *, use_demo: bool = False) -> HomeOverviewResponse:
    """返回完整首页；开发缺失时展示原前端样例，生产缺失时保持未知计数和空数组。"""
    keys = {"home": "default"}
    rows = read_snapshots(session, keys)
    empty = HomeOverview(
        home_stats=[
            {"label": label, "value": "—"} for label in ("智能体数量", "高质量数据集", "模型数量")
        ],
        token_metrics=[],
        compute_metrics=[],
        featured_resources=[],
        application_scenarios=[],
        recent_updates=[],
        question_suggestions=[],
        leaderboard_groups=[],
    )
    data = snapshot_data(
        rows, "home", HomeOverview, empty, demo_variant="default" if use_demo else None
    )
    return HomeOverviewResponse(
        **data.model_dump(), metadata=metadata(rows, keys, use_demo=use_demo)
    )


def budget_summary(data: BudgetInput, *, available: bool) -> Budget:
    """按已用金额与月度限额计算预算；零限额避免除零，超支不裁剪使用率。"""
    percentage = round(data.used / data.limit * 100, 2) if data.limit else 0
    if not available:
        message = "暂无预算数据"
    elif data.limit == 0:
        message = "预算额度为零，已有支出" if data.used else "尚未设置预算额度"
    elif data.used > data.limit:
        message = "当前消耗已超出预算"
    elif percentage >= data.warning_threshold:
        message = "当前消耗已达到预算预警阈值"
    else:
        message = "当前消耗在预算范围内"
    return Budget(**data.model_dump(), percentage=percentage, message=message)


def get_operations(
    session: Session,
    token_period: str,
    token_dimension: str,
    ranking_type: str,
    ranking_period: str,
    *,
    use_demo: bool = False,
) -> OperationsOverviewResponse:
    """精确查询已发布组合；use_demo 仅让缺失板块采用固定样例，不覆盖主动清空的内容。"""
    keys = {
        "token": f"{token_period}:{token_dimension}",
        "compute": "default",
        "budget": "default",
        "rankings": f"{ranking_type}:{ranking_period}",
    }
    rows = read_snapshots(session, keys)
    token = snapshot_data(
        rows,
        "token",
        TokenAnalysis,
        TokenAnalysis(token_summary=[], trend=[], token_breakdown=[]),
        demo_variant=token_period if use_demo else None,
    )
    compute = snapshot_data(
        rows,
        "compute",
        ComputeAnalysis,
        ComputeAnalysis(node_loads=[], compute_summary=[], compute_pools=[]),
        demo_variant="default" if use_demo else None,
    )
    rankings = snapshot_data(
        rows,
        "rankings",
        Rankings,
        Rankings(rankings=[]),
        demo_variant=ranking_type if use_demo else None,
    )
    budget = snapshot_data(
        rows,
        "budget",
        BudgetInput,
        BudgetInput(used=0, limit=0),
        demo_variant="default" if use_demo else None,
    )
    return OperationsOverviewResponse(
        **token.model_dump(),
        **compute.model_dump(),
        **rankings.model_dump(),
        budget=budget_summary(budget, available="budget" in rows or use_demo),
        metadata=metadata(rows, keys, use_demo=use_demo),
    )


def save_snapshot(
    session: Session, section: str, variant: str, data: OverviewModel
) -> SnapshotReceipt:
    """完整替换指定板块快照并提交事务；同一筛选键重复更新不会新增记录。"""
    now = datetime.now(UTC)
    try:
        row = session.get(OverviewSnapshot, (section, variant))
        if row is None:
            row = OverviewSnapshot(section=section, variant=variant)
            session.add(row)
        row.payload = data.model_dump_json(by_alias=True)
        row.updated_at = now.replace(tzinfo=None)
        session.commit()
    except IntegrityError:
        # 两个请求同时首次创建同一快照时，由唯一键拦截；调用方可重试完整 PUT。
        session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "overview_write_conflict") from None
    except SQLAlchemyError as exc:
        session.rollback()
        logger.warning("Overview snapshot write failed (%s)", type(exc).__name__)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "overview_unavailable") from None
    return SnapshotReceipt(section=section, variant=variant, updated_at=now)
