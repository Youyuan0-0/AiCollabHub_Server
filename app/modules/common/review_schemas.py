"""资源审核共用的评分字段；审核结论和必填条件由各业务模块定义。"""

from pydantic import ConfigDict, Field

from app.modules.common.schemas import Payload


class ReviewQualityScores(Payload):
    """七项质量评分均为 1 至 5 分整数，不设置及格线，不接受额外评分字段。"""

    model_config = ConfigDict(extra="forbid")
    format_compliance: int = Field(ge=1, le=5, strict=True)
    safety_compliance: int = Field(ge=1, le=5, strict=True)
    annotation_compliance: int = Field(ge=1, le=5, strict=True)
    structural_completeness: int = Field(ge=1, le=5, strict=True)
    authenticity: int = Field(ge=1, le=5, strict=True)
    consistency: int = Field(ge=1, le=5, strict=True)
    cleanliness: int = Field(ge=1, le=5, strict=True)
