import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_harness.model import Model, ModelError
from agent_harness.runtime import Runtime
from agent_harness.tools import create_default_tool_manager
from agent_harness.tools.todo import TodoStore, create_todo_tool


def _decision(*calls: tuple[str, dict]) -> dict:
    return {
        "kind": "tool_calls",
        "tool_calls": [
            {"tool_call_id": call_id, "name": "todo", "arguments": arguments}
            for call_id, arguments in calls
        ],
        "assistant_message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": "todo", "arguments": json.dumps(arguments)}}
                for call_id, arguments in calls
            ],
        },
    }


def _events(trace) -> list[dict]:
    events = [
        json.loads(record.getMessage().removeprefix("tool_trace "))
        for record in trace.records if record.getMessage().startswith("tool_trace ")
    ]
    return [event for event in events if event["event"] == "tool_result"]


class TodoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = TodoStore()
        self.tools = create_default_tool_manager()
        self.tools.register(create_todo_tool(self.store))
        self.model = Mock(spec=Model)
        self.runtime = Runtime(self.model, self.tools)

    def _todo(self, action: str, session_id: str = "A", **arguments) -> dict:
        result = self.tools.execute(
            "todo", {"action": action, **arguments}, session_id=session_id
        )
        json.dumps(result, allow_nan=False)
        return result

    def test_crud_keeps_id_stable_and_returns_snapshots(self) -> None:
        created = self._todo("create", content="  write tests  ")["result"]
        todo_id = created["todo"]["id"]
        self.assertTrue(created["created"])
        self.assertEqual(self._todo("get", todo_id=todo_id)["result"]["todo"], {
            "id": todo_id, "content": "write tests"
        })
        created["todo"]["content"] = "mutated outside store"
        self.assertEqual(self._todo("list")["result"]["todos"], [
            {"id": todo_id, "content": "write tests"}
        ])
        updated = self._todo("update", todo_id=todo_id, content="run tests")
        self.assertEqual(updated["result"]["todo"], {"id": todo_id, "content": "run tests"})
        self.assertEqual(self._todo("delete", todo_id=todo_id)["result"]["deleted_id"], todo_id)
        self.assertEqual(self._todo("list")["result"]["todos"], [])
        self.assertEqual(self._todo("get", todo_id=todo_id)["error"]["code"], "todo_not_found")

    def test_missing_and_repeated_ids_do_not_change_other_items(self) -> None:
        first = self._todo("create", content="first")["result"]["todo"]
        second = self._todo("create", content="second")["result"]["todo"]
        for action, fields in (
            ("get", {}), ("update", {"content": "changed"}), ("delete", {}),
        ):
            self.assertEqual(
                self._todo(action, todo_id="does-not-exist", **fields)["error"]["code"],
                "todo_not_found",
            )
        self._todo("delete", todo_id=first["id"])
        self.assertEqual(self._todo("delete", todo_id=first["id"])["error"]["code"], "todo_not_found")
        self.assertEqual(self.store.list("A"), [second])

    def test_cross_session_reads_and_writes_are_rejected(self) -> None:
        a = self._todo("create", "A", content="same")["result"]["todo"]
        b = self._todo("create", "B", content="same")["result"]["todo"]
        self.assertNotEqual(a["id"], b["id"])
        for action, fields in (
            ("get", {}), ("update", {"content": "changed"}), ("delete", {}),
        ):
            self.assertEqual(
                self._todo(action, "A", todo_id=b["id"], **fields)["error"]["code"],
                "todo_not_found",
            )
        self.assertEqual(self._todo("list", "A")["result"]["todos"], [a])
        self.assertEqual(self._todo("list", "B")["result"]["todos"], [b])

    def test_missing_trusted_session_and_invalid_action_fields_are_rejected(self) -> None:
        started, result = self.tools.execute_with_start_status("todo", {"action": "list"})
        self.assertFalse(started)
        self.assertEqual(result["error"]["code"], "missing_session")
        for action, arguments in (("create", {}), ("list", {"content": "x"}), ("delete", {})):
            result = self._todo(action, **arguments)
            self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(self.store.list("A"), [])

    def test_model_cannot_inject_session_id(self) -> None:
        schema = self.tools.get_definition("todo")["parameters_schema"]
        self.assertNotIn("session_id", schema["properties"])
        self.assertFalse(schema["additionalProperties"])
        self.model.decide.side_effect = [
            _decision(("spoof", {"action": "create", "content": "injected", "session_id": "B"})),
            {"kind": "final", "text": "Rejected."},
        ]
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            self.runtime.run("try to switch session", "A")
        event = _events(trace)[0]
        self.assertEqual(event["session_id"], "A")
        self.assertFalse(event["started"])
        self.assertEqual(event["error_code"], "invalid_arguments")
        self.assertEqual(self.store.list("A"), [])
        self.assertEqual(self.store.list("B"), [])

    def test_repeated_create_reuses_id_and_id_collision_cannot_overwrite(self) -> None:
        first = self._todo("create", content="2")["result"]["todo"]
        repeat = self._todo("create", content=" 2 ")["result"]
        self.assertFalse(repeat["created"])
        self.assertEqual(repeat["todo"], first)
        with patch("agent_harness.tools.todo.uuid4", return_value=SimpleNamespace(hex=first["id"])):
            result = self._todo("create", "B", content="different")
        self.assertEqual(result["error"]["code"], "todo_id_conflict")
        self.assertEqual(self.store.list("A"), [first])
        self.assertEqual(self.store.list("B"), [])

    def test_model_failure_preserves_write_and_success_trace(self) -> None:
        self.model.decide.side_effect = [
            _decision(("create", {"action": "create", "content": "2"})),
            ModelError("request failed"),
        ]
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            result = self.runtime.run("create a todo", "A")
        self.assertEqual(result["status"], "failed")
        self.assertEqual([item["content"] for item in self.store.list("A")], ["2"])
        self.assertTrue(_events(trace)[0]["ok"])

    def test_budget_limit_preserves_write(self) -> None:
        create = _decision(("create", {"action": "create", "content": "2"}))
        lists = [_decision((f"list-{i}", {"action": "list"})) for i in range(2, 8)]
        last = _decision(("too-late", {"action": "create", "content": "extra"}))
        self.model.decide.side_effect = [create, *lists, last]
        with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
            result = self.runtime.run("create a todo", "A")
        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual([item["content"] for item in self.store.list("A")], ["2"])
        self.assertTrue(_events(trace)[0]["ok"])
        self.assertFalse(_events(trace)[-1]["started"])

    def test_after_write_error_is_verified_and_duplicate_is_not_reapplied(self) -> None:
        self.model.decide.side_effect = [
            _decision(("create", {"action": "create", "content": "2"})),
            _decision(("repeat", {"action": "create", "content": "2"})),
            {"kind": "final", "text": "One todo exists."},
        ]
        apply = self.store._apply

        def write_then_raise(todo_id, expected):
            apply(todo_id, expected)
            raise RuntimeError("simulated error after applying write")

        with patch.object(self.store, "_apply", side_effect=write_then_raise) as writes:
            with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
                result = self.runtime.run("create a todo", "A")
        self.assertEqual(result["status"], "completed")
        writes.assert_called_once()
        self.assertEqual([item["content"] for item in self.store.list("A")], ["2"])
        self.assertTrue(_events(trace)[0]["result_summary"]["recovered"])
        messages = self.model.decide.call_args.args[0]
        outputs = [json.loads(msg["content"])["result"] for msg in messages if msg["role"] == "tool"]
        self.assertTrue(outputs[0]["recovered"])
        self.assertFalse(outputs[1]["created"])
        self.assertEqual(outputs[0]["todo"]["id"], outputs[1]["todo"]["id"])

    def test_uncertain_handler_outcome_stops_remaining_calls_and_model(self) -> None:
        create = self.store.create
        for invalid_return in (False, True):
            with self.subTest(invalid_return=invalid_return):
                session_id = str(invalid_return)

                def write_without_valid_reply(owner, content):
                    create(owner, content)
                    if invalid_return:
                        return {"invalid": {1}}
                    raise RuntimeError("simulated handler error after committing")

                self.model.reset_mock()
                self.model.decide.side_effect = [
                    _decision(
                        ("write", {"action": "create", "content": "2"}),
                        ("repeat", {"action": "create", "content": "2"}),
                    )
                ]
                with patch.object(self.store, "create", side_effect=write_without_valid_reply) as writes:
                    with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
                        result = self.runtime.run("create a todo", session_id)
                self.assertEqual(result["status"], "failed")
                self.assertIn("无法核实", result["answer"])
                self.model.decide.assert_called_once()
                writes.assert_called_once()
                self.assertEqual([item["content"] for item in self.store.list(session_id)], ["2"])
                events = _events(trace)
                self.assertEqual([event["started"] for event in events], [True, False])
                self.assertEqual([event["error_code"] for event in events], [
                    "tool_state_uncertain", "tool_skipped"
                ])
