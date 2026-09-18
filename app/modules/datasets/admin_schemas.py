"""数据集后台查询与审核契约，状态和评分均由服务端校验。"""

from datetime import date
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from app.modules.applications.publication_schemas import ApplicationVisibility
from app.modules.common.schemas import CatalogQuery, PageQuery, Payload

DatasetDecision = Literal["approved", "rejected", "needs_changes"]
DATASET_STATUSES = ("draft", "pending", "approved", "rejected", "needs_changes")
Score = Annotated[int, Field(ge=1, le=5, strict=True)]


class DatasetListing(Payload):
    """独立管理目录上下架；revision 为当前状态修订号，0 表示尚未手动调整。"""

    model_config = ConfigDict(extra="forbid")
    status: Literal["listed", "unlisted"]
    revision: int = Field(ge=0, strict=True)


class DatasetAdminQuery(CatalogQuery):
    """后台目录支持上下架筛选；空值同时返回已上架和已下架资源。"""

    listing_status: Literal["", "listed", "unlisted"] = Field(default="", alias="listingStatus")


class DatasetVisibility(ApplicationVisibility):
    """沿用场景应用的四种范围；必须携带修订号，0 表示尚未保存过配置。"""

    revision: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_targets(self) -> Self:
        """指定范围必须有目标；全部用户不保存之前残留的目标标识。"""
        if self.scope != "all" and not self.targets:
            raise ValueError("请选择或填写组织、成员或项目组标识")
        if self.scope == "all":
            self.targets = []
        return self


class DatasetScores(Payload):
    """管理员填写的七维质量评分，每项为 1–5 分整数；旧三项仅保留在历史快照中。"""

    model_config = ConfigDict(extra="forbid")
    format_compliance: Score
    safety_compliance: Score
    annotation_compliance: Score
    structural_completeness: Score
    authenticity: Score
    consistency: Score
    cleanliness: Score


class ReviewDataset(Payload):
    """审核指定修订的申请；通过必填评分，退回必须说明原因。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: DatasetDecision
    reason: str = Field(default="", max_length=2000)
    revision: int = Field(ge=1, strict=True)
    scores: DatasetScores | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        """拦截缺少依据的审核，避免前端绕过必填规则。"""
        if self.decision == "approved" and self.scores is None:
            raise ValueError("通过前请完成七项质量评分")
        if self.decision != "approved" and not self.reason:
            raise ValueError("驳回或要求补充材料时，请填写原因")
        return self


class DatasetReviewQuery(PageQuery):
    """审核队列按提交日期筛选；空状态包含草稿，日期包含起止当天（UTC）。"""

    status: Literal["", "draft", "pending", "approved", "rejected", "needs_changes"] = "pending"
    sort: Literal["updated", "submitted"] = "updated"
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        """拒绝倒置日期范围，不把非法查询误显示为空列表。"""
        if self.start and self.end and self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class DatasetAuditQuery(PageQuery):
    """按当次快照、审核员和审核日期检索；日期包含起止当天（UTC）。"""

    decision: Literal["", "approved", "rejected", "needs_changes"] = ""
    reviewer: str = Field(default="", max_length=200)
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> Self:
        """拒绝倒置的审核日期范围，避免错误条件表现为空记录。"""
        if self.start and self.end and self.start > self.end:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class PublishDataset(Payload):
    """管理员补发已审核申请，修订号防止旧页面发布已经变化的资料。"""

    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=1, strict=True)
