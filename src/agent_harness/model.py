"""DeepSeek Chat Completions 的纯文本与工具协议适配。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, TypedDict

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    AuthenticationError,
    OpenAI,
)

from agent_harness.config import Settings


class ModelError(Exception):
    """模型调用失败。信息已脱敏，不包含 API Key 或原始敏感请求。"""


class ToolCall(TypedDict):
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


class FinalDecision(TypedDict):
    kind: Literal["final"]
    text: str


class ToolCallsDecision(TypedDict):
    kind: Literal["tool_calls"]
    tool_calls: list[ToolCall]
    assistant_message: dict[str, Any]


ModelDecision = FinalDecision | ToolCallsDecision


def _parse_arguments(raw_arguments: str) -> dict[str, Any]:
    try:
        arguments = json.loads(raw_arguments)
        if isinstance(arguments, dict):
            # 标准库解码器也接受 NaN 和 Infinity，需额外排除这些值。
            json.dumps(arguments, allow_nan=False)
            return arguments
    except (ValueError, RecursionError):
        raise ModelError("工具调用参数不是有效的 JSON。") from None
    raise ModelError("工具调用参数必须是 JSON 对象。")


def _parse_tool_calls(calls: list[object], content: str | None) -> ToolCallsDecision:
    parsed_calls: list[ToolCall] = []
    replay_calls: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for call in calls:
        if getattr(call, "type", None) != "function":
            raise ModelError("模型返回了不支持的工具调用类型。")
        call_id = getattr(call, "id", None)
        if not isinstance(call_id, str) or not call_id.strip():
            raise ModelError("工具调用缺少有效的调用 ID。")
        if call_id in seen_ids:
            raise ModelError("模型返回了重复的工具调用 ID。")
        seen_ids.add(call_id)

        function = getattr(call, "function", None)
        name = getattr(function, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise ModelError("工具调用缺少有效的工具名称。")
        raw_arguments = getattr(function, "arguments", None)
        if not isinstance(raw_arguments, str):
            raise ModelError("工具调用缺少 JSON 参数字符串。")

        parsed_calls.append({
            "tool_call_id": call_id,
            "name": name,
            "arguments": _parse_arguments(raw_arguments),
        })
        replay_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": raw_arguments},
        })

    return {
        "kind": "tool_calls",
        "tool_calls": parsed_calls,
        "assistant_message": {
            "role": "assistant",
            "content": content,
            "tool_calls": replay_calls,
        },
    }


def _parse_decision(response: object) -> ModelDecision:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ModelError("模型响应缺少有效的 choices。")
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason not in ("stop", "tool_calls"):
        raise ModelError("模型响应的结束状态不受支持或结果不完整。")
    message = getattr(choice, "message", None)
    if getattr(message, "role", None) != "assistant":
        raise ModelError("模型响应缺少有效的 assistant 消息。")
    if getattr(message, "function_call", None) is not None:
        raise ModelError("模型返回了不支持的旧版 function_call。")

    content = getattr(message, "content", None)
    if content is not None and not isinstance(content, str):
        raise ModelError("模型回复的 content 格式不受支持。")
    calls = getattr(message, "tool_calls", None)
    if calls is not None and not isinstance(calls, list):
        raise ModelError("模型回复的 tool_calls 必须是列表。")
    if finish_reason == "tool_calls":
        if not calls:
            raise ModelError("模型请求工具调用，但未返回工具调用列表。")
        return _parse_tool_calls(calls, content)
    if calls:
        raise ModelError("模型响应的结束状态与工具调用不一致。")
    if not content or not content.strip():
        raise ModelError("模型返回了空内容。")
    return {"kind": "final", "text": content}


class Model:
    """DeepSeek 模型的一次请求适配。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
        )

    def chat(self, text: str) -> str:
        """发送一条用户文本，返回模型回复文本；失败时抛出 ModelError。"""
        try:
            response = self._client.chat.completions.create(
                model=self._settings.model,
                messages=[{"role": "user", "content": text}],
            )
        except AuthenticationError as exc:
            raise ModelError(
                "API Key 无效或无权限，请检查 DEEPSEEK_API_KEY。"
            ) from exc
        except APITimeoutError as exc:
            raise ModelError("请求超时。") from exc
        except APIConnectionError as exc:
            raise ModelError("无法连接模型服务，请检查网络或 BASE_URL。") from exc
        except APIError as exc:
            raise ModelError(f"模型服务返回错误：{type(exc).__name__}") from exc
        except Exception as exc:  # 兜底，避免主流程因未预期异常崩溃
            raise ModelError(f"调用模型时发生未预期错误：{type(exc).__name__}") from exc

        if not response.choices:
            raise ModelError("模型返回了空结果。")
        content = response.choices[0].message.content
        if not content:
            raise ModelError("模型返回了空内容。")
        return content

    def decide(
        self,
        messages: Sequence[Mapping[str, Any]],
        tool_definitions: Sequence[Mapping[str, Any]] = (),
    ) -> ModelDecision:
        """发出一次模型决策请求，返回文本或待执行的工具调用。"""
        try:
            request: dict[str, Any] = {
                "model": self._settings.model,
                "messages": list(messages),
                # DeepSeek 的非思考模式可仅用可见消息继续工具对话。
                "extra_body": {"thinking": {"type": "disabled"}},
            }
            if tool_definitions:
                request["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool["description"],
                            "parameters": tool["parameters_schema"],
                        },
                    }
                    for tool in tool_definitions
                ]
            response = self._client.chat.completions.create(**request)
        except APIError as exc:
            raise ModelError(f"模型决策请求失败（{type(exc).__name__}）。") from None
        except Exception as exc:
            raise ModelError(f"模型决策请求发生未预期错误（{type(exc).__name__}）。") from None

        return _parse_decision(response)
