"""最小命令行入口：读取配置并向 DeepSeek 发送一条文本请求。

用法：
    python -m agent_harness.cli "Hello"
    python -m agent_harness.cli --trace --session <ID> "<消息>"
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from agent_harness.config import ConfigError, load_settings
from agent_harness.model import Model, ModelError
from agent_harness.runtime import Runtime
from agent_harness.session import SessionError, SessionStore
from agent_harness.tools import create_default_tool_manager
from agent_harness.trace import TRACE_LOGGER_NAME


def _configure_encoding() -> None:
    """在 Windows 控制台统一使用 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _write_trace_error(code: str) -> None:
    print(json.dumps({"event": "cli_error", "status": "failed", "error_code": code}), file=sys.stderr)


def _session_main(args: list[str], *, trace: bool = False) -> int:
    if args == ["--new-session"]:
        session_id = None
        message = ""
    elif args[0] == "--session" and len(args) >= 2:
        session_id = args[1]
        message = " ".join(args[2:])
    else:
        print("用法：--new-session 或 --session <ID> [消息]", file=sys.stderr)
        return 2

    running_request = False
    try:
        sessions = SessionStore(Path.cwd() / "sessions")
        model = Model(load_settings()) if message else None
        runtime = Runtime(model, create_default_tool_manager(), sessions=sessions)
        if session_id is None:
            output = {"session_id": runtime.create_session()}
        elif not message:
            state = runtime.restore_session(session_id)
            output = {"session_id": session_id, "todos": state["todos"]}
        else:
            running_request = True
            output = runtime.run(message, session_id)
    except (SessionError, ConfigError, ModelError) as exc:
        if not trace:
            print(f"[会话错误] {exc}", file=sys.stderr)
        elif isinstance(exc, ConfigError):
            _write_trace_error("config_error")
        elif isinstance(exc, ModelError):
            _write_trace_error("model_error")
        elif not running_request:
            _write_trace_error("session_error")
        # Runtime.run 已为 SessionError 写出错误和最终状态。
        return 1

    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("status", "completed") == "completed" else 1


def _session_with_trace(args: list[str]) -> int:
    if len(args) < 3 or args[0] != "--session":
        _write_trace_error("invalid_trace_arguments")
        return 2

    logger = logging.getLogger(TRACE_LOGGER_NAME)
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(lambda record: hasattr(record, "trace_payload"))
    handler.setFormatter(logging.Formatter("%(trace_payload)s"))
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        return _session_main(args, trace=True)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        handler.close()


def main(argv: list[str] | None = None) -> int:
    _configure_encoding()
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：python -m agent_harness.cli "<消息>" | --new-session | --session <ID> [消息]', file=sys.stderr)
        return 2

    if args[0] == "--trace":
        return _session_with_trace(args[1:])
    if args[0] in ("--new-session", "--session"):
        return _session_main(args)

    text = " ".join(args)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[配置错误] {exc}", file=sys.stderr)
        return 1

    try:
        answer = Model(settings).chat(text)
    except ModelError as exc:
        print(f"[模型错误] {exc}", file=sys.stderr)
        return 1

    print(answer)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
