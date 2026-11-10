"""最小命令行入口：读取配置并向 DeepSeek 发送一条文本请求。

用法：
    python -m agent_harness.cli "Hello"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_harness.config import ConfigError, load_settings
from agent_harness.model import Model, ModelError
from agent_harness.runtime import Runtime
from agent_harness.session import SessionError, SessionStore
from agent_harness.tools import create_default_tool_manager


def _configure_encoding() -> None:
    """在 Windows 控制台统一使用 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def _session_main(args: list[str]) -> int:
    if args == ["--new-session"]:
        session_id = None
        message = ""
    elif args[0] == "--session" and len(args) >= 2:
        session_id = args[1]
        message = " ".join(args[2:])
    else:
        print("用法：--new-session 或 --session <ID> [消息]", file=sys.stderr)
        return 2

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
            output = runtime.run(message, session_id)
    except (SessionError, ConfigError, ModelError) as exc:
        print(f"[会话错误] {exc}", file=sys.stderr)
        return 1

    print(json.dumps(output, ensure_ascii=False))
    return 0 if output.get("status", "completed") == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    _configure_encoding()
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：python -m agent_harness.cli "<消息>" | --new-session | --session <ID> [消息]', file=sys.stderr)
        return 2

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
