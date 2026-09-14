"""第三方对话调用扩展点；当前默认使用原样回显，不内置供应方协议和密钥。"""

from typing import Protocol

from app.schemas.conversations import ChatReply, ConversationMessage


class ChatProvider(Protocol):
    """实现 reply 即可接入第三方；messages 包含历史及当前问题，返回统一回复结构。"""

    def reply(
        self,
        messages: list[ConversationMessage],
        *,
        model: str,
        knowledge_base: str,
        tools: list[str],
    ) -> ChatReply: ...


class EchoChatProvider:
    """临时回复提供器，原样返回最后一条用户消息，保留换行与空格。"""

    def reply(
        self,
        messages: list[ConversationMessage],
        *,
        model: str,
        knowledge_base: str,
        tools: list[str],
    ) -> ChatReply:
        # 服务层确保消息非空且最后一条是当前问题，不生成模拟引用。
        return ChatReply(text=messages[-1].content)
