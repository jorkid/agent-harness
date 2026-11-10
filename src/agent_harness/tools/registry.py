"""工具注册、参数校验和分发。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal, TypedDict

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from referencing import Registry


class PublicToolDefinition(TypedDict):
    name: str
    description: str
    parameters_schema: dict[str, Any]


class ToolSuccess(TypedDict):
    ok: Literal[True]
    result: dict[str, Any]


class ToolErrorDetail(TypedDict):
    code: str
    message: str


class ToolFailure(TypedDict):
    ok: Literal[False]
    error: ToolErrorDetail


ToolResult = ToolSuccess | ToolFailure


class ToolError(Exception):
    """工具可预期的业务失败，消息可安全反馈给模型。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: Callable[[Mapping[str, Any]], dict[str, Any]]


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        return isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item)
            for key, item in value.items()
        )
    return False


def _check_schema_policy(value: object) -> None:
    if isinstance(value, dict):
        # 本地 JSON Pointer 可以指向文档内任意位置，遍历嵌套对象和数组。
        divisor = value.get("multipleOf")
        if isinstance(divisor, (int, float)) and not isinstance(divisor, bool):
            raise SchemaError("工具参数 Schema 不支持 multipleOf 约束。")
        for key, child in value.items():
            if (
                key in ("$ref", "$dynamicRef", "$recursiveRef")
                and isinstance(child, str)
                and child
                and not child.startswith("#")
            ):
                raise SchemaError(f"工具参数 Schema 的 {key} 仅支持同文档引用。")
            _check_schema_policy(child)
    elif isinstance(value, list):
        for child in value:
            _check_schema_policy(child)


def _public_definition(
    tool: ToolDefinition, validator: Draft202012Validator
) -> PublicToolDefinition:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters_schema": deepcopy(validator.schema),
    }


def _failure(code: str, message: str) -> ToolFailure:
    return {"ok": False, "error": {"code": code, "message": message}}


class ToolManager:
    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolDefinition, Draft202012Validator]] = {}

    def register(self, tool: ToolDefinition) -> None:
        """注册工具并验证 Schema。重复名称视为配置错误。"""
        if tool.name in self._tools:
            raise ValueError(f"工具 {tool.name} 已注册。")
        schema = tool.parameters_schema
        if (
            not isinstance(schema, dict)
            or schema.get("type") != "object"
            or not _is_json_value(schema)
        ):
            raise ValueError("工具参数 Schema 必须是根类型为 object 的 JSON 对象。")

        # 保存快照，让实际校验与对外发布的 Schema 始终一致。
        schema = deepcopy(schema)
        Draft202012Validator.check_schema(schema)
        _check_schema_policy(schema)
        # 显式使用不自动获取外部资源的引用注册表。
        self._tools[tool.name] = (tool, Draft202012Validator(schema, registry=Registry()))

    def get_definition(self, name: str) -> PublicToolDefinition | None:
        """按名称查询公开定义。"""
        entry = self._tools.get(name)
        return _public_definition(*entry) if entry is not None else None

    def public_definitions(self) -> list[PublicToolDefinition]:
        """按注册顺序返回可直接序列化的公开定义。"""
        return [_public_definition(*entry) for entry in self._tools.values()]

    def execute(self, name: str, arguments: object) -> ToolResult:
        """校验已解析的 JSON 参数，通过后调用注册的 handler。"""
        entry = self._tools.get(name) if isinstance(name, str) else None
        if entry is None:
            return _failure("unknown_tool", "工具未注册。")
        tool, validator = entry

        try:
            if not isinstance(arguments, dict) or not _is_json_value(arguments):
                return _failure("invalid_arguments", "工具参数必须是合法的 JSON 对象。")
        except RecursionError:
            return _failure("invalid_arguments", "工具参数嵌套过深或含循环引用。")

        try:
            validator.validate(arguments)
        except ValidationError as exc:
            return _failure(
                "invalid_arguments",
                f"{exc.json_path} 未通过 Schema 的 {exc.validator} 约束。",
            )
        except OverflowError:
            return _failure(
                "invalid_arguments", "当前参数与数值约束的组合超出 Schema 校验器处理范围。"
            )
        except Exception:
            # 例如元 Schema 检查通过，但实际校验时无法解析 $ref。
            return _failure("invalid_tool_schema", "工具 Schema 配置无法执行校验。")

        try:
            result = tool.handler(arguments)
        except ToolError as exc:
            return _failure(exc.code, str(exc))
        except Exception:
            return _failure("tool_execution_failed", "工具执行失败。")

        try:
            if isinstance(result, dict) and _is_json_value(result):
                json.dumps(result, allow_nan=False)
                return {"ok": True, "result": result}
        except Exception:
            pass
        return _failure("invalid_tool_result", "工具返回值必须是可序列化的 JSON 对象。")
