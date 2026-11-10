"""构造当前会话的基础模型上下文。"""

from __future__ import annotations

from typing import Any

_TODO_GUIDANCE = (
    "For a Todo update or deletion, use the exact ID of the single item clearly "
    "identified by the user's request and the current conversation or tool results. "
    "Never guess. If more than one item could be meant, or the requested change is "
    "unclear, ask the user which item or change they mean before mutating any Todo. "
    "A relative date requested for a Todo can be recorded in its text."
)


class ContextError(Exception):
    """历史消息无法安全地回放。"""


def build_messages(
    history: list[dict[str, Any]], message: str
) -> list[dict[str, Any]]:
    """验证历史工具组，拼接当前用户消息。系统指令只用于模型请求。"""
    index = 0
    while index < len(history):
        entry = history[index]
        role = entry.get("role")
        index += 1
        if role not in ("user", "assistant"):
            raise ContextError("历史包含孤立的工具结果或不支持的消息角色。")
        calls = entry.get("tool_calls") if role == "assistant" else None
        if calls is None:
            continue
        if not isinstance(calls, list):
            raise ContextError("历史工具调用格式无效。")
        ids: set[str] = set()
        for call in calls:
            if not isinstance(call, dict):
                raise ContextError("历史工具调用格式无效。")
            call_id = call.get("id")
            if not isinstance(call_id, str) or not call_id.strip() or call_id in ids:
                raise ContextError("历史工具调用 ID 无效。")
            if call.get("type") != "function":
                raise ContextError("历史工具调用 type 必须为 function。")
            function = call.get("function")
            if not isinstance(function, dict):
                raise ContextError("历史工具调用缺少有效的 function 定义。")
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ContextError("历史工具调用 function.name 必须为非空字符串。")
            if not isinstance(function.get("arguments"), str):
                raise ContextError("历史工具调用 function.arguments 必须为字符串。")
            ids.add(call_id)
            if (
                index >= len(history)
                or history[index].get("role") != "tool"
                or history[index].get("tool_call_id") != call_id
            ):
                raise ContextError("历史工具调用与结果不配对。")
            if not isinstance(history[index].get("content"), str):
                raise ContextError("历史工具结果 content 必须为字符串。")
            index += 1
    return [
        {"role": "system", "content": _TODO_GUIDANCE},
        *history,
        {"role": "user", "content": message},
    ]
