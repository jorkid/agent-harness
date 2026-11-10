import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from agent_harness.context import MAX_USER_CHARS, build_messages, split_for_compaction
from agent_harness.model import Model, ModelError
from agent_harness.runtime import Runtime
from agent_harness.session import SessionError, SessionStore
from agent_harness.tools import create_default_tool_manager


def _final(text="answer"):
    return {"kind": "final", "text": text}


def _user(text):
    return {"role": "user", "content": text}


def _history(count):
    return [_user(f"temporary request {index}") for index in range(count)]


def _tool_call(name, arguments, call_id):
    return {
        "kind": "tool_calls",
        "tool_calls": [{"tool_call_id": call_id, "name": name, "arguments": arguments}],
        "assistant_message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }],
        },
    }


class CompactionTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="agent-harness-compaction-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.sessions = SessionStore(self.directory / "sessions")
        self.model = Mock(spec=Model)
        self.runtime = Runtime(self.model, create_default_tool_manager(), sessions=self.sessions)
        self.session_id = self.runtime.create_session()

    def _seed(self, messages, **fields):
        state = self.sessions.load(self.session_id)
        self.sessions.save(
            self.session_id, {**state, "messages": messages, **fields}, expected=state
        )

    def test_twenty_messages_does_not_trigger_compaction(self):
        self._seed(_history(18))
        self.model.decide.return_value = _final()
        self.assertEqual(self.runtime.run("current", self.session_id)["status"], "completed")
        self.model.decide.assert_called_once()
        self.assertEqual(len(self.sessions.load(self.session_id)["messages"]), 20)

    def test_compaction_keeps_entire_tool_batch_at_cut(self):
        assistant = _tool_call("calculator", {"expression": "1+1"}, "first")["assistant_message"]
        assistant["tool_calls"].extend(
            _tool_call("search", {"query": "python"}, "second")["assistant_message"]["tool_calls"]
        )
        group = [assistant, *[
            {"role": "tool", "tool_call_id": call_id, "content": "{}"}
            for call_id in ("first", "second")
        ]]
        self._seed([*_history(12), *group, *_history(6)])
        self.model.decide.side_effect = [_final(), _final("[]"), _final("old requests")]
        self.assertEqual(self.runtime.run("current", self.session_id)["status"], "completed")
        state = self.sessions.load(self.session_id)
        self.assertEqual(len(state["messages"]), 11)
        self.assertEqual(state["messages"][:3], group)
        self.assertEqual(build_messages(state["messages"], "next")[1:4], group)
        payload = json.loads(self.model.decide.call_args_list[2].args[0][1]["content"])
        self.assertEqual(payload["older_messages"], _history(12))

    def test_rolls_prior_summary_and_restores_memory_in_new_process(self):
        quote = "Always report decimal answers to two places."
        self._seed([_user(quote), *_history(18)])
        self.model.decide.side_effect = [
            _final(), _final("[0]"), _final("summary one"),
            _final(), _final("[0]"), _final("summary two"),
        ]
        self.runtime.run("first", self.session_id)
        first = self.sessions.load(self.session_id)
        self.assertEqual(first["memory"], [quote])
        self.sessions.append_messages(self.session_id, _history(9))
        self.runtime.run("second", self.session_id)
        followup = self.model.decide.call_args_list[3].args[0]
        self.assertIn(quote, followup[0]["content"])
        self.assertIn("summary one", followup[0]["content"])
        merge = json.loads(self.model.decide.call_args_list[5].args[0][1]["content"])
        self.assertEqual(merge["previous_summary"], "summary one")

        probe = """
import json, sys
from pathlib import Path
from agent_harness.context import build_messages
from agent_harness.session import SessionStore
state = SessionStore(Path(sys.argv[1])).load(sys.argv[2])
print(json.dumps(build_messages(state["messages"], "after restart",
      summary=state["summary"], memory=state["memory"])))
"""
        process = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(self.directory / "sessions"), self.session_id],
            capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        restored = json.loads(process.stdout)
        state = self.sessions.load(self.session_id)
        self.assertEqual(restored[1:-1], state["messages"])
        self.assertIn(quote, restored[0]["content"])
        self.assertIn("summary two", restored[0]["content"])

    def test_hundredth_turn_reuses_preference_from_memory(self):
        quote = "计算结果一律保留两位小数。"
        final_request = "用计算器计算 7/2。"
        maintenance_summaries = 0

        def decide(messages, definitions=()):
            nonlocal maintenance_summaries
            last = messages[-1]
            if not definitions:
                source = json.loads(last["content"])
                if isinstance(source, list):
                    return _final(json.dumps([i for i, candidate in enumerate(source) if candidate == quote]))
                maintenance_summaries += 1
                return _final("Earlier turns contained temporary requests.")
            if last["role"] == "tool":
                self.assertIn(quote, messages[0]["content"])
                value = json.loads(last["content"])["result"]["value"]
                return _final(f"{value:.2f}")
            if last["content"] == final_request:
                self.assertIn(quote, messages[0]["content"])
                self.assertNotIn(quote, [entry.get("content") for entry in messages[1:]])
                return _tool_call("calculator", {"expression": "7/2"}, "hundredth-turn")
            return _final("收到。")

        self.model.decide.side_effect = decide
        self.assertEqual(self.runtime.run(quote, self.session_id)["status"], "completed")
        for turn in range(2, 100):
            self.assertEqual(self.runtime.run(f"临时请求 {turn}", self.session_id)["status"], "completed")
            self.assertLessEqual(len(self.sessions.load(self.session_id)["messages"]), 20)

        self.assertGreater(maintenance_summaries, 1)
        before = self.sessions.load(self.session_id)
        self.assertEqual(before["memory"], [quote])
        self.assertNotIn(quote, before["summary"])
        restarted = Runtime(
            self.model, create_default_tool_manager(),
            sessions=SessionStore(self.directory / "sessions"),
        )
        result = restarted.run(final_request, self.session_id)
        self.assertEqual((result["status"], result["answer"]), ("completed", "3.50"))

    def test_compacted_context_supports_todo_followup(self):
        self._seed(_history(19))
        create = _tool_call("todo", {"action": "create", "content": "write paper"}, "create")
        self.model.decide.side_effect = [create, _final(), _final("[]"), _final("older requests")]
        self.runtime.run("create a todo", self.session_id)
        original, = self.sessions.load(self.session_id)["todos"]

        def followup(messages, definitions):
            if messages[-1]["role"] == "tool":
                return _final("rescheduled")
            earlier = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
            self.assertEqual(earlier[-1]["result"]["todo"], original)
            return _tool_call("todo", {
                "action": "update", "todo_id": original["id"], "content": "write paper tomorrow",
            }, "update")

        self.model.decide.side_effect = followup
        self.assertEqual(
            self.runtime.run("move that one to tomorrow", self.session_id)["answer"], "rescheduled"
        )
        self.assertEqual(self.sessions.load(self.session_id)["todos"], [
            {**original, "content": "write paper tomorrow"}
        ])

    def test_failed_maintenance_preserves_history_and_prior_notes(self):
        for responses in (
            [ModelError("memory failure")],
            [_final("[999]")],
            [_final("[0]"), ModelError("summary failure")],
            [_final("[0]"), _final("x" * 2001)],
        ):
            with self.subTest(responses=responses):
                history = _history(19)
                self._seed(history, memory=["existing quote"], summary="prior summary")
                self.model.decide.side_effect = [_final(), *responses]
                self.assertEqual(self.runtime.run("current", self.session_id)["status"], "completed")
                state = self.sessions.load(self.session_id)
                self.assertEqual(state["messages"], [
                    *history, _user("current"), {"role": "assistant", "content": "answer"},
                ])
                self.assertEqual(state["summary"], "prior summary")
                self.assertEqual(state["memory"], ["existing quote"])

    def test_memory_can_only_select_exact_user_quotes(self):
        quote = "My fixed preference is metric units."
        older = [
            _user(quote),
            {"role": "assistant", "content": "Speculation: the user lives on Mars."},
            *_history(17),
        ]
        self._seed(older, summary="Another speculation")
        self.model.decide.side_effect = [_final(), _final("[0]"), _final("unverified context")]
        self.runtime.run("current", self.session_id)
        candidates = json.loads(self.model.decide.call_args_list[1].args[0][1]["content"])
        self.assertNotIn("Another speculation", candidates)
        self.assertTrue(all("Mars" not in entry for entry in candidates))
        self.assertEqual(self.sessions.load(self.session_id)["memory"], [quote])

    def test_save_conflict_keeps_concurrent_todo_and_history(self):
        self._seed(_history(19))
        other_todo = {"id": "existing", "content": "confirmed elsewhere"}
        decisions = iter([_final(), _final("[]"), _final("candidate summary")])

        def decide(messages, tools=()):
            result = next(decisions)
            if result["text"] == "candidate summary":
                current = self.sessions.load(self.session_id)
                self.sessions.save(self.session_id, {
                    **current, "todos": [other_todo],
                    "messages": [*current["messages"], _user("newer entry")],
                }, expected=current)
            return result

        self.model.decide.side_effect = decide
        self.runtime.run("current", self.session_id)
        state = self.sessions.load(self.session_id)
        self.assertEqual(state["todos"], [other_todo])
        self.assertEqual(len(state["messages"]), 22)
        self.assertEqual(state["messages"][-1], _user("newer entry"))
        self.assertEqual(state["summary"], "")

    def test_compaction_read_failure_keeps_appended_history(self):
        self._seed(_history(19))
        original = self.sessions.load

        def load_once_unavailable(session_id):
            if load.call_count == 3:
                raise SessionError("read unavailable during maintenance")
            return original(session_id)

        self.model.decide.return_value = _final()
        with patch.object(self.sessions, "load", side_effect=load_once_unavailable) as load:
            self.assertEqual(self.runtime.run("current", self.session_id)["status"], "completed")
        self.model.decide.assert_called_once()
        self.assertEqual(len(original(self.session_id)["messages"]), 21)

    def test_note_bounds_and_session_isolation(self):
        self._seed([], summary="s" * 3000, memory=["secret synthetic quote", "m" * 241] + [
            f"quote {index}" for index in range(15)
        ])
        other = self.runtime.create_session()
        self.model.decide.return_value = _final()
        self.runtime.run("current", self.session_id)
        self.runtime.run("current", other)
        system = self.model.decide.call_args_list[0].args[0][0]["content"]
        notes = json.loads(system.splitlines()[-1])
        self.assertEqual(len(notes["unverified_summary"]), 2000)
        self.assertEqual(len(notes["historical_user_quotes"]), 16)
        self.assertNotIn("m" * 241, notes["historical_user_quotes"])
        other_system = self.model.decide.call_args_list[1].args[0][0]["content"]
        self.assertNotIn("secret synthetic quote", other_system)
        self.assertNotIn("unverified_summary", other_system)

    def test_oversized_user_message_does_not_call_model_or_compact(self):
        self._seed(_history(21))
        before = self.sessions.load(self.session_id)
        result = self.runtime.run("x" * (MAX_USER_CHARS + 1), self.session_id)
        self.assertEqual(result["status"], "failed")
        self.model.decide.assert_not_called()
        self.assertEqual(self.sessions.load(self.session_id), before)

    def test_oversized_history_group_is_not_partially_injected(self):
        assistant = _tool_call("calculator", {"expression": "1+1"}, "large")["assistant_message"]
        history = [assistant, {"role": "tool", "tool_call_id": "large", "content": "x" * 64000}]
        messages = build_messages(history, "current")
        self.assertEqual([msg["role"] for msg in messages], ["system", "user"])
        self.assertIsNone(split_for_compaction(history))

    def test_defers_compaction_if_fewer_than_two_model_calls_remain(self):
        self._seed(_history(19))
        self.model.decide.side_effect = [
            *[_tool_call("calculator", {"expression": "1+1"}, f"call-{i}") for i in range(6)],
            _final(),
        ]
        self.assertEqual(self.runtime.run("current", self.session_id)["status"], "completed")
        self.assertEqual(self.model.decide.call_count, 7)
        self.assertGreater(len(self.sessions.load(self.session_id)["messages"]), 20)

    def test_long_backlog_drains_in_batches_on_later_requests(self):
        quote = "Always use two decimal places."
        history = []
        for i in range(16):
            history.extend([
                _user(quote if i == 0 else "u" * 7900),
                {"role": "assistant", "content": "a" * 5000},
            ])
        self._seed(history)
        self.model.decide.side_effect = [
            _final(), _final("[0]"), _final("first batch"),
            _final(), _final("[0]"), _final("second batch"),
        ]
        for round_number in range(2):
            before = self.sessions.load(self.session_id)
            self.assertEqual(self.runtime.run("short followup", self.session_id)["status"], "completed")
            after = self.sessions.load(self.session_id)
            extract, summarize = [
                call.args[0] for call in self.model.decide.call_args_list[round_number * 3 + 1:
                                                                       round_number * 3 + 3]
            ]
            for request in (extract, summarize):
                self.assertLessEqual(len(json.dumps(request, ensure_ascii=False, separators=(",", ":"))), 64000)
            source = json.loads(summarize[1]["content"])
            self.assertEqual(source["previous_summary"], before["summary"])
            appended = [*before["messages"], _user("short followup"),
                        {"role": "assistant", "content": "answer"}]
            count = len(source["older_messages"])
            self.assertEqual(source["older_messages"], appended[:count])
            self.assertEqual(after["messages"], appended[count:])
            self.assertLess(len(after["messages"]), len(before["messages"]))
            self.assertEqual(after["memory"], [quote])
        self.assertIn(quote, self.model.decide.call_args_list[3].args[0][0]["content"])

    def test_oversized_old_group_is_retained_without_blocking_later_groups(self):
        assistant = _tool_call("todo", {"action": "list"}, "big")["assistant_message"]
        group = [assistant, {"role": "tool", "tool_call_id": "big", "content": "x" * 70000}]
        self._seed([*group, *_history(19)])
        self.model.decide.side_effect = [_final(), _final("[]"), _final("smaller groups")]
        with self.assertLogs("agent_harness.runtime", level="WARNING") as logs:
            self.assertEqual(self.runtime.run("current", self.session_id)["status"], "completed")
        self.assertIn("context_compaction_retained_oversized_groups", logs.output[0])
        self.assertEqual(self.sessions.load(self.session_id)["messages"][:2], group)
        self.assertEqual(len(self.sessions.load(self.session_id)["messages"]), 12)
        payload = json.loads(self.model.decide.call_args_list[2].args[0][1]["content"])
        self.assertEqual(payload["older_messages"], _history(11))

    def test_sole_oversized_old_group_defers_without_maintenance_calls(self):
        calls = [
            _tool_call("todo", {"action": "list"}, f"call-{i}")["assistant_message"]["tool_calls"][0]
            for i in range(10)
        ]
        group = [{"role": "assistant", "content": None, "tool_calls": calls}, *[
            {"role": "tool", "tool_call_id": call["id"], "content": "x" * 7000}
            for call in calls
        ]]
        history = [*group, *_history(8)]
        self._seed(history)
        self.model.decide.return_value = _final()
        with self.assertLogs("agent_harness.runtime", level="WARNING") as logs:
            self.assertEqual(self.runtime.run("short", self.session_id)["status"], "completed")
        self.model.decide.assert_called_once()
        self.assertIn("context_compaction_deferred", logs.output[0])
        self.assertEqual(self.sessions.load(self.session_id)["messages"], [
            *history, _user("short"), {"role": "assistant", "content": "answer"},
        ])

    def test_large_todo_list_is_bounded_before_next_decision_and_survives_reload(self):
        todos = [{"id": f"{i:032x}", "content": f"{i:03d}" + "x" * 497} for i in range(135)]
        self._seed([], todos=todos)
        self.model.decide.side_effect = [
            _tool_call("todo", {"action": "list"}, "todo-list"), _final(),
        ]
        result = self.runtime.run("list todos", self.session_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.model.decide.call_count, 2)

        probe = """
import json, sys
from pathlib import Path
from agent_harness.context import build_messages
from agent_harness.session import SessionStore
state = SessionStore(Path(sys.argv[1])).load(sys.argv[2])
build_messages(state["messages"], "after restart")
print(json.dumps(state))
"""
        process = subprocess.run(
            [sys.executable, "-B", "-c", probe, str(self.directory / "sessions"), self.session_id],
            capture_output=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        state = json.loads(process.stdout)
        saved = state["messages"][2]
        replayed = self.model.decide.call_args_list[1].args[0][-1]
        self.assertEqual(saved, replayed)
        self.assertEqual(saved["tool_call_id"], "todo-list")
        self.assertLessEqual(len(saved["content"]), 8000)
        bounded = json.loads(saved["content"])
        note = bounded["truncation"]
        self.assertEqual(note, {
            "truncated": True, "original_chars": 75500,
            "total_items": 135, "shown_items": len(bounded["result"]["todos"]),
        })
        self.assertTrue(0 < note["shown_items"] < len(todos))
        self.assertEqual(bounded["result"]["todos"], todos[:note["shown_items"]])
        self.assertEqual(state["todos"], todos)

    def test_large_final_answer_is_bounded_and_identical_to_history(self):
        self.model.decide.return_value = _final("x" * 65000)
        result = self.runtime.run("long answer", self.session_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["answer"]), 8000)
        self.assertTrue(result["answer"].endswith("[回答已截断]"))
        self.assertEqual(self.sessions.load(self.session_id)["messages"][-1], {
            "role": "assistant", "content": result["answer"],
        })
