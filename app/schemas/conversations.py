"""对齐前端 ChatInput、ChatAnswer 和历史列表契约。"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field

from app.schemas.overview import OverviewModel

ShortText = Annotated[str, Field(min_length=1, max_length=200)]


class ChatInput(OverviewModel):
    """input 表单字段内的 JSON；模型、知识库和工具仅回显，不执行真实调用。"""

    question: str = Field(min_length=1, max_length=10000, pattern=r"\S")
    model: ShortText
    knowledge_base: str = Field(max_length=200)
    tools: list[ShortText] = Field(default_factory=list, max_length=20)


class AnswerSource(OverviewModel):
    """固定演示引用，不代表真实知识检索结果。"""

    title: str
    location: str
    score: float = Field(ge=0, le=1)


class ChatAnswer(OverviewModel):
    """兼容原前端的最近一轮问答；完整上下文通过 messages 接口读取。"""

    id: str
    question: str
    model: str
    knowledge_base: str
    text: str
    sources: list[AnswerSource]


class ConversationSummary(OverviewModel):
    """历史摘要，time 为 ISO 时间或固定样例说明，按新记录在前展示。"""

    id: str
    title: str
    time: str
    model: str


class NewConversationInput(OverviewModel):
    """新建空对话的初始选项；首次发送问题时可更新模型与知识库。"""

    title: ShortText = "新对话"
    model: ShortText = "AeroLLM-LongCat-2.0"
    knowledge_base: str = Field(default="", max_length=200)
    tools: list[ShortText] = Field(default_factory=list, max_length=20)


class ConversationMessage(OverviewModel):
    """一条用户或助手消息；保留时间及引用，供第三方 API 组装上下文。"""

    id: str
    role: Literal["user", "assistant"]
    content: str
    created_at: datetime
    model: str
    knowledge_base: str
    tools: list[str] = Field(default_factory=list)
    sources: list[AnswerSource] = Field(default_factory=list)


class ConversationRecord(OverviewModel):
    """临时完整会话结构；version 用于拦截并发覆盖，不承担用户权限判断。"""

    id: str
    title: str
    model: str
    knowledge_base: str
    tools: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    messages: list[ConversationMessage] = Field(default_factory=list)
    version: int = Field(default=0, ge=0)


class ChatReply(OverviewModel):
    """回复提供器只返回答案和引用，会话标识与持久化由平台服务管理。"""

    text: str
    sources: list[AnswerSource] = Field(default_factory=list)
