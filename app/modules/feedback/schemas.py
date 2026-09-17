"""反馈契约及前后端共用的枚举和文件限制。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from app.modules.common.schemas import PageQuery, Payload

FEEDBACK_TYPES = {
    "function_error": "功能异常",
    "experience_suggestion": "体验建议",
    "resource_issue": "资源问题",
    "other": "其他",
}
FEEDBACK_MODULES = {
    "home": "首页 / 导航",
    "datasets": "数据集",
    "models": "模型库",
    "applications": "场景应用",
    "knowledge": "知识库",
    "tools": "AI 工具",
    "other": "其他",
}
FILE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_ATTACHMENTS = 3
MAX_FILE_BYTES = 1024**3
TEMPORARY_SECONDS = 24 * 3600
OBJECT_PREFIX = "feedback/attachments/"


class FeedbackInput(Payload):
    """原始反馈内容；账号归属只从后端登录会话读取。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    type: Literal["function_error", "experience_suggestion", "resource_issue", "other"]
    module: Literal["home", "datasets", "models", "applications", "knowledge", "tools", "other"]
    type_other: str | None = Field(default=None, max_length=200)
    module_other: str | None = Field(default=None, max_length=200)
    title: str = Field(min_length=1, max_length=60)
    description: str = Field(min_length=1, max_length=5000)
    contact: str = Field(min_length=1, max_length=200)
    attachment_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    client_request_id: UUID

    @model_validator(mode="after")
    def validate_other(self) -> Self:
        """选择其他时说明必填，并拒绝重复的附件编号。"""
        for value, explanation in ((self.type, self.type_other), (self.module, self.module_other)):
            if value == "other" and not explanation:
                raise ValueError("选择其他时请补充说明")
            if value != "other" and explanation:
                raise ValueError("仅选择其他时填写补充说明")
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError("请勿重复关联同一附件")
        self.type_other = self.type_other or None
        self.module_other = self.module_other or None
        return self


class FeedbackUpdate(Payload):
    """每次处理追加一条说明，带上版本和稳定请求编号。"""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    status: Literal["processing", "resolved"]
    reply: str = Field(min_length=1, max_length=5000)
    expected_version: int = Field(ge=1)
    client_request_id: UUID


class FeedbackQuery(PageQuery):
    """反馈分页及当前状态筛选。"""

    status: Literal["all", "open", "processing", "resolved"] = "all"
