import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_harness.config import Settings
from agent_harness.model import Model, ModelError
from agent_harness.tools import create_default_tool_manager


def _response(
    content: object = None,
    tool_calls: object = None,
    finish_reason: str = "stop",
) -> SimpleNamespace:
    return SimpleNamespace(choices=[
        SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls),
        )
    ])


def _call(
    call_id: str = "call_1",
    name: str = "calculator",
    arguments: object = '{"expression":"1+1"}',
    call_type: str = "function",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type=call_type,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        client_factory = patch("agent_harness.model.OpenAI")
        factory = client_factory.start()
        self.addCleanup(client_factory.stop)
        self.create = factory.return_value.chat.completions.create
        self.model = Model(Settings("test-key", "https://example.invalid", "test-model"))
        self.messages = [{"role": "user", "content": "A test prompt"}]

    def test_final_text(self) -> None:
        self.create.return_value = _response("Visible answer.")
        result = self.model.decide(self.messages)
        self.assertEqual(result, {"kind": "final", "text": "Visible answer."})
        self.create.assert_called_once_with(
            model="test-model",
            messages=self.messages,
            extra_body={"thinking": {"type": "disabled"}},
        )

    def test_single_tool_call_preserves_visible_assistant_message(self) -> None:
        raw_arguments = '{ "expression": "1+1" }'
        response = _response("I will calculate.", [_call(arguments=raw_arguments)], "tool_calls")
        response.choices[0].message.reasoning_content = "ignored"
        self.create.return_value = response

        result = self.model.decide(self.messages)
        self.assertEqual(result, {
            "kind": "tool_calls",
            "tool_calls": [{
                "tool_call_id": "call_1",
                "name": "calculator",
                "arguments": {"expression": "1+1"},
            }],
            "assistant_message": {
                "role": "assistant",
                "content": "I will calculate.",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": raw_arguments},
                }],
            },
        })
        self.assertEqual(json.loads(json.dumps(result, allow_nan=False)), result)

    def test_multiple_calls_keep_order_and_ids(self) -> None:
        self.create.return_value = _response(
            tool_calls=[_call(), _call("call_2", "search", '{"query":"python"}')],
            finish_reason="tool_calls",
        )

        result = self.model.decide(self.messages)
        self.assertEqual(result["tool_calls"], [
            {"tool_call_id": "call_1", "name": "calculator", "arguments": {"expression": "1+1"}},
            {"tool_call_id": "call_2", "name": "search", "arguments": {"query": "python"}},
        ])
        self.assertEqual(
            [call["id"] for call in result["assistant_message"]["tool_calls"]],
            ["call_1", "call_2"],
        )
        self.assertIsNone(result["assistant_message"]["content"])

    def test_rejects_invalid_json_arguments(self) -> None:
        for raw in ("{", '{"n":NaN}', '{"n":Infinity}', '{"n":1e999}'):
            with self.subTest(raw=raw):
                self.create.return_value = _response(
                    tool_calls=[_call(arguments=raw)], finish_reason="tool_calls"
                )
                with self.assertRaisesRegex(ModelError, "有效的 JSON"):
                    self.model.decide(self.messages)

    def test_rejects_arguments_that_are_not_objects(self) -> None:
        for raw in ('["python"]', "null", "42"):
            with self.subTest(raw=raw):
                self.create.return_value = _response(
                    tool_calls=[_call(arguments=raw)], finish_reason="tool_calls"
                )
                with self.assertRaisesRegex(ModelError, "JSON 对象"):
                    self.model.decide(self.messages)

    def test_rejects_empty_responses(self) -> None:
        for response in (None, SimpleNamespace(choices=[]), _response(), _response("  ")):
            with self.subTest(response=response):
                self.create.return_value = response
                with self.assertRaises(ModelError):
                    self.model.decide(self.messages)

    def test_rejects_missing_call_fields(self) -> None:
        for field, message in (("id", "调用 ID"), ("name", "工具名称"), ("arguments", "参数字符串")):
            with self.subTest(field=field):
                call = _call()
                delattr(call if field == "id" else call.function, field)
                self.create.return_value = _response(
                    tool_calls=[call], finish_reason="tool_calls"
                )
                with self.assertRaisesRegex(ModelError, message):
                    self.model.decide(self.messages)

    def test_rejects_duplicate_call_ids(self) -> None:
        self.create.return_value = _response(
            tool_calls=[_call(), _call()], finish_reason="tool_calls"
        )
        with self.assertRaisesRegex(ModelError, "重复的工具调用 ID"):
            self.model.decide(self.messages)

    def test_rejects_unsupported_response_structures(self) -> None:
        cases = (
            SimpleNamespace(choices={}),
            SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop")]),
            _response(content=["text"]),
            _response(tool_calls={"type": "function"}, finish_reason="tool_calls"),
            _response(tool_calls=[_call(call_type="custom")], finish_reason="tool_calls"),
            _response(tool_calls=[_call(arguments={"expression": "1+1"})], finish_reason="tool_calls"),
            _response(tool_calls=[_call()], finish_reason="length"),
            _response(tool_calls=[_call()]),
            _response(finish_reason="tool_calls"),
            _response(finish_reason="function_call"),
        )
        for response in cases:
            with self.subTest(response=response):
                self.create.return_value = response
                with self.assertRaises(ModelError):
                    self.model.decide(self.messages)

    def test_maps_registry_definitions_to_serializable_api_tools(self) -> None:
        definitions = create_default_tool_manager().public_definitions()
        self.create.return_value = _response("No tool needed.")

        result = self.model.decide(self.messages, definitions)

        self.assertEqual(result["kind"], "final")
        self.create.assert_called_once()
        payload = self.create.call_args.kwargs
        expected = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters_schema"],
                },
            }
            for tool in definitions
        ]
        self.assertEqual(payload["tools"], expected)
        self.assertEqual(json.loads(json.dumps(payload["tools"], allow_nan=False)), expected)
        self.assertEqual(payload["extra_body"], {"thinking": {"type": "disabled"}})

    def test_does_not_include_transport_error_details(self) -> None:
        self.create.side_effect = RuntimeError("private request data")
        with self.assertRaises(ModelError) as error:
            self.model.decide(self.messages)
        self.assertNotIn("private request data", str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_t01_chat_request_is_unchanged(self) -> None:
        self.create.return_value = _response("Plain reply.")
        self.assertEqual(self.model.chat("Hello"), "Plain reply.")
        self.create.assert_called_once_with(
            model="test-model", messages=[{"role": "user", "content": "Hello"}]
        )
