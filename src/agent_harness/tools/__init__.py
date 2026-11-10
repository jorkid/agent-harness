"""T02 工具及注册表的公开入口。"""

from agent_harness.tools.calculator import CALCULATOR
from agent_harness.tools.registry import (
    PublicToolDefinition,
    ToolDefinition,
    ToolError,
    ToolManager,
    ToolResult,
)
from agent_harness.tools.search import SEARCH

__all__ = [
    "PublicToolDefinition",
    "ToolDefinition",
    "ToolError",
    "ToolManager",
    "ToolResult",
    "create_default_tool_manager",
]


def create_default_tool_manager() -> ToolManager:
    manager = ToolManager()
    manager.register(CALCULATOR)
    manager.register(SEARCH)
    return manager
