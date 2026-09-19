from __future__ import annotations

from datetime import date
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from app.modules.applications.publication_schemas import ApplicationPayload
from app.modules.common.schemas import PageQuery


class DemoSessionInput(ApplicationPayload):
    """演示身份切换参数，明确区分用户端和管理端。"""

    actor_id: str = Field(max_length=80)
    audience: Literal["user", "admin"] = "user"


class InteractionInput(ApplicationPayload):
    """互动状态的部分更新，空值表示本次不修改对应状态。"""

    favorite: bool | None = None
    subscribed: bool | None = None
    rating: int | None = Field(default=None, ge=1, le=5, strict=True)
    comment: str = Field(default="", max_length=1000)


class RunInput(ApplicationPayload):
    """应用运行请求，包含幂等编号、结构化输入和运行选项。"""

    request_id: str = Field(min_length=8, max_length=100)
    input: dict = Field(max_length=50)
    options: dict[str, str] = Field(default_factory=dict, max_length=20)
    simulate_failure: bool = False


class SyncInput(ApplicationPayload):
    """指定工具链资源和版本的幂等同步请求。"""

    external_id: str = Field(min_length=1, max_length=100)
    external_version: str = Field(min_length=1, max_length=100)
    request_id: str = Field(min_length=8, max_length=100)
    simulate_failure: bool = False


class WorkflowPolicy(ApplicationPayload):
    """应用展示、质量检查、通知与运行告警策略。"""

    revision: int = Field(ge=1)
    toolchain_url: str = Field(default="", max_length=2000)
    announcement: str = Field(default="", max_length=1000)
    recommended_ids: list[str] = Field(default_factory=list, max_length=12)
    hot_categories: list[str] = Field(default_factory=list, max_length=5)
    default_sort: Literal["recommended", "rating", "recent_download", "downloads", "favorites"] = (
        "recommended"
    )
    page_size: Literal[6, 12, 24] = 12
    sensitive_terms: list[str] = Field(default_factory=lambda: ["演示拦截词"], max_length=100)
    # 兼容旧客户端提交的配置；不再返回、保存或用作通过门槛。
    minimum_quality_score: int | None = Field(default=None, ge=1, le=5, exclude=True)
    notifications_enabled: bool = True
    notification_template: str = Field(default="{name}：{status}", max_length=300)
    failure_threshold: int = Field(default=3, ge=1, le=100)

    @field_validator("default_sort")
    @classmethod
    def normalize_default_sort(cls, value: str) -> str:
        """将旧评分排序配置归并到综合排序，保留旧客户端提交的兼容性。"""
        return "recommended" if value == "rating" else value

    @field_validator("toolchain_url")
    @classmethod
    def toolchain_url_valid(cls, value: str) -> str:
        """校验可选工具链入口地址，沿用教程链接的安全规则。"""
        if not value:
            return value
        from app.modules.applications.publication_schemas import Tutorial

        return Tutorial.safe_url(value)

    @field_validator("sensitive_terms")
    @classmethod
    def terms(cls, values: list[str]) -> list[str]:
        """校验敏感词长度并清理空白和重复词条。"""
        if any(not item.strip() or len(item) > 80 for item in values):
            raise ValueError("敏感词需为1至80字")
        return list(dict.fromkeys(item.strip() for item in values))

    @field_validator("notification_template")
    @classmethod
    def template(cls, value: str) -> str:
        """通知模板只允许 name 和 status 占位符，拒绝格式扩展。"""
        from string import Formatter

        try:
            for _, name, spec, conversion in Formatter().parse(value):
                if name is not None and (name not in ("name", "status") or spec or conversion):
                    raise ValueError
        except ValueError:
            raise ValueError("通知模板只支持 {name} 和 {status}") from None
        return value


class HistoryQuery(PageQuery):
    """审核历史的审核人、结论和日期范围筛选参数。"""

    reviewer: str = ""
    decision: Literal["", "approved", "rejected", "needs_changes"] = ""
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def dates(self) -> Self:
        """校验审核历史筛选的开始日期不晚于结束日期。"""
        if self.start and self.end and self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        return self
