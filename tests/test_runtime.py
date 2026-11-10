import json
import unittest
from unittest.mock import Mock, patch

from openai import APITimeoutError

from agent_harness.config import Settings
from agent_harness.model import Model, ModelDecision, ToolCall, ToolCallsDecision
from agent_harness.runtime import Runtime
from agent_harness.tools import create_default_tool_manager


def _call(call_id: str, expression: str) -> ToolCall:
    return {
        "tool_call_id": call_id,
        "name": "calculator",
        "arguments": {"expression": expression},
    }


def _batch(*calls: ToolCall) -> ToolCallsDecision:
    return {
        "kind": "tool_calls",
        "tool_calls": list(calls),
        "assistant_message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call["tool_call_id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": json.dumps(call["arguments"]),
                    },
                }
                for call in calls
            ],
        },
    }


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = Mock(spec=Model)
        self.tools = create_default_tool_manager()
        self.runtime = Runtime(self.model, self.tools)
        self.requests: list[list[dict]] = []

    def _script(self, *decisions: ModelDecision) -> None:
        pending = iter(decisions)

        def decide(messages: list[dict], definitions: list[dict]) -> ModelDecision:
            self.requests.append(list(messages))
            self.assertEqual(definitions, self.tools.public_definitions())
            return next(pending)

        self.model.decide.side_effect = decide

    def test_direct_reply_and_request_isolation(self) -> None:
        self._script(
            {"kind": "final", "text": "answer A"},
            {"kind": "final", "text": "answer B"},
        )
        with self.assertNoLogs("agent_harness.runtime", level="INFO"):
            first = self.runtime.run("message A", "session-A")
            second = self.runtime.run("message B", "session-B")

        self.assertEqual(first, {"session_id": "session-A", "answer": "answer A", "status": "completed"})
        self.assertEqual(second, {"session_id": "session-B", "answer": "answer B", "status": "completed"})
        self.assertEqual(self.requests, [
            [{"role": "user", "content": "message A"}],
            [{"role": "user", "content": "message B"}],
        ])

    def test_executes_tool_and_feeds_result_before_final_reply(self) -> None:
        batch = _batch(_call("call_1", "17*(23+5)"))
        self._script(batch, {"kind": "final", "text": "476"})

        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            result = self.runtime.run("calculate", "trusted")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["answer"], "476")
        self.assertEqual(len(self.requests), 2)
        self.assertIs(self.requests[1][1], batch["assistant_message"])
        tool_message = self.requests[1][2]
        self.assertEqual(tool_message["role"], "tool")
        self.assertEqual(tool_message["tool_call_id"], "call_1")
        self.assertEqual(json.loads(tool_message["content"]), {"ok": True, "result": {"value": 476}})
        self.assertEqual(json.loads(trace.records[0].getMessage().removeprefix("tool_trace ")), {
            "session_id": "trusted", "tool_call_id": "call_1", "tool_name": "calculator",
            "started": True, "ok": True, "result_summary": {"field_count": 1, "value": 476},
        })

    def test_feedback_distinguishes_handler_error_and_validation_rejection(self) -> None:
        rejected = _call("invalid", "1+1")
        rejected["arguments"]["session_id"] = "spoofed"
        self._script(
            _batch(_call("divide", "1/0"), rejected),
            {"kind": "final", "text": "The calls failed."},
        )
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            result = self.runtime.run("calculate", "trusted")

        self.assertEqual(result["session_id"], "trusted")
        self.assertEqual(result["status"], "completed")
        messages = self.requests[1][2:]
        self.assertEqual([item["tool_call_id"] for item in messages], ["divide", "invalid"])
        errors = [json.loads(item["content"]) for item in messages]
        self.assertEqual([item["error"]["code"] for item in errors], ["division_by_zero", "invalid_arguments"])
        events = [json.loads(record.getMessage().removeprefix("tool_trace ")) for record in trace.records]
        self.assertEqual([event["started"] for event in events], [True, False])
        self.assertEqual([event["ok"] for event in events], [False, False])
        self.assertNotIn("spoofed", " ".join(trace.output))

    def test_trace_redacts_api_key_shaped_identifiers(self) -> None:
        secret = "sk-" + "x" * 24
        call = {"tool_call_id": "call_" + secret, "name": secret, "arguments": {}}
        self._script(_batch(call), {"kind": "final", "text": "Unknown tool."})
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            self.runtime.run("input", "trusted")

        self.assertNotIn(secret, trace.output[0])
        self.assertIn("[redacted]", trace.output[0])
        self.assertIn("unknown_tool", trace.output[0])

    def test_multiple_tools_keep_order_and_ids(self) -> None:
        batch = _batch(
            {"tool_call_id": "search-first", "name": "search", "arguments": {"query": "python"}},
            _call("calc-second", "4+7"),
        )
        self._script(batch, {"kind": "final", "text": "done"})
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            result = self.runtime.run("search and calculate", "trusted")

        self.assertEqual(result["status"], "completed")
        self.assertIs(self.requests[1][1], batch["assistant_message"])
        messages = self.requests[1][2:]
        self.assertEqual([item["tool_call_id"] for item in messages], ["search-first", "calc-second"])
        self.assertTrue(json.loads(messages[0]["content"])["result"]["mock"])
        self.assertEqual(json.loads(messages[1]["content"])["result"]["value"], 11)
        self.assertEqual(
            [json.loads(record.getMessage().removeprefix("tool_trace "))["tool_call_id"]
             for record in trace.records],
            ["search-first", "calc-second"],
        )

    def test_eighth_tool_decision_stops_model_and_skips_its_batch(self) -> None:
        batches = [_batch(_call(f"call_{i}", "1+1")) for i in range(1, 8)]
        batches.append(_batch(_call("last-a", "2+2"), _call("last-b", "3+3")))
        self._script(*batches)
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            result = self.runtime.run("keep calling", "trusted")

        self.assertEqual(result["status"], "limit_reached")
        self.assertIn("未获得最终回答", result["answer"])
        self.assertEqual(self.model.decide.call_count, 8)
        events = [json.loads(record.getMessage().removeprefix("tool_trace ")) for record in trace.records]
        self.assertEqual([event["started"] for event in events], [True] * 7 + [False, False])
        self.assertEqual([event["tool_call_id"] for event in events[-2:]], ["last-a", "last-b"])
        self.assertEqual([event["error_code"] for event in events[-2:]], ["limit_reached"] * 2)

    def test_eighth_final_decision_still_completes(self) -> None:
        batches = [_batch(_call(f"call_{i}", "1+1")) for i in range(1, 8)]
        self._script(*batches, {"kind": "final", "text": "final on eighth"})
        with self.assertLogs("agent_harness.runtime", level="INFO"):
            result = self.runtime.run("calculate", "trusted")
        self.assertEqual(result, {"session_id": "trusted", "answer": "final on eighth", "status": "completed"})
        self.assertEqual(self.model.decide.call_count, 8)

    def test_sdk_timeout_returns_failed(self) -> None:
        settings = Settings("test-key", "https://example.invalid", "test-model")
        with patch("agent_harness.model.OpenAI") as factory:
            create = factory.return_value.chat.completions.create
            create.side_effect = APITimeoutError(request=Mock())
            result = Runtime(Model(settings), self.tools).run("hello", "trusted")
        self.assertEqual(result["session_id"], "trusted")
        self.assertEqual(result["status"], "failed")
        self.assertIn("未获得最终回答", result["answer"])
        create.assert_called_once()

    def test_sdk_client_uses_approved_retry_and_timeout(self) -> None:
        model = Model(Settings("test-key", "https://example.invalid", "test-model"))
        self.addCleanup(model._client.close)
        self.assertEqual(model._client.max_retries, 0)
        self.assertEqual(model._client.timeout, 30.0)
