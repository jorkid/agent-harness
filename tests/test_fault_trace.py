import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai import APIConnectionError, APITimeoutError

from agent_harness import cli
from agent_harness.config import Settings
from agent_harness.model import Model, ModelError
from agent_harness.runtime import Runtime
from agent_harness.session import SessionError, SessionStore
from agent_harness.tools import ToolDefinition, create_default_tool_manager


_FINAL = {"kind": "final", "text": "confirmed reply"}


def _batch(*calls):
    return {
        "kind": "tool_calls",
        "tool_calls": [
            {"tool_call_id": call_id, "name": name, "arguments": arguments}
            for call_id, name, arguments in calls
        ],
        "assistant_message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function",
                 "function": {"name": name, "arguments": json.dumps(arguments)}}
                for call_id, name, arguments in calls
            ],
        },
    }


def _todo_create(call_id, content):
    return call_id, "todo", {"action": "create", "content": content}


def _events(logs):
    return [
        json.loads(payload)
        for record in logs.records
        for prefix, _, payload in [record.getMessage().partition(" ")]
        if prefix in ("request_trace", "tool_trace")
    ]


def _feedback(state):
    return [
        json.loads(message["content"]) for message in state["messages"] if message["role"] == "tool"
    ]


class FaultTraceTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(TemporaryDirectory())
        self.directory = Path(temp) / "sessions"
        self.sessions = SessionStore(self.directory)
        self.session_id = self.sessions.create()
        self.model = Mock(spec=Model)
        self.tools = create_default_tool_manager()

    def _state(self):
        return SessionStore(self.directory).load(self.session_id)

    def _run(self, model=None):
        runtime = Runtime(model if model is not None else self.model, self.tools, sessions=self.sessions)
        with self.assertLogs("agent_harness.runtime", level="INFO") as logs:
            result = runtime.run("private user input", self.session_id)
        events = _events(logs)
        self.assertEqual(events[-1]["event"], "request_finished")
        self.assertEqual(events[-1]["status"], result["status"])
        return result, events

    def _register(self, handler, schema=None):
        self.tools.register(ToolDefinition("probe", "Test probe", schema or {"type": "object"}, handler))

    def _check_tool(self, events, call_id, status, error=None):
        actual = [event for event in events if event.get("tool_call_id") == call_id]
        started = status in ("succeeded", "failed", "unconfirmed")
        expected_phases = ["tool_requested", "tool_started", "tool_result"] if started else [
            "tool_requested", "tool_result",
        ]
        self.assertEqual([event["event"] for event in actual], expected_phases)
        self.assertTrue(all(event["proposed"] for event in actual))
        self.assertFalse(actual[0]["started"])
        self.assertEqual(actual[-1]["started"], started)
        self.assertEqual(actual[-1]["status"], status)
        self.assertEqual(actual[-1]["ok"], status == "succeeded")
        self.assertEqual(actual[-1].get("error_code"), error)
        return actual

    def _sdk_failure(self, expected_code, *, error=None, response=None):
        with patch("agent_harness.model.OpenAI") as factory:
            create = factory.return_value.chat.completions.create
            create.side_effect = error
            create.return_value = response
            model = Model(Settings("test-key", "https://example.invalid", "test-model"))
            result, events = self._run(model)
            create.assert_called_once()
        self.assertEqual(result["status"], "failed")
        self.assertIn("未获得最终回答", result["answer"])
        self.assertEqual(self._state()["messages"], [{"role": "user", "content": "private user input"}])
        self.assertEqual(events[-1]["model_calls"], 1)
        self.assertEqual([e["error_code"] for e in events if e["event"] == "model_error"], [expected_code])

    def test_sdk_timeout(self):
        self._sdk_failure("model_timeout", error=APITimeoutError(request=Mock()))

    def test_sdk_connection_error(self):
        self._sdk_failure("model_connection_error", error=APIConnectionError(request=Mock()))

    def test_sdk_invalid_response(self):
        self._sdk_failure("model_invalid_response", response=SimpleNamespace(choices=[]))

    def test_unknown_tool(self):
        self.model.decide.side_effect = [_batch(("unknown", "missing", {})), _FINAL]
        result, events = self._run()
        self.assertEqual(result["answer"], _FINAL["text"])
        self._check_tool(events, "call-1-1", "rejected", "unknown_tool")
        self.assertEqual(_feedback(self._state())[0]["error"]["code"], "unknown_tool")

    def test_missing_and_wrong_type_arguments_never_start_handler(self):
        handler = Mock()
        self._register(handler, {
            "type": "object", "required": ["query"], "properties": {"query": {"type": "string"}},
        })
        self.model.decide.side_effect = [_batch(
            ("missing", "probe", {}), ("type", "probe", {"query": 1}),
        ), _FINAL]
        _, events = self._run()
        handler.assert_not_called()
        for call_id in ("call-1-1", "call-1-2"):
            self._check_tool(events, call_id, "rejected", "invalid_arguments")
        self.assertEqual([item["error"]["code"] for item in _feedback(self._state())],
                         ["invalid_arguments", "invalid_arguments"])

    def test_invalid_schema_never_starts_handler(self):
        handler = Mock()
        self._register(handler, {"type": "object", "$ref": "#/$defs/missing"})
        self.model.decide.side_effect = [_batch(("schema", "probe", {})), _FINAL]
        _, events = self._run()
        handler.assert_not_called()
        self._check_tool(events, "call-1-1", "rejected", "invalid_tool_schema")
        self.assertEqual(_feedback(self._state())[0]["error"]["code"], "invalid_tool_schema")

    def _handler_failure(self, failure, expected_code):
        handler = Mock(side_effect=failure)
        self._register(handler)
        self.model.decide.side_effect = [_batch(("probe-call", "probe", {})), _FINAL]
        result, events = self._run()
        handler.assert_called_once_with({})
        self.assertEqual(result["answer"], _FINAL["text"])
        self._check_tool(events, "call-1-1", "failed", expected_code)
        self.assertEqual(_feedback(self._state())[0]["error"]["code"], expected_code)
        self.assertNotIn(str(failure), json.dumps(events))

    def test_handler_exception(self):
        self._handler_failure(RuntimeError("internal private exception body"), "tool_execution_failed")

    def test_handler_timeout(self):
        self._handler_failure(TimeoutError("internal private timeout body"), "tool_timeout")

    def test_model_corrects_failure_and_finishes(self):
        def decide(messages, _definitions):
            if self.model.decide.call_count == 1:
                return _batch(("failed", "calculator", {"expression": "8/0"}))
            feedback = json.loads(messages[-1]["content"])
            if self.model.decide.call_count == 2:
                self.assertEqual(feedback["error"]["code"], "division_by_zero")
                return _batch(("retry", "calculator", {"expression": "8/2"}))
            self.assertEqual(feedback["result"]["value"], 4)
            return _FINAL

        self.model.decide.side_effect = decide
        result, events = self._run()
        self.assertEqual(result["answer"], _FINAL["text"])
        self._check_tool(events, "call-1-1", "failed", "division_by_zero")
        self._check_tool(events, "call-2-1", "succeeded")
        self.assertEqual(events[-1]["model_calls"], 3)
        self.assertEqual(self._state()["messages"][-1], {"role": "assistant", "content": _FINAL["text"]})

    def test_uncertain_todo_skips_remaining_batch_without_retry(self):
        self.model.decide.return_value = _batch(
            _todo_create("unconfirmed", "may have committed"), _todo_create("skipped", "must not exist"),
        )
        original_load = self.sessions.load

        def fail_after_replace(session_id):
            state = original_load(session_id)
            if read.call_count == 4:
                self.assertEqual([todo["content"] for todo in state["todos"]], ["may have committed"])
                raise SessionError("injected readback failure")
            return state

        with patch.object(self.sessions, "load", side_effect=fail_after_replace) as read:
            result, events = self._run()
        self.assertEqual(result["status"], "failed")
        self.assertIn("无法核实", result["answer"])
        self.model.decide.assert_called_once()
        self._check_tool(events, "call-1-1", "unconfirmed", "tool_state_uncertain")
        self._check_tool(events, "call-1-2", "skipped", "tool_skipped")
        self.assertEqual(events[-1]["successful_tools"], 0)
        state = self._state()
        self.assertEqual([todo["content"] for todo in state["todos"]], ["may have committed"])
        self.assertEqual([item["error"]["code"] for item in _feedback(state)],
                         ["tool_state_uncertain", "tool_skipped"])

    def _run_cli(self):
        with (
            patch.object(cli, "SessionStore", return_value=self.sessions),
            patch.object(cli, "load_settings"),
            patch.object(cli, "Model", return_value=self.model),
            redirect_stdout(io.StringIO()) as stdout,
            self.assertLogs("agent_harness.runtime", level="INFO") as logs,
        ):
            exit_code = cli.main(["--session", self.session_id, "synthetic CLI input"])
        result = json.loads(stdout.getvalue())
        self.assertEqual(result["session_id"], self.session_id)
        events = _events(logs)
        self.assertEqual(events[-1]["status"], result["status"])
        return exit_code, result, events

    def test_cli_reports_model_error_after_confirmed_todo(self):
        self.model.decide.side_effect = [
            _batch(_todo_create("committed", "confirmed todo")), ModelError("injected model failure"),
        ]
        exit_code, result, events = self._run_cli()
        self.assertEqual((exit_code, result["status"]), (1, "failed"))
        self.assertIn("未获得最终回答", result["answer"])
        self._check_tool(events, "call-1-1", "succeeded")
        self.assertEqual(events[-2]["error_code"], "model_error")
        self.assertEqual(events[-1]["successful_tools"], 1)
        state = self._state()
        self.assertEqual(state["todos"], [_feedback(state)[0]["result"]["todo"]])
        self.assertEqual(state["messages"][-1]["role"], "tool")

    def test_cli_reports_budget_exhaustion_after_confirmed_todo(self):
        decisions = [
            _batch(_todo_create("committed", "confirmed todo")),
            *[_batch((f"list-{i}", "todo", {"action": "list"})) for i in range(6)],
            _batch(_todo_create("skipped", "must not exist")),
        ]
        process = self._cli_process("--trace", decisions=decisions)
        result = json.loads(process.stdout)
        events = [json.loads(line) for line in process.stderr.splitlines()]
        self.assertEqual((process.returncode, result["status"]), (1, "limit_reached"))
        self.assertEqual(set(result), {"session_id", "answer", "status"})
        self.assertEqual(result["session_id"], self.session_id)
        self.assertEqual((events[-1]["event"], events[-1]["status"]), ("request_finished", result["status"]))
        self.assertIn("未获得最终回答", result["answer"])
        self._check_tool(events, "call-1-1", "succeeded")
        self._check_tool(events, "call-8-1", "skipped", "limit_reached")
        self.assertEqual(events[-2]["event"], "budget_exhausted")
        self.assertEqual((events[-1]["model_calls"], events[-1]["successful_tools"]), (8, 7))
        state = self._state()
        self.assertEqual([todo["content"] for todo in state["todos"]], ["confirmed todo"])
        self.assertEqual(_feedback(state)[-1]["error"]["code"], "limit_reached")
        self.assertEqual(state["messages"][-1]["role"], "tool")

    def test_context_summary_failure_keeps_raw_history_and_charges_budget(self):
        before = [{"role": "user", "content": f"fixture {i}"} for i in range(19)]
        self.sessions.append_messages(self.session_id, before)
        self.model.decide.side_effect = [
            _FINAL, {"kind": "final", "text": "[]"}, ModelError("private detail", code="model_timeout"),
        ]
        result, events = self._run()
        self.assertEqual(result["answer"], _FINAL["text"])
        self.assertEqual(events[-1]["model_calls"], 3)
        maintenance, = [e for e in events if e["event"] == "context_maintenance_failed"]
        self.assertEqual((maintenance["stage"], maintenance["error_code"]), ("summary", "model_timeout"))
        state = self._state()
        self.assertEqual(state["messages"], [
            *before, {"role": "user", "content": "private user input"},
            {"role": "assistant", "content": _FINAL["text"]},
        ])
        self.assertEqual((state["summary"], state["memory"]), ("", []))

    def test_context_maintenance_uses_two_remaining_calls(self):
        self.sessions.append_messages(self.session_id, [
            {"role": "user", "content": f"fixture {i}"} for i in range(19)
        ])
        self.model.decide.side_effect = [
            *[_batch((f"calc-{i}", "calculator", {"expression": "1+1"})) for i in range(5)],
            _FINAL,
            {"kind": "final", "text": "[]"},
            {"kind": "final", "text": "compacted summary"},
        ]
        result, events = self._run()
        self.assertEqual(result["answer"], _FINAL["text"])
        self.assertEqual((events[-1]["model_calls"], events[-1]["successful_tools"]), (8, 5))
        self.assertEqual(self.model.decide.call_count, 8)
        self.assertEqual(self._state()["summary"], "compacted summary")

    def test_trace_is_bounded_and_redacts_arguments_results_and_raw_inputs(self):
        secret = "sk-" + "a" * 32
        request_value = "private-query-" + "x" * 6000
        result_value = "private-result-" + "y" * 4000
        self._register(Mock(return_value={"secret": secret, "content": result_value}))
        self.model.decide.side_effect = [
            _batch(
                ("probe-call", "probe", {"query": request_value, "Authorization": f"Bearer {secret}"}),
                (secret, "x" * 200, {}),
            ),
            _FINAL,
        ]
        _, events = self._run()
        start, _, finish = self._check_tool(events, "call-1-1", "succeeded")
        self.assertEqual(start["parameters"], {
            "fields": {"query": {"type": "string", "chars": len(request_value)}}, "other_field_count": 1,
        })
        self.assertEqual(finish["result_summary"], {"field_count": 2})
        redacted, _ = self._check_tool(events, "call-1-2", "rejected", "unknown_tool")
        self.assertEqual(redacted["tool_name"], "unknown_tool")
        for event in events:
            payload = json.dumps(event)
            self.assertLess(len(payload), 900)
            for private in (secret, "Authorization", "private-query", "private-result",
                            "private user input", _FINAL["text"]):
                self.assertNotIn(private, payload)

    def test_start_event_precedes_handler_and_events_share_request_order(self):
        with self.assertLogs("agent_harness.runtime", level="INFO") as logs:
            def execute(_arguments):
                events = _events(logs)
                self.assertEqual(events[-1]["event"], "tool_started")
                self.assertEqual(events[-1]["tool_call_id"], "call-1-1")
                return {"count": 1}

            self._register(execute)
            self.model.decide.side_effect = [
                _batch(("valid", "probe", {}), ("missing", "missing", {})), _FINAL,
            ]
            runtime = Runtime(self.model, self.tools, sessions=self.sessions)
            result = runtime.run("synthetic", self.session_id)
        self.assertEqual(result["status"], "completed")
        events = _events(logs)
        self._check_tool(events, "call-1-1", "succeeded")
        self._check_tool(events, "call-1-2", "rejected", "unknown_tool")
        self.assertEqual([e["event"] for e in events], [
            "request_started", "tool_requested", "tool_requested", "tool_started",
            "tool_result", "tool_result", "request_finished",
        ])
        self.assertEqual(len({e["request_id"] for e in events}), 1)
        self.assertEqual({e["session_id"] for e in events}, {self.session_id})
        self.assertEqual([e["sequence"] for e in events], list(range(1, len(events) + 1)))
        self.assertTrue(all(datetime.fromisoformat(e["timestamp"]).utcoffset() is not None for e in events))
        self.assertEqual([e["tool_index"] for e in events if e["event"] == "tool_requested"], [1, 2])

    def test_untrusted_tool_names_and_ids_never_enter_trace(self):
        secrets = ("sk-" + "a" * 32, "PrefixlessSyntheticCredential77")
        handler = Mock(return_value={"value": 1})
        self._register(handler)
        self.model.decide.side_effect = [
            _batch(
                (secrets[0], f"unknown-{secrets[0]}", {}),
                (secrets[1], f"unknown-{secrets[1]}", {}),
                ("call-prefix-" + secrets[1], "probe", {}),
            ),
            _FINAL,
        ]
        result, events = self._run()
        handler.assert_called_once_with({})
        self.assertEqual(result["status"], "completed")
        for position, secret in enumerate(secrets, 1):
            requested, rejected = self._check_tool(events, f"call-1-{position}", "rejected", "unknown_tool")
            self.assertEqual(requested["request_id"], rejected["request_id"])
            self.assertEqual(requested["tool_name"], "unknown_tool")
            self.assertEqual(rejected["tool_name"], "unknown_tool")
            self.assertNotIn(secret, json.dumps(events))
        started = self._check_tool(events, "call-1-3", "succeeded")
        self.assertTrue(all(event["tool_name"] == "probe" for event in started))
        tools = [message for message in self._state()["messages"] if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tools],
                         [*secrets, "call-prefix-" + secrets[1]])

    def _cli_process(self, *flags, session_id=None, model_fails=False, config_fails=False, decisions=None):
        if decisions is None:
            decisions = [_batch(("provider-call", "calculator", {"expression": "17+25"})), _FINAL]
        script = f"""
from unittest.mock import Mock, patch
from agent_harness import cli
from agent_harness.config import ConfigError
from agent_harness.model import ModelError

model = Mock()
model_failure = ModelError("private response body", code="model_timeout")
model.decide.side_effect = model_failure if {model_fails!r} else {decisions!r}
config_failure = ConfigError("synthetic credential and C:/private/sessions")
with (
    patch.object(cli, "load_settings", side_effect=config_failure if {config_fails!r} else None),
    patch.object(cli, "Model", return_value=model),
):
    raise SystemExit(cli.main())
"""
        target = session_id if session_id is not None else self.session_id
        return subprocess.run(
            [sys.executable, "-B", "-c", script, *flags, "--session", target, "synthetic input"],
            cwd=self.directory.parent, capture_output=True, encoding="utf-8", timeout=30,
        )

    def test_cli_trace_streams_json_lines_to_stderr(self):
        process = self._cli_process("--trace")
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {
            "session_id": self.session_id, "answer": _FINAL["text"], "status": "completed",
        })
        events = [json.loads(line) for line in process.stderr.splitlines()]
        self.assertEqual([event["event"] for event in events], [
            "request_started", "tool_requested", "tool_started", "tool_result", "request_finished",
        ])
        self._check_tool(events, "call-1-1", "succeeded")
        self.assertEqual(len({event["request_id"] for event in events}), 1)
        self.assertEqual(events[-1]["status"], "completed")
        for raw in ("provider-call", "synthetic input", "17+25"):
            self.assertNotIn(raw, process.stderr)
        self.assertEqual(_feedback(self._state()), [{"ok": True, "result": {"value": 42}}])

    def test_cli_without_trace_preserves_stdout_and_stderr(self):
        process = self._cli_process()
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {
            "session_id": self.session_id, "answer": _FINAL["text"], "status": "completed",
        })
        self.assertEqual(process.stderr, "")
        self.assertEqual(_feedback(self._state()), [{"ok": True, "result": {"value": 42}}])

    def test_cli_trace_missing_session_has_one_structured_failure(self):
        original = self._state()
        process = self._cli_process("--trace", session_id="0" * 32)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        events = [json.loads(line) for line in process.stderr.splitlines()]
        self.assertEqual([event["event"] for event in events], [
            "request_started", "session_error", "request_finished",
        ])
        self.assertEqual(events[1]["error_code"], "session_error")
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(len({event["request_id"] for event in events}), 1)
        self.assertEqual(self._state(), original)
        self.assertFalse((self.directory / ("0" * 32 + ".json")).exists())

    def test_cli_invalid_trace_usage_is_json_without_session_changes(self):
        original = self._state()
        process = subprocess.run(
            [sys.executable, "-B", "-m", "agent_harness.cli", "--trace", "--session"],
            cwd=self.directory.parent, capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual((process.returncode, process.stdout), (2, ""))
        self.assertEqual([json.loads(line) for line in process.stderr.splitlines()], [
            {"event": "cli_error", "status": "failed", "error_code": "invalid_trace_arguments"},
        ])
        self.assertEqual(self._state(), original)

    def test_cli_without_trace_preserves_missing_session_error(self):
        process = self._cli_process(session_id="0" * 32)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        self.assertEqual(process.stderr.strip(), "[会话错误] 会话不存在。")

    def test_cli_trace_model_failure_preserves_json_stdout_and_exit_code(self):
        process = self._cli_process("--trace", model_fails=True)
        self.assertEqual(process.returncode, 1)
        output = json.loads(process.stdout)
        self.assertEqual(set(output), {"session_id", "answer", "status"})
        self.assertEqual((output["session_id"], output["status"]), (self.session_id, "failed"))
        events = [json.loads(line) for line in process.stderr.splitlines()]
        self.assertEqual([event["event"] for event in events], [
            "request_started", "model_error", "request_finished",
        ])
        self.assertEqual(events[1]["error_code"], "model_timeout")
        self.assertEqual(events[-1]["status"], output["status"])
        self.assertNotIn("private response body", process.stderr + process.stdout)

    def test_cli_trace_setup_error_is_safe_json(self):
        process = self._cli_process("--trace", config_fails=True)
        self.assertEqual(process.returncode, 1)
        self.assertEqual(process.stdout, "")
        self.assertEqual([json.loads(line) for line in process.stderr.splitlines()], [
            {"event": "cli_error", "status": "failed", "error_code": "config_error"},
        ])

    def _seed_compaction(self):
        empty = self._state()
        initial = {
            **empty,
            "messages": [{"role": "user", "content": f"fixture {i}"} for i in range(19)],
            "todos": [{"id": "existing", "content": "confirmed todo"}],
        }
        self.sessions.save(self.session_id, initial, expected=empty)
        before = {**initial, "messages": [
            *initial["messages"], {"role": "user", "content": "private user input"},
            {"role": "assistant", "content": _FINAL["text"]},
        ]}
        candidate = {
            **before, "messages": before["messages"][-10:],
            "memory": ["fixture 0"], "summary": "candidate summary",
        }
        self.model.decide.side_effect = [
            _FINAL, {"kind": "final", "text": "[0]"}, {"kind": "final", "text": candidate["summary"]},
        ]
        return before, candidate

    def _check_compaction_outcome(self, result, events, status, error_code="session_error"):
        self.assertEqual((result["status"], result["answer"]), ("completed", _FINAL["text"]))
        self.assertEqual(events[-1]["model_calls"], 3)
        event, = [event for event in events if event["event"].startswith("context_maintenance_")]
        self.assertEqual((event["event"], event["stage"], event["status"], event["error_code"]),
                         (f"context_maintenance_{status}", "save", status, error_code))

    def _replace_fault(self, *, after, unreadable=False):
        original_replace = os.replace
        original_load = self.sessions.load
        attempts = 0
        reads_after_error = 0

        def replace(source, target):
            nonlocal attempts
            if not json.loads(Path(source).read_text(encoding="utf-8"))["summary"]:
                return original_replace(source, target)
            attempts += 1
            if after:
                original_replace(source, target)
            raise OSError("synthetic replace failure")

        def load(session_id):
            nonlocal reads_after_error
            if attempts:
                reads_after_error += 1
                if unreadable:
                    raise SessionError("synthetic readback failure")
            return original_load(session_id)

        with (
            patch("agent_harness.session.os.replace", side_effect=replace),
            patch.object(self.sessions, "load", side_effect=load),
        ):
            result, events = self._run()
        self.assertEqual(attempts, 1)
        self.assertEqual(reads_after_error, 1)
        return result, events

    def test_context_replace_failure_before_write_is_confirmed_failed(self):
        before, _ = self._seed_compaction()
        result, events = self._replace_fault(after=False)
        self._check_compaction_outcome(result, events, "failed")
        self.assertEqual(self._state(), before)

    def test_context_replace_failure_after_write_is_recovered(self):
        _, candidate = self._seed_compaction()
        result, events = self._replace_fault(after=True)
        self._check_compaction_outcome(result, events, "recovered")
        self.assertEqual(self._state(), candidate)

    def test_context_readback_failure_leaves_commit_unconfirmed(self):
        _, candidate = self._seed_compaction()
        result, events = self._replace_fault(after=True, unreadable=True)
        self._check_compaction_outcome(result, events, "unconfirmed")
        self.assertEqual(self._state(), candidate)

    def test_context_cas_conflict_never_credits_other_save(self):
        before, candidate = self._seed_compaction()
        original_save = self.sessions.save

        def concurrent_save(session_id, state, *, expected):
            self.assertEqual(state, candidate)
            self.assertEqual(expected, before)
            # Another writer has already committed the same candidate.
            original_save(session_id, candidate, expected=before)
            original_save(session_id, state, expected=expected)

        with patch.object(self.sessions, "save", side_effect=concurrent_save) as save:
            result, events = self._run()
        save.assert_called_once()
        self._check_compaction_outcome(result, events, "failed", "session_conflict")
        self.assertEqual(self._state(), candidate)

    def test_context_changed_snapshot_after_save_error_is_unconfirmed(self):
        before, candidate = self._seed_compaction()
        original_save = self.sessions.save
        concurrent = {**candidate, "todos": [
            *before["todos"], {"id": "other", "content": "another confirmed todo"},
        ]}

        def other_writer(session_id, state, *, expected):
            self.assertEqual(state, candidate)
            self.assertEqual(expected, before)
            original_save(session_id, concurrent, expected=before)
            raise SessionError("synthetic save error with a different authoritative snapshot")

        with patch.object(self.sessions, "save", side_effect=other_writer) as save:
            result, events = self._run()
        save.assert_called_once()
        self._check_compaction_outcome(result, events, "unconfirmed")
        self.assertEqual(self._state(), concurrent)

