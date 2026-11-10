"""查询固定的 Mock 搜索数据。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_harness.tools.registry import ToolDefinition

_MOCK_RESULTS = {
    "python": ("Python 文档", "Python 语言参考和标准库文档。"),
    "agent harness": ("Agent Harness", "协调模型、工具、会话和上下文的运行框架。"),
}


def _search(arguments: Mapping[str, Any]) -> dict[str, Any]:
    query = arguments["query"].strip().casefold()
    match = _MOCK_RESULTS.get(query)
    results = [] if match is None else [{"title": match[0], "snippet": match[1]}]
    return {"mock": True, "results": results}


SEARCH = ToolDefinition(
    name="search",
    description="按关键词查询固定 Mock 数据。示例关键词为 python 和 agent harness。",
    parameters_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
                "pattern": r"\S",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    handler=_search,
)
