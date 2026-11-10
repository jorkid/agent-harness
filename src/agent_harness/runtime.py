"""单次请求内协调模型与工具。"""

from __future__ import annotations

import json
import logging
import re
from threading import Lock
from typing import Any, Literal, TypedDict

from agent_harness.context import (
    MAX_USER_CHARS,
    ContextError,
    bound_final_answer,
    build_messages,
    check_request_size,
    format_tool_result,
    memory_candidates,
    memory_request,
    parse_memory,
    parse_summary,
    split_for_compaction,
    summary_request,
)
from agent_harness.model import Model, ModelError
from agent_harness.tools import ToolManager, ToolResult
from agent_harness.session import SessionConflictError, SessionError, SessionState, SessionStore
from agent_harness.tools.registry import TOOL_STATE_UNCERTAIN, ToolError
from agent_harness.tools.todo import TodoItem, TodoStore, create_todo_tool

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
    def __init__(
        self, model: Model | None, tools: ToolManager, *, sessions: SessionStore | None = None
    ) -> None:
        self._model = model
        self._tools = tools
        self._sessions = sessions
        self._run_lock = Lock()
        self._todos = None
        if sessions is not None:
            self._todos = TodoStore(commit=self._commit_todos)
            tools.register(create_todo_tool(self._todos))

    def _session_store(self) -> SessionStore:
        if self._sessions is None:
            raise SessionError("未配置会话存储。")
        return self._sessions

    def create_session(self) -> str:
        return self._session_store().create()

    def restore_session(self, session_id: str) -> SessionState:
        return self._session_store().load(session_id)

    def _commit_todos(
        self, session_id: str, previous_todos: list[TodoItem], todos: list[TodoItem]
    ) -> bool:
        store = self._session_store()
        try:
            before = store.load(session_id)
        except SessionError:
            raise ToolError("todo_save_failed", "保存前无法读取会话，未开始写入。") from None

        if before["todos"] != previous_todos:
            raise ToolError("todo_save_conflict", "会话待办已变更，未写入过期快照。")

        expected: SessionState = {**before, "todos": todos}
        recovered = False
        try:
            store.save(session_id, expected, expected=before)
        except SessionConflictError as exc:
            raise ToolError("todo_save_conflict", str(exc)) from None
        except Exception:
            recovered = True

        try:
            observed = store.load(session_id)
        except Exception:
            raise ToolError(TOOL_STATE_UNCERTAIN, "无法读取会话 JSON 核实写入。") from None
        if observed == expected:
            return recovered
        if observed == before:
            raise ToolError("todo_save_failed", "已核实会话 JSON 未发生变更。")
        raise ToolError(TOOL_STATE_UNCERTAIN, "会话 JSON 与预期写入状态不一致。")

    def run(self, message: str, session_id: str) -> RuntimeResult:
        """使用调用方提供的可信会话标识处理一条消息。"""
        if len(message) > MAX_USER_CHARS:
            return {
                "session_id": session_id,
                "answer": f"用户消息超过 {MAX_USER_CHARS} 字符限制。",
                "status": "failed",
            }
        if self._todos is None:
            result, _ = self._run([{"role": "user", "content": message}], session_id)
            return result
        # 同一个 Runtime 串行加载和更新会话，避免覆盖另一个请求的快照。
        with self._run_lock:
            state = self.restore_session(session_id)
            try:
                self._todos.restore(session_id, state["todos"])
            except ToolError as exc:
                raise SessionError(str(exc)) from None
            history = state["messages"]
            try:
                messages = build_messages(
                    history, message, summary=state["summary"], memory=state["memory"]
                )
            except ContextError as exc:
                return {"session_id": session_id, "answer": str(exc), "status": "failed"}
            start = len(messages) - 1
            result, decisions = self._run(messages, session_id)
            if not self._save_history(session_id, history, messages[start:]):
                return {
                    "session_id": session_id,
                    "answer": "本轮历史未能确认保存。此前已确认的工具写入仍保留。",
                    "status": "failed",
                }
            if result["status"] == "completed" and decisions <= _MAX_DECISIONS - 2:
                self._compact_history(session_id)
            return result

    def _compact_history(self, session_id: str) -> None:
        store = self._session_store()
        try:
            state = store.load(session_id)
            batch = split_for_compaction(
                state["messages"], summary=state["summary"], memory=state["memory"]
            )
            if batch is None:
                return
            if batch.oversized_groups:
                _logger.warning(
                    "context_compaction_retained_oversized_groups session_id=%s count=%d",
                    session_id, batch.oversized_groups,
                )
            older = batch.older
            candidates = memory_candidates(state["memory"], older)
            extract = memory_request(candidates)
            summarize = summary_request(state["summary"], older)
            check_request_size(extract)
            check_request_size(summarize)
            memory = parse_memory(self._maintenance_text(extract), candidates)
            summary = parse_summary(self._maintenance_text(summarize))
            store.save(
                session_id,
                {**state, "messages": batch.retained, "memory": memory, "summary": summary},
                expected=state,
            )
        except ContextError as exc:
            _logger.warning("context_compaction_deferred session_id=%s: %s", session_id, exc)
        except (ModelError, SessionError):
            # 追加历史已经独立提交；压缩失败不回滚，也不重试覆盖。
            return

    def _maintenance_text(self, messages: list[dict[str, Any]]) -> str:
        assert self._model is not None
        decision = self._model.decide(messages)
        if decision["kind"] != "final":
            raise ContextError("压缩请求未返回文本。")
        return decision["text"]

    def _save_history(
        self, session_id: str, history: list[dict[str, Any]], new: list[dict[str, Any]]
    ) -> bool:
        store = self._session_store()
        expected = [*history, *new]
        for _ in range(2):
            try:
                store.append_messages(session_id, new, expected_messages=history)
                return True
            except SessionError:
                try:
                    observed = store.load(session_id)["messages"]
                except SessionError:
                    return False
                if observed == expected:
                    return True
                if observed != history:
                    return False
        return False

    def _run(
        self, messages: list[dict[str, Any]], session_id: str
    ) -> tuple[RuntimeResult, int]:
        if self._model is None:
            raise SessionError("发送消息需要配置模型。")
        definitions = self._tools.public_definitions()
        started_count = 0

        for turn in range(1, _MAX_DECISIONS + 1):
            try:
                check_request_size(messages)
            except ContextError as exc:
                return {"session_id": session_id, "answer": str(exc), "status": "failed"}, turn - 1
            try:
                decision = self._model.decide(list(messages), definitions)
            except ModelError:
                return {
                    "session_id": session_id,
                    "answer": f"模型请求失败，未获得最终回答。此前已启动 {started_count} 次工具调用。",
                    "status": "failed",
                }, turn

            if decision["kind"] == "final":
                answer = bound_final_answer(decision["text"])
                messages.append({"role": "assistant", "content": answer})
                return {
                    "session_id": session_id,
                    "answer": answer,
                    "status": "completed",
                }, turn

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
                    "content": format_tool_result(result),
                })
                if not result["ok"] and result["error"]["code"] == TOOL_STATE_UNCERTAIN:
                    uncertain = True

            if uncertain:
                return {
                    "session_id": session_id,
                    "answer": "工具写入状态无法核实，已停止本次请求。此前的操作未回滚。",
                    "status": "failed",
                }, turn

        return {
            "session_id": session_id,
            "answer": (
                f"已达到 {_MAX_DECISIONS} 次模型决策上限，未获得最终回答。"
                f"此前已启动 {started_count} 次工具调用。"
            ),
            "status": "limit_reached",
        }, _MAX_DECISIONS
