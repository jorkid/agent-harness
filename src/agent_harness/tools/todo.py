"""当前进程内的会话隔离 Todo。"""

from __future__ import annotations

from collections.abc import Mapping
from threading import Lock
from typing import Any, TypedDict
from uuid import uuid4

from agent_harness.tools.registry import TOOL_STATE_UNCERTAIN, ToolDefinition, ToolError


class TodoItem(TypedDict):
    id: str
    content: str


class TodoStore:
    """保存待办，所有查询和写入都使用可信会话标识。"""

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, TodoItem]] = {}
        self._lock = Lock()

    def list(self, session_id: str) -> list[TodoItem]:
        with self._lock:
            return [item.copy() for owner, item in self._items.values() if owner == session_id]

    def get(self, session_id: str, todo_id: str) -> TodoItem:
        with self._lock:
            return self._owned(session_id, todo_id).copy()

    def create(self, session_id: str, content: str) -> dict[str, Any]:
        with self._lock:
            existing = self._find_content(session_id, content)
            if existing is not None:
                return {"todo": existing.copy(), "created": False, "recovered": False}
            todo_id = uuid4().hex
            if todo_id in self._items:
                raise ToolError("todo_id_conflict", "生成的待办 ID 已存在，创建已停止。")
            item: TodoItem = {"id": todo_id, "content": content}
            recovered = self._write(todo_id, (session_id, item))
            return {"todo": item.copy(), "created": True, "recovered": recovered}

    def update(self, session_id: str, todo_id: str, content: str) -> dict[str, Any]:
        with self._lock:
            self._owned(session_id, todo_id)
            existing = self._find_content(session_id, content)
            if existing is not None and existing["id"] != todo_id:
                raise ToolError("todo_content_exists", "当前会话已有另一条相同内容的待办。")
            item: TodoItem = {"id": todo_id, "content": content}
            recovered = self._write(todo_id, (session_id, item))
            return {"todo": item.copy(), "recovered": recovered}

    def delete(self, session_id: str, todo_id: str) -> dict[str, Any]:
        with self._lock:
            self._owned(session_id, todo_id)
            recovered = self._write(todo_id, None)
            return {"deleted_id": todo_id, "recovered": recovered}

    def _owned(self, session_id: str, todo_id: str) -> TodoItem:
        entry = self._items.get(todo_id)
        if entry is None or entry[0] != session_id:
            raise ToolError("todo_not_found", "当前会话中找不到该待办。")
        return entry[1]

    def _find_content(self, session_id: str, content: str) -> TodoItem | None:
        for owner, item in self._items.values():
            if owner == session_id and item["content"] == content:
                return item
        return None

    def _apply(self, todo_id: str, expected: tuple[str, TodoItem] | None) -> None:
        if expected is None:
            del self._items[todo_id]
        else:
            self._items[todo_id] = expected

    def _write(self, todo_id: str, expected: tuple[str, TodoItem] | None) -> bool:
        try:
            self._apply(todo_id, expected)
        except Exception:
            # 持有同一把锁核查目标记录，避免异常后重放写入。
            try:
                if self._items.get(todo_id) == expected:
                    return True
            except Exception:
                pass
            raise ToolError(TOOL_STATE_UNCERTAIN, "待办写入状态无法核实。") from None
        return False


_ACTION_FIELDS = {
    "create": {"action", "content"},
    "list": {"action"},
    "get": {"action", "todo_id"},
    "update": {"action", "todo_id", "content"},
    "delete": {"action", "todo_id"},
}


def create_todo_tool(store: TodoStore) -> ToolDefinition:
    def handle(arguments: Mapping[str, Any], *, session_id: str) -> dict[str, Any]:
        action = arguments["action"]
        if arguments.keys() != _ACTION_FIELDS[action]:
            raise ToolError("invalid_arguments", "参数与待办操作不匹配。")
        if action == "create":
            return store.create(session_id, arguments["content"].strip())
        if action == "list":
            return {"todos": store.list(session_id)}

        todo_id = arguments["todo_id"]
        if action == "get":
            return {"todo": store.get(session_id, todo_id)}
        if action == "update":
            return store.update(session_id, todo_id, arguments["content"].strip())
        return store.delete(session_id, todo_id)

    return ToolDefinition(
        name="todo",
        description=(
            "Manage todos in the current session. create needs content, list needs no "
            "additional fields, get and delete need todo_id, update needs todo_id and content. "
            "A repeated create for the same active content returns the existing todo."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTION_FIELDS)},
                "todo_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "content": {"type": "string", "minLength": 1, "maxLength": 500, "pattern": r"\S"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        handler=handle,
        requires_session=True,
    )
