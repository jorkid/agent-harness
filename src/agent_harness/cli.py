"""最小命令行入口：读取配置并向 DeepSeek 发送一条文本请求。

用法：
    python -m agent_harness.cli "Hello"
"""

from __future__ import annotations

import sys

from agent_harness.config import ConfigError, load_settings
from agent_harness.model import Model, ModelError


def _configure_encoding() -> None:
    """在 Windows 控制台统一使用 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _configure_encoding()
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('用法：python -m agent_harness.cli "<消息>"', file=sys.stderr)
        return 2

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
