"""对齐用户端 overview.ts；展示字段保留 camelCase，内部使用 snake_case。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

Percent = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]
Text = Annotated[str, Field(min_length=1, max_length=2000)]
TokenPeriod = Literal["日", "周", "月"]
TokenDimension = Literal["人员", "组织", "应用", "模型"]
RankingType = Literal["用户活跃榜", "应用热度榜", "组织贡献榜"]
RankingPeriod = Literal["日榜", "周榜", "月榜", "总榜"]


class OverviewModel(BaseModel):
    """统一字段别名并拒绝未知字段，避免录入拼写错误被静默忽略。"""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", allow_inf_nan=False
    )


class DisplayMetric(OverviewModel):
    """首页展示指标；value 允许携带 M、k 等单位，不用展示字符串参与计费。"""

    label: Text
    value: Text


class TokenMetric(DisplayMetric):
    """Token 摘要说明需标注统计范围，当前快照不代表当前登录人的实时用量。"""

    description: Text


class ComputeMetric(OverviewModel):
    """算力进度条使用 0～100 的百分数，禁止负数和越界数据。"""

    label: Text
    value: Percent


class ApplicationScenario(OverviewModel):
    """应用场景及其站内入口；不允许发布外部地址或协议相对地址。"""

    name: Text
    description: Text
    path: Text

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """限制场景链接为受支持的站内路径。"""
        # 首页目前仅跳转固定目录；未来资源详情入口需明确加入允许列表。
        if value not in {"/models", "/applications", "/datasets", "/tools", "/knowledge"}:
            raise ValueError("path 必须是已支持的站内资源目录")
        return value


class FeaturedResource(ApplicationScenario):
    """热门资源卡片，type 对应前端展示的资源类型标签。"""

    type: Text


class RecentUpdate(OverviewModel):
    """动态时间为展示文本，建议录入绝对时间，避免缓存后仍显示“刚刚”。"""

    time: Text
    title: Text
    description: Text


class LeaderboardItem(OverviewModel):
    """榜单条目按发布顺序排名；两个指标保留前端需要的展示单位。"""

    name: Text
    tags: list[Text] = Field(max_length=10)
    metric: Text
    secondary: Text
    tone: Literal["dataset", "model", "application", "user"]


class LeaderboardGroup(OverviewModel):
    """固定榜单分类标识，与首页现有页签保持一致。"""

    id: Literal["datasets", "models", "applications", "rated", "popular", "new", "users"]
    label: Text
    items: list[LeaderboardItem] = Field(max_length=100)


class HomeOverview(OverviewModel):
    """首页完整快照；写入时必须提供全部板块，空数组表示主动清空。"""

    home_stats: list[DisplayMetric] = Field(max_length=20)
    token_metrics: list[TokenMetric] = Field(max_length=20)
    compute_metrics: list[ComputeMetric] = Field(max_length=20)
    featured_resources: list[FeaturedResource] = Field(max_length=100)
    application_scenarios: list[ApplicationScenario] = Field(max_length=100)
    recent_updates: list[RecentUpdate] = Field(max_length=100)
    question_suggestions: list[Text] = Field(max_length=20)
    leaderboard_groups: list[LeaderboardGroup] = Field(max_length=7)

    @field_validator("leaderboard_groups")
    @classmethod
    def unique_groups(cls, value: list[LeaderboardGroup]) -> list[LeaderboardGroup]:
        """拒绝首页排行榜中重复的分组标识。"""
        # 重复标识会破坏前端页签选择和列表键值，发布时直接拒绝。
        if len({group.id for group in value}) != len(value):
            raise ValueError("榜单分类 id 不能重复")
        return value


class SummaryMetric(DisplayMetric):
    """详情指标的 note 用于展示占比、对比周期或统计口径。"""

    note: Text


class TokenBreakdown(OverviewModel):
    """维度消耗分布，percent 为条形图宽度，amount 为带单位的消耗量。"""

    name: Text
    amount: Text
    percent: Percent


class TokenAnalysis(OverviewModel):
    """每个统计周期和分析维度分别保存，趋势为当前前端采用的归一化百分数。"""

    token_summary: list[SummaryMetric] = Field(max_length=20)
    trend: list[Percent] = Field(max_length=366)
    token_breakdown: list[TokenBreakdown] = Field(max_length=100)


class ComputeSummary(SummaryMetric):
    """算力摘要颜色只接受前端已经实现的主题。"""

    tone: Literal["blue", "teal", "violet", "amber"]


class ComputePool(OverviewModel):
    """资源池监控快照，jobs 是非负队列作业数，GPU 和显存保留展示单位。"""

    name: Text
    gpu: Text
    memory: Text
    jobs: int = Field(ge=0)
    status: Literal["繁忙", "正常", "空闲"]


class ComputeAnalysis(OverviewModel):
    """算力不随 Token 或排行筛选条件改变，单独更新与读取。"""

    node_loads: list[Literal["high", "medium", "low"]] = Field(max_length=10000)
    compute_summary: list[ComputeSummary] = Field(max_length=20)
    compute_pools: list[ComputePool] = Field(max_length=100)


class BudgetInput(OverviewModel):
    """月度预算输入，金额单位为元；使用率及预警文字由服务端统一计算。"""

    used: float = Field(ge=0, le=1e15)
    limit: float = Field(ge=0, le=1e15)
    warning_threshold: Percent = 85

    @field_validator("used", "limit")
    @classmethod
    def validate_amount(cls, value: float) -> float:
        """拒绝小于一分钱的非零预算金额，避免使用率计算溢出。"""
        # 金额以元录入，非零值至少一分，避免极小预算导致使用率溢出。
        if 0 < value < 0.01:
            raise ValueError("非零金额不能小于 0.01 元")
        return value


class Budget(BudgetInput):
    """预算允许超支，percentage 可超过 100；零预算的特殊情况通过 message 说明。"""

    percentage: float = Field(ge=0)
    message: Text


class Rankings(OverviewModel):
    """运营榜单每行固定为名称和指标两列，服务端保留发布时的排名顺序。"""

    rankings: list[tuple[Text, Text]] = Field(max_length=100)


class OperationsOverview(TokenAnalysis, ComputeAnalysis, Rankings):
    """聚合运营详情，字段与用户端 OperationsOverview 一致。"""

    budget: Budget


class SnapshotMetadata(OverviewModel):
    """元信息区分快照、缺失与演示板块；样例没有真实发布时间，不能视为实时统计。"""

    data_source: Literal["snapshot"] = "snapshot"
    updated_at: dict[str, datetime | None]
    missing_sections: list[str]
    demo_sections: list[str] = Field(default_factory=list)


class HomeOverviewResponse(HomeOverview):
    """兼容现有首页字段，并补充快照来源与更新时间。"""

    metadata: SnapshotMetadata


class OperationsOverviewResponse(OperationsOverview):
    """兼容现有运营字段，并分别报告各板块是否已有数据。"""

    metadata: SnapshotMetadata


class SnapshotReceipt(OverviewModel):
    """成功发布快照后返回所属板块、筛选键和 UTC 更新时间。"""

    section: str
    variant: str
    updated_at: datetime
