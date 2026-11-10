"""独立 Session 文件的创建、读取与原子替换。"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import TYPE_CHECKING, Any, TypedDict, cast
from uuid import uuid4

if TYPE_CHECKING:
    from agent_harness.tools.todo import TodoItem

_SESSION_ID = re.compile(r"[0-9a-f]{32}")


class SessionError(Exception):
    """可安全报告给调用方的会话错误。"""


class SessionConflictError(SessionError):
    """全量保存的预期快照与权威文件不一致。"""


class SessionState(TypedDict):
    messages: list[dict[str, Any]]
    todos: list[TodoItem]
    memory: list[Any]
    summary: str


def _validate_state(value: object) -> SessionState:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("messages"), list)
        or not all(isinstance(message, dict) for message in value["messages"])
        or not isinstance(value.get("todos"), list)
        or not isinstance(value.get("memory"), list)
        or not isinstance(value.get("summary"), str)
    ):
        raise SessionError("会话 JSON 结构无效。")
    ids: set[str] = set()
    for todo in value["todos"]:
        if (
            not isinstance(todo, dict)
            or not isinstance(todo.get("id"), str)
            or not todo["id"]
            or not isinstance(todo.get("content"), str)
            or todo["id"] in ids
        ):
            raise SessionError("会话中的待办记录无效或 ID 重复。")
        ids.add(todo["id"])
    return cast(SessionState, value)


class SessionStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory.absolute()
        self._check_directory()
        # 只解析父目录，不能把配置目录本身的 junction 解析掉。
        try:
            self._directory = self._directory.parent.resolve() / self._directory.name
        except (OSError, RuntimeError):
            raise SessionError("无法验证会话目录路径。") from None
        self._lock = Lock()

    def create(self) -> str:
        """创建空会话，返回系统分配的 ID。"""
        with self._lock:
            self._check_directory()
            try:
                self._directory.mkdir(parents=True, exist_ok=True)
                while True:
                    session_id = uuid4().hex
                    path = self._path(session_id)
                    if not path.exists():
                        break
                self._replace(path, {"messages": [], "todos": [], "memory": [], "summary": ""})
            except (OSError, ValueError, TypeError, RecursionError):
                raise SessionError("无法创建会话文件。") from None
        return session_id

    def load(self, session_id: str) -> SessionState:
        """从权威 JSON 读取一个独立快照。"""
        path = self._existing_path(session_id)
        try:
            with path.open(encoding="utf-8") as stream:
                return _validate_state(json.load(stream))
        except (OSError, ValueError, RecursionError):
            raise SessionError("无法读取会话 JSON。") from None

    def save(
        self, session_id: str, state: SessionState, *, expected: SessionState
    ) -> None:
        """只有权威文件仍与原始快照一致时才全量保存。"""
        with self._lock:
            latest = self.load(session_id)
            try:
                current_json = json.dumps(latest, sort_keys=True, allow_nan=False)
                expected_json = json.dumps(expected, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError, RecursionError):
                raise SessionError("无法比较会话快照。") from None
            if current_json != expected_json:
                raise SessionConflictError("会话快照已过期，未保存。")
            self._save(session_id, state)

    def append_messages(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        expected_messages: list[dict[str, Any]] | None = None,
    ) -> None:
        """在最新状态上追加消息，可选校验追加前的历史。"""
        with self._lock:
            latest = self.load(session_id)
            if expected_messages is not None and latest["messages"] != expected_messages:
                raise SessionConflictError("会话消息已变更，未追加历史。")
            self._save(session_id, {**latest, "messages": [*latest["messages"], *messages]})

    def _save(self, session_id: str, state: SessionState) -> None:
        path = self._existing_path(session_id)
        _validate_state(state)
        try:
            self._replace(path, state)
        except (OSError, ValueError, TypeError, RecursionError):
            raise SessionError("无法保存会话 JSON。") from None

    def _check_directory(self) -> None:
        try:
            metadata = self._directory.lstat()
        except FileNotFoundError:
            return
        except OSError:
            raise SessionError("无法验证会话目录路径。") from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        if not stat.S_ISDIR(metadata.st_mode) or attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise SessionError("会话目录不安全，必须使用普通目录。")

    def _path(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise SessionError("会话 ID 无效。")
        self._check_directory()
        path = self._directory / f"{session_id}.json"
        try:
            if path.is_symlink() or path.resolve() != path:
                raise SessionError("会话文件路径不安全。")
        except (OSError, RuntimeError):
            raise SessionError("无法验证会话文件路径。") from None
        return path

    def _existing_path(self, session_id: str) -> Path:
        path = self._path(session_id)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise SessionError("会话不存在。") from None
        except OSError:
            raise SessionError("无法访问会话文件。") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1:
            raise SessionError("会话文件路径不安全。")
        return path

    @staticmethod
    def _replace(path: Path, state: SessionState) -> None:
        temp_path: Path | None = None
        try:
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(state, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
