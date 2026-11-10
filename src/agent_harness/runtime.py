"""单次请求内协调模型与工具。"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, TypedDict

from agent_harness.model import Model, ModelError
from agent_harness.tools import ToolManager, ToolResult
from agent_harness.tools.registry import TOOL_STATE_UNCERTAIN

_MAX_DECISIONS = 8
_API_KEY_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{12,}")
_logger = logging.getLogger(__name__)


class RuntimeResult(TypedDict):
    session_id: str
    answer: str
    status: Literal["completed", "failed", "limit_reached"]


def _log_tool_result(
    session_id: str, call_id: str, name: str, started: bool, result: ToolResult
) -> None:
    event: dict[str, Any] = {
        "session_id": session_id,
        "tool_call_id": call_id,
        "tool_name": name,
        "started": started,
        "ok": result["ok"],
    }
    if result["ok"]:
        data = result["result"]
        summary = {"field_count": len(data)}
        value = data.get("value")
        if type(value) in (int, float):
            summary["value"] = value
        if data.get("recovered") is True:
            summary["recovered"] = True
        event["result_summary"] = summary
    else:
        event["error_code"] = result["error"]["code"]
    # 参数和自由文本不进入 Trace。
    encoded = json.dumps(event, allow_nan=False)
    _logger.info("tool_trace %s", _API_KEY_PATTERN.sub("[redacted]", encoded))


class Runtime:
    def __init__(self, model: Model, tools: ToolManager) -> None:
        self._model = model
        self._tools = tools

    def run(self, message: str, session_id: str) -> RuntimeResult:
        """使用调用方提供的可信会话标识处理一条消息。"""
        messages: list[dict[str, Any]] = [{"role": "user", "content": message}]
        definitions = self._tools.public_definitions()
        started_count = 0

        for turn in range(1, _MAX_DECISIONS + 1):
            try:
                decision = self._model.decide(messages, definitions)
            except ModelError:
                return {
                    "session_id": session_id,
                    "answer": f"模型请求失败，未获得最终回答。此前已启动 {started_count} 次工具调用。",
                    "status": "failed",
                }

            if decision["kind"] == "final":
                return {
                    "session_id": session_id,
                    "answer": decision["text"],
                    "status": "completed",
                }

            messages.append(decision["assistant_message"])
            uncertain = False
            for call in decision["tool_calls"]:
                call_id = call["tool_call_id"]
                name = call["name"]
                if uncertain:
                    started = False
                    result: ToolResult = {
                        "ok": False,
                        "error": {"code": "tool_skipped", "message": "前一工具的状态无法核实。"},
                    }
                elif turn == _MAX_DECISIONS:
                    # 预算已耗尽，没有下一轮模型请求可以接收工具结果。
                    started = False
                    result = {
                        "ok": False,
                        "error": {"code": "limit_reached", "message": "模型决策预算已耗尽。"},
                    }
                else:
                    started, result = self._tools.execute_with_start_status(
                        name, call["arguments"], session_id=session_id
                    )
                started_count += started
                _log_tool_result(session_id, call_id, name, started, result)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(result, allow_nan=False),
                })
                if not result["ok"] and result["error"]["code"] == TOOL_STATE_UNCERTAIN:
                    uncertain = True

            if uncertain:
                return {
                    "session_id": session_id,
                    "answer": "工具写入状态无法核实，已停止本次请求。此前的操作未回滚。",
                    "status": "failed",
                }

        return {
            "session_id": session_id,
            "answer": (
                f"已达到 {_MAX_DECISIONS} 次模型决策上限，未获得最终回答。"
                f"此前已启动 {started_count} 次工具调用。"
            ),
            "status": "limit_reached",
        }
