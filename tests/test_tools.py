import json
import unittest
from unittest.mock import Mock

from jsonschema import SchemaError

from agent_harness.tools import (
    ToolDefinition,
    ToolManager,
    create_default_tool_manager,
)


class ToolManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = create_default_tool_manager()

    def test_registers_built_in_tools(self) -> None:
        self.assertEqual(
            [tool["name"] for tool in self.manager.public_definitions()],
            ["calculator", "search"],
        )
        self.assertIsNotNone(self.manager.get_definition("calculator"))
        self.assertIsNotNone(self.manager.get_definition("search"))
        self.assertIsNone(self.manager.get_definition("unknown"))

    def test_exports_only_json_metadata(self) -> None:
        definitions = self.manager.public_definitions()
        self.assertEqual(json.loads(json.dumps(definitions)), definitions)
        for definition, parameter in zip(definitions, ("expression", "query"), strict=True):
            with self.subTest(tool=definition["name"]):
                self.assertEqual(
                    set(definition), {"name", "description", "parameters_schema"}
                )
                self.assertTrue(definition["description"])
                schema = definition["parameters_schema"]
                self.assertEqual(schema["type"], "object")
                self.assertEqual(schema["required"], [parameter])
                self.assertEqual(schema["properties"][parameter]["type"], "string")
                self.assertFalse(schema["additionalProperties"])

    def test_schema_snapshot_and_validation_before_dispatch(self) -> None:
        manager = ToolManager()
        handler = Mock(return_value={"value": "ok"})
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        manager.register(ToolDefinition("probe", "Test handler.", schema, handler))

        schema["properties"]["n"]["type"] = "string"
        manager.public_definitions()[0]["parameters_schema"]["properties"].clear()
        rejected = manager.execute("probe", {"n": "bad"})
        self.assertEqual(rejected["error"]["code"], "invalid_arguments")
        handler.assert_not_called()

        self.assertEqual(
            manager.execute("probe", {"n": 1}),
            {"ok": True, "result": {"value": "ok"}},
        )
        handler.assert_called_once_with({"n": 1})

    def test_rejects_duplicate_registration_and_invalid_schema(self) -> None:
        handler = Mock()
        with self.assertRaises(ValueError):
            self.manager.register(
                ToolDefinition("search", "Duplicate tool.", {"type": "object"}, handler)
            )
        with self.assertRaises(SchemaError):
            self.manager.register(
                ToolDefinition(
                    "bad", "Invalid schema.", {"type": "object", "required": "n"}, handler
                )
            )
        self.assertIsNone(self.manager.get_definition("bad"))
        handler.assert_not_called()

    def test_rejects_unknown_tool(self) -> None:
        result = self.manager.execute("missing", {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown_tool")

    def test_rejects_non_json_objects(self) -> None:
        manager = ToolManager()
        handler = Mock(return_value={})
        manager.register(ToolDefinition("probe", "Test handler.", {"type": "object"}, handler))
        cyclic = {}
        cyclic["self"] = cyclic
        cases = (None, [], '{"n": 1}', {1: "value"}, {"n": (1,)}, {"n": float("nan")}, cyclic)
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = manager.execute("probe", arguments)
                self.assertEqual(result["error"]["code"], "invalid_arguments")
        handler.assert_not_called()

    def test_rejects_missing_required_parameter(self) -> None:
        result = self.manager.execute("calculator", {})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("required", result["error"]["message"])

    def test_rejects_wrong_parameter_type(self) -> None:
        result = self.manager.execute("calculator", {"expression": 123})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("$.expression", result["error"]["message"])

    def test_rejects_session_id_in_model_arguments(self) -> None:
        result = self.manager.execute("search", {"query": "python", "session_id": "other"})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("additionalProperties", result["error"]["message"])

    def test_returns_generic_error_for_unexpected_handler_exception(self) -> None:
        handler = Mock(side_effect=RuntimeError("private exception details"))
        self.manager.register(
            ToolDefinition("broken", "Test handler failure.", {"type": "object"}, handler)
        )
        result = self.manager.execute("broken", {})
        self.assertEqual(result["error"]["code"], "tool_execution_failed")
        self.assertNotIn("private exception details", result["error"]["message"])

    def test_rejects_multiple_of_at_registration(self) -> None:
        handler = Mock()
        schemas = (
            {"type": "object", "multipleOf": 0.1},
            {
                "type": "object",
                "$defs": {"n": {"type": "number", "multipleOf": 1e308}},
                "properties": {"n": {"$ref": "#/$defs/n"}},
            },
        )
        for index, schema in enumerate(schemas):
            with self.subTest(schema=schema):
                name = f"multiple_{index}"
                with self.assertRaisesRegex(SchemaError, "multipleOf"):
                    self.manager.register(ToolDefinition(name, "Test numeric rule.", schema, handler))
                self.assertIsNone(self.manager.get_definition(name))
        handler.assert_not_called()

    def test_rejects_external_refs_at_registration(self) -> None:
        handler = Mock()
        cases = (
            ("$ref", "https://schema.invalid/number.json"),
            ("$ref", "./number.json"),
            ("$dynamicRef", "https://schema.invalid/number.json#n"),
            ("$recursiveRef", "https://schema.invalid/number.json"),
        )
        for index, (keyword, reference) in enumerate(cases):
            with self.subTest(keyword=keyword, reference=reference):
                name = f"external_{index}"
                schema = {"type": "object", "properties": {"n": {keyword: reference}}}
                with self.assertRaisesRegex(SchemaError, rf"\{keyword}"):
                    self.manager.register(ToolDefinition(name, "Test external ref.", schema, handler))
                self.assertIsNone(self.manager.get_definition(name))
        handler.assert_not_called()

    def test_missing_local_reference_is_a_schema_error(self) -> None:
        handler = Mock(return_value={"executed": True})
        schema = {
            "type": "object",
            "properties": {"n": {"$ref": "#/$defs/missing"}},
            "required": ["n"],
        }
        self.manager.register(ToolDefinition("bad_ref", "Test missing ref.", schema, handler))

        result = self.manager.execute("bad_ref", {"n": 1})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_tool_schema")
        self.assertNotIn("#/$defs/missing", result["error"]["message"])
        handler.assert_not_called()

    def test_valid_local_reference_still_validates_and_dispatches(self) -> None:
        handler = Mock(return_value={"executed": True})
        schema = {
            "type": "object",
            "$defs": {"n": {"type": "integer"}},
            "properties": {"n": {"$ref": "#/$defs/n"}},
            "required": ["n"],
        }
        self.manager.register(ToolDefinition("valid_ref", "Test local ref.", schema, handler))

        failure = self.manager.execute("valid_ref", {"n": "1"})
        self.assertEqual(failure["error"]["code"], "invalid_arguments")
        handler.assert_not_called()

        self.assertEqual(
            self.manager.execute("valid_ref", {"n": 1}),
            {"ok": True, "result": {"executed": True}},
        )
        handler.assert_called_once_with({"n": 1})

    def test_rejects_handler_results_that_are_not_json_objects(self) -> None:
        handler = Mock()
        self.manager.register(
            ToolDefinition("bad_result", "Test output validation.", {"type": "object"}, handler)
        )
        cyclic = {}
        cyclic["self"] = cyclic
        cases = (
            {"n": {1}},
            {"n": [float("nan")]},
            {"n": float("inf")},
            {"n": float("-inf")},
            {1: "not a string key"},
            cyclic,
            [1],
        )
        for value in cases:
            with self.subTest(value=value):
                handler.return_value = value
                result = self.manager.execute("bad_result", {})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "invalid_tool_result")
                json.dumps(result, allow_nan=False)


class CalculatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = create_default_tool_manager()

    def test_addition(self) -> None:
        self.assertEqual(
            self.manager.execute("calculator", {"expression": "1+1"}),
            {"ok": True, "result": {"value": 2}},
        )

    def test_precedence_parentheses_and_signed_numbers(self) -> None:
        cases = {
            "2 + 3 * 4": 14,
            " (2 + 3) * 4 - 8 / 2 ": 16,
            "-(3.5 + +1.5) / 2": -2.5,
            "1e15": 10**15,
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertEqual(result, {"ok": True, "result": {"value": expected}})

    def test_division_by_zero(self) -> None:
        result = self.manager.execute("calculator", {"expression": "2 / (1 - 1)"})
        self.assertEqual(result["error"]["code"], "division_by_zero")

    def test_rejects_arbitrary_python(self) -> None:
        result = self.manager.execute(
            "calculator", {"expression": '__import__("os").system("echo hacked")'}
        )
        self.assertEqual(result["error"]["code"], "invalid_expression")

    def test_rejects_invalid_syntax_and_unsupported_operators(self) -> None:
        for expression in ("2 +", "2 ** 4", "4 // 2", "4 % 3", "True + 1", "1 + 1 # text"):
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertEqual(result["error"]["code"], "invalid_expression")

    def test_rejects_out_of_range_literals_and_intermediate_results(self) -> None:
        for expression in ("1e16", "1e309", "(1e15 * 2) / 2"):
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertEqual(result["error"]["code"], "out_of_range")

    def test_rejects_exact_values_above_magnitude_limit(self) -> None:
        for expression in (
            "1000000000000000.01",
            "-1000000000000000.01",
            "1000000000000000 + 0.01",
            "1e15 + 1e-300",
        ):
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "out_of_range")

    def test_accepts_magnitude_limit_and_representable_small_number(self) -> None:
        for expression, expected in (
            ("1000000000000000.0", 1e15),
            ("-999999999999999.99 - 0.01", -1e15),
            ("5e-324", 5e-324),
        ):
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertEqual(result, {"ok": True, "result": {"value": expected}})
                self.assertIsInstance(result["result"]["value"], float)

    def test_rejects_nonzero_underflow_as_out_of_range(self) -> None:
        for expression in ("1e-400", "1 / 1e-400", "1 / (1e-200 * 1e-200)", "1e-999999999"):
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "out_of_range")

    def test_exact_zero_divisor_is_still_reported(self) -> None:
        for expression in ("1 / 0e-400", "1 / (0.3 - 0.2 - 0.1)"):
            with self.subTest(expression=expression):
                result = self.manager.execute("calculator", {"expression": expression})
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "division_by_zero")

    def test_rejects_overlong_expression_before_parsing(self) -> None:
        result = self.manager.execute("calculator", {"expression": "1" * 257})
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertIn("maxLength", result["error"]["message"])


class SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = create_default_tool_manager()

    def test_returns_fixed_mock_data(self) -> None:
        self.assertEqual(
            self.manager.execute("search", {"query": "  PYTHON "}),
            {
                "ok": True,
                "result": {
                    "mock": True,
                    "results": [{"title": "Python 文档", "snippet": "Python 语言参考和标准库文档。"}],
                },
            },
        )

    def test_no_match_returns_empty_mock_results(self) -> None:
        self.assertEqual(
            self.manager.execute("search", {"query": "no matching fixture"}),
            {"ok": True, "result": {"mock": True, "results": []}},
        )

    def test_rejects_blank_overlong_and_wrong_type_query(self) -> None:
        for query in ("", " \t\n ", "a" * 129, 42):
            with self.subTest(query=query):
                result = self.manager.execute("search", {"query": query})
                self.assertEqual(result["error"]["code"], "invalid_arguments")
