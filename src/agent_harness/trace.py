"""有界的请求与工具结构化日志。"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from agent_harness.tools.registry import TOOL_STATE_UNCERTAIN, ToolResult

TRACE_LOGGER_NAME = "agent_harness.runtime"
_logger = logging.getLogger(TRACE_LOGGER_NAME)
_LABEL = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_SECRET = re.compile(r"sk-[A-Za-z0-9_-]{12,}|gh[pousr]_|github_pat_|api[_-]?key|authorization", re.I)
_PARAMETER_FIELDS = ("action", "todo_id", "content", "expression", "query")
_TODO_ACTIONS = ("create", "get", "list", "update", "delete")


def safe_label(value: str) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value) or _SECRET.search(value):
        return "[redacted]"
    return value


def _parameters(arguments: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key in _PARAMETER_FIELDS:
        if key not in arguments:
            continue
        value = arguments[key]
        if key == "action" and isinstance(value, str) and value in _TODO_ACTIONS:
            fields[key] = value
        elif isinstance(value, str):
            fields[key] = {"type": "string", "chars": len(value)}
        else:
            fields[key] = {"type": type(value).__name__}
    return {"fields": fields, "other_field_count": len(arguments) - len(fields)}


class RequestTrace:
    def __init__(self, session_id: str | None) -> None:
        self._session_id = safe_label(session_id) if session_id is not None else "[redacted]"
        self._request_id = uuid4().hex
        self._sequence = 0
        self.model_calls = 0
        self.successful_tools = 0

    def start(self, verified_session_id: str | None = None) -> None:
        if self._sequence:
            return
        if verified_session_id is not None:
            self._session_id = safe_label(verified_session_id)
        self.emit("request_started")

    def emit(self, event: str, *, tool: dict[str, Any] | None = None, **details: Any) -> None:
        self._sequence += 1
        payload = {
            "event": event,
            "session_id": self._session_id,
            "request_id": self._request_id,
            "sequence": self._sequence,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            **(tool or {}),
            **details,
        }
        prefix = "tool_trace" if tool is not None else "request_trace"
        encoded = json.dumps(payload, allow_nan=False)
        _logger.info("%s %s", prefix, encoded, extra={"trace_payload": encoded})

    def tool_requested(
        self, decision: int, position: int, registered_name: str | None, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        tool = {
            # Trace uses its own request-local identifier; protocol IDs stay in Messages.
            "tool_call_id": f"call-{decision}-{position}",
            "tool_name": safe_label(registered_name) if registered_name is not None else "unknown_tool",
            "decision": decision,
            "tool_index": position,
            "proposed": True,
        }
        self.emit("tool_requested", tool=tool, started=False, parameters=_parameters(arguments))
        return tool

    def tool_result(self, tool: dict[str, Any], started: bool, result: ToolResult) -> None:
        if result["ok"]:
            self.successful_tools += 1
            data = result["result"]
            summary: dict[str, Any] = {"field_count": len(data)}
            value = data.get("value")
            if type(value) in (int, float) and abs(value) <= 10**15:
                summary["value"] = value
            if data.get("recovered") is True:
                summary["recovered"] = True
            for key in ("todos", "results"):
                if isinstance(data.get(key), list):
                    summary[f"{key}_count"] = len(data[key])
            self.emit(
                "tool_result", tool=tool, started=started, ok=True, status="succeeded",
                result_summary=summary,
            )
            return

        code = result["error"]["code"]
        if code == TOOL_STATE_UNCERTAIN:
            status = "unconfirmed"
        elif started:
            status = "failed"
        elif code in ("limit_reached", "tool_skipped"):
            status = "skipped"
        else:
            status = "rejected"
        self.emit(
            "tool_result", tool=tool, started=started, ok=False, status=status,
            error_code=safe_label(code),
        )
