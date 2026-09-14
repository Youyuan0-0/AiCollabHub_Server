from pydantic import Field

from app.modules.common.schemas import Payload


class ChatInput(Payload):
    question: str = Field(min_length=1, max_length=10000)
    model: str = "AeroLLM-LongCat-2.0"
    knowledge_base: str = "航空发动机技术文档"
    tools: list[str] = Field(default_factory=list, max_length=20)
    filenames: list[str] = Field(default_factory=list, max_length=10)
