"""日志查询参数和管理端展示契约。"""

from datetime import datetime, timedelta
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.modules.common.auth.models import ActorType, OperationAction, OperationTarget
from app.modules.common.schemas import PageQuery, Payload
from app.modules.common.system_logs import SystemLogLevel

ACTION_LABELS = {
    "login": "登录",
    "logout": "退出",
    "single_logout": "单点登出",
    "create": "新增",
    "update": "修改",
    "delete": "删除",
    "enable": "启用",
    "disable": "停用",
    "grant": "授权",
    "revoke": "撤权",
    "sync": "身份同步",
    "invoke": "调用",
    "upload": "上传",
    "download": "下载",
    "review": "审核",
    "configure": "配置",
}
TARGET_LABELS = {
    "user": "用户",
    "session": "登录会话",
    "identity_sync": "身份同步",
    "dataset": "数据集",
    "model": "模型",
    "application": "场景应用",
    "knowledge_base": "知识库",
    "tool": "AI 工具",
    "conversation": "会话",
    "feedback": "反馈",
    "file": "文件",
    "platform": "平台",
}
LEVEL_LABELS = {
    "info": "INFO",
    "warning": "WARNING",
    "error": "ERROR",
}
SOURCE_LABELS = {
    "auth": "单点登录",
    "identity_sync": "身份同步",
    "database": "数据库",
    "db": "数据库事务",
    "cache": "缓存",
    "storage": "文件存储",
    "http": "接口服务",
    "feedback": "反馈处理",
    "applications": "场景应用",
    "datasets": "数据集",
    "models": "模型库",
    "knowledge": "知识库",
    "tools": "AI 工具",
    "common": "公共服务",
    "services": "业务服务",
    "main": "应用服务",
    "core": "运行基础",
}


class LogQuery(PageQuery):
    """时间必须带时区，范围最多 90 天；两类日志统一支持正倒序分页。"""

    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=10, ge=1, le=100, alias="pageSize")
    start_at: AwareDatetime | None = Field(default=None, alias="startAt")
    end_at: AwareDatetime | None = Field(default=None, alias="endAt")
    sort_order: Literal["asc", "desc"] = Field(default="desc", alias="sortOrder")

    @model_validator(mode="after")
    def validate_time_range(self) -> Self:
        """拒绝半个时间区间和过长范围，默认时间由查询仓储统一补齐。"""
        self.q = self.q.strip()
        if (self.start_at is None) != (self.end_at is None):
            raise ValueError("开始时间和结束时间须同时提供")
        if self.start_at is not None and self.end_at is not None:
            if self.start_at > self.end_at:
                raise ValueError("开始时间不能晚于结束时间")
            if self.end_at - self.start_at > timedelta(days=90):
                raise ValueError("单次查询时间范围不能超过 90 天")
        return self


class OperationLogQuery(LogQuery):
    """按表中动作、对象枚举及实际操作人筛选，支持稳定的正倒序分页。"""

    action: OperationAction | None = None
    target_type: OperationTarget | None = Field(default=None, alias="targetType")
    operator: Literal["admin", "iam"] | None = None


class SystemLogQuery(LogQuery):
    """按日志级别及来源模块筛选技术事件。"""

    level: SystemLogLevel | None = None
    source: str | None = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
    request_id: UUID | None = Field(default=None, alias="requestId")


class OperationLogItem(Payload):
    """列表使用当前用户显示名；编号保持原始日志关联，不表示历史姓名快照。"""

    id: UUID
    actor_type: ActorType
    actor_name: str
    user_id: UUID | None
    action: OperationAction
    target_type: OperationTarget
    target_id: str | None
    summary: str
    request_id: UUID | None
    created_at: datetime


class SystemLogItem(Payload):
    """中文摘要用于列表，脱敏后的原始消息保留用于技术核对。"""

    id: UUID
    level: SystemLogLevel
    source: str
    event: str
    message: str
    summary: str
    request_id: UUID | None
    created_at: datetime


class SystemLogDetail(SystemLogItem):
    """详情返回脱敏后的技术诊断上下文，保留原始记录供运维核对。"""

    details: dict[str, JsonValue]


class LogPage[Item](Payload):
    """统一分页结构，时间与编号共同保证相同时间下顺序稳定。"""

    items: list[Item]
    total: int
    page: int
    page_size: int


class LogOption(Payload):
    """日志筛选枚举的编码及中文标签。"""

    value: str
    label: str


class LogOptions(Payload):
    """两类日志共用的筛选和展示标签。"""

    actions: list[LogOption]
    target_types: list[LogOption]
    operators: list[LogOption]
    levels: list[LogOption]
    sources: list[LogOption]
