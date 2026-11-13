"""需要原生对话消息时复用 LangChain 裁剪，并验证工具调用配对。"""

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage, trim_messages
from langchain_core.messages.utils import count_tokens_approximately


def validate_pairs(messages: list[BaseMessage]) -> None:
    """拒绝孤立工具消息或没有结果的调用。

    Args:
        messages: 按时间排序的完整对话。
    """
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, AIMessage):
            if pending:
                raise ValueError("上一轮工具调用尚无结果")
            pending.update(call["id"] for call in message.tool_calls)
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ValueError("孤立 ToolMessage")
            pending.remove(message.tool_call_id)
        elif pending:
            raise ValueError("工具结果未完成就进入下一轮")
    if pending:
        raise ValueError("工具调用缺少结果")


def trim_dialogue(messages: list[BaseMessage], max_tokens: int) -> list[BaseMessage]:
    """从 human 边界裁剪，不能切开工具调用组。

    Args:
        messages: 完整消息。
        max_tokens: 输入预算。
    Returns:
        配对有效的近期消息。
    """
    validate_pairs(messages)
    result = trim_messages(messages, max_tokens=max_tokens, token_counter=count_tokens_approximately,
                           strategy="last", start_on="human", include_system=True, allow_partial=False)
    validate_pairs(result)
    return result
