import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from agent_harness import cli
from agent_harness.model import Model, ModelError
from agent_harness.runtime import Runtime
from agent_harness.session import SessionConflictError, SessionError, SessionStore
from agent_harness.tools import create_default_tool_manager


def _todo_call(**arguments) -> dict:
    call = {"tool_call_id": "todo-call", "name": "todo", "arguments": arguments}
    return {
        "kind": "tool_calls",
        "tool_calls": [call],
        "assistant_message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "todo-call",
                "type": "function",
                "function": {"name": "todo", "arguments": json.dumps(arguments)},
            }],
        },
    }


_FINAL = {"kind": "final", "text": "Done"}


class SessionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(prefix="agent-harness-session-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sessions = SessionStore(self.root / "sessions")
        self.model = Mock(spec=Model)
        self.runtime = Runtime(self.model, create_default_tool_manager(), sessions=self.sessions)

    def _execute(self, session_id: str, **arguments) -> dict:
        self.model.decide.side_effect = [_todo_call(**arguments), _FINAL]
        return self.runtime.run("test todo", session_id)

    def _tool_result(self) -> dict:
        messages = self.model.decide.call_args.args[0]
        return json.loads(messages[-1]["content"])

    def test_creates_distinct_sessions_and_preserves_json_fields(self) -> None:
        a = self.runtime.create_session()
        b = self.runtime.create_session()
        self.assertRegex(a, r"^[0-9a-f]{32}$")
        self.assertNotEqual(a, b)
        empty = {"messages": [], "todos": [], "memory": [], "summary": ""}
        self.assertEqual(self.sessions.load(a), empty)
        self.assertEqual(self.sessions.load(b), empty)
        state = {
            "messages": [{"role": "user", "content": "历史记录"}],
            "todos": [],
            "memory": ["偏好中文"],
            "summary": "测试摘要",
        }
        self.sessions.save(a, state, expected=empty)
        self._execute(a, action="create", content="a only")
        snapshot = self.sessions.load(a)
        self.assertEqual(snapshot["messages"][:1], state["messages"])
        self.assertEqual(snapshot["memory"], state["memory"])
        self.assertEqual(snapshot["summary"], state["summary"])
        self.assertEqual(len(snapshot["messages"]), 5)
        self.assertEqual(snapshot["todos"][0]["content"], "a only")
        self.assertEqual(self.sessions.load(b), empty)

    def test_stale_full_save_cannot_erase_new_todo(self) -> None:
        session_id = self.sessions.create()
        expected = self.sessions.load(session_id)
        stale_update = {**expected, "messages": [{"role": "user", "content": "stale"}]}
        self._execute(session_id, action="create", content="keep me")
        current = self.sessions.load(session_id)

        with self.assertRaisesRegex(SessionConflictError, "已过期"):
            self.sessions.save(session_id, stale_update, expected=expected)
        self.assertEqual(self.sessions.load(session_id), current)
        self.assertEqual(current["todos"][0]["content"], "keep me")

    def test_append_messages_keeps_latest_todo_id_memory_and_summary(self) -> None:
        session_id = self.sessions.create()
        self._execute(session_id, action="create", content="keep me")
        expected = self.sessions.load(session_id)
        initial_message = {"role": "user", "content": "previous message"}
        current = {
            **expected,
            "messages": [initial_message],
            "memory": ["latest memory"],
            "summary": "latest summary",
        }
        self.sessions.save(session_id, current, expected=expected)
        new_message = {"role": "assistant", "content": "new message"}

        self.sessions.append_messages(session_id, [new_message])

        self.assertEqual(
            self.sessions.load(session_id),
            {**current, "messages": [initial_message, new_message]},
        )

    def test_update_stops_if_authoritative_file_cannot_be_read(self) -> None:
        session_id = self.sessions.create()
        expected = self.sessions.load(session_id)
        path = self.root / "sessions" / f"{session_id}.json"
        before = path.read_bytes()
        messages = [{"role": "user", "content": "not saved"}]
        with (
            patch.object(self.sessions, "load", side_effect=SessionError("injected read error")),
            patch.object(self.sessions, "_replace", wraps=self.sessions._replace) as replace,
        ):
            with self.assertRaisesRegex(SessionError, "read error"):
                self.sessions.append_messages(session_id, messages)
            with self.assertRaisesRegex(SessionError, "read error"):
                self.sessions.save(session_id, {**expected, "messages": messages}, expected=expected)
            replace.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_runtime_rejects_todo_candidate_based_on_stale_snapshot(self) -> None:
        session_id = self.sessions.create()
        concurrent_todo = {"id": "external-todo", "content": "already confirmed"}
        responses = iter([_todo_call(action="create", content="stale create"), _FINAL])

        def decide(messages, definitions):
            if len(messages) == 2:
                before = self.sessions.load(session_id)
                self.sessions.save(
                    session_id, {**before, "todos": [concurrent_todo]}, expected=before
                )
            return next(responses)

        self.model.decide.side_effect = decide
        self.runtime.run("create", session_id)
        self.assertEqual(self._tool_result()["error"]["code"], "todo_save_conflict")
        self.assertEqual(self.sessions.load(session_id)["todos"], [concurrent_todo])

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_cli_rejects_junction_for_create_and_restore_without_external_changes(self) -> None:
        outside = self.root / "external"
        external_id = SessionStore(outside).create()
        expected = {path.name: path.read_bytes() for path in outside.iterdir()}
        junction = self.root / "sessions"
        self.assertEqual(junction.parent.resolve(), self.root.resolve())
        subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            check=True, capture_output=True,
        )
        try:
            self.assertEqual(junction.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
            # self.sessions was initialized before the directory became a junction.
            with self.assertRaisesRegex(SessionError, "目录不安全"):
                self.sessions.create()
            with self.assertRaisesRegex(SessionError, "目录不安全"):
                self.sessions.load(external_id)
            for args in (("--new-session",), ("--session", external_id)):
                with self.subTest(args=args):
                    process = subprocess.run(
                        [sys.executable, "-B", "-m", "agent_harness.cli", *args],
                        cwd=self.root, capture_output=True, encoding="utf-8", timeout=30,
                    )
                    self.assertEqual(process.returncode, 1, process.stderr)
                    self.assertIn("目录不安全", process.stderr)
                    self.assertEqual(process.stdout, "")
            self.assertEqual({path.name: path.read_bytes() for path in outside.iterdir()}, expected)
        finally:
            self.assertEqual(junction.parent.resolve(), self.root.resolve())
            self.assertEqual(junction.lstat().st_reparse_tag, stat.IO_REPARSE_TAG_MOUNT_POINT)
            os.rmdir(junction)

    def test_rejects_invalid_missing_and_traversal_ids(self) -> None:
        existing = self.sessions.create()
        outside = self.root / "outside.json"
        outside.write_text("outside", encoding="utf-8")
        for session_id in ("", "../outside", r"..\outside", r"C:\outside", existing + ".json"):
            with self.subTest(session_id=session_id):
                with self.assertRaisesRegex(SessionError, "ID 无效"):
                    self.runtime.run("read", session_id)
        missing = "0" * 32
        with self.assertRaisesRegex(SessionError, "不存在"):
            self.sessions.load(missing)
        with self.assertRaisesRegex(SessionError, "不存在"):
            state = self.sessions.load(existing)
            self.sessions.save(missing, state, expected=state)
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")
        self.model.decide.assert_not_called()
        self.assertFalse((self.root / "sessions" / f"{missing}.json").exists())

    def test_rejects_hardlink_alias(self) -> None:
        session_id = self.sessions.create()
        source = self.root / "sessions" / f"{session_id}.json"
        alias_id = "1" * 32
        alias = source.with_name(f"{alias_id}.json")
        try:
            os.link(source, alias)
        except OSError as exc:
            self.skipTest(f"test filesystem does not support hard links: {type(exc).__name__}")
        with self.assertRaisesRegex(SessionError, "不安全"):
            self.sessions.load(alias_id)

    def test_persisted_update_and_delete_are_scoped_to_owner(self) -> None:
        a, b = self.sessions.create(), self.sessions.create()
        self._execute(a, action="create", content="A todo")
        self._execute(b, action="create", content="B todo")
        item_a = self.sessions.load(a)["todos"][0]
        item_b = self.sessions.load(b)["todos"][0]

        self._execute(b, action="delete", todo_id=item_a["id"])
        self.assertEqual(self._tool_result()["error"]["code"], "todo_not_found")
        self._execute(a, action="update", todo_id=item_a["id"], content="updated")
        self.assertEqual(self.sessions.load(a)["todos"], [{**item_a, "content": "updated"}])
        self.assertEqual(self.sessions.load(b)["todos"], [item_b])
        self._execute(a, action="delete", todo_id=item_a["id"])
        self.assertEqual(self.sessions.load(a)["todos"], [])
        self.assertEqual(self.sessions.load(b)["todos"], [item_b])

    def test_failure_before_replace_leaves_old_bytes_and_reports_not_written(self) -> None:
        session_id = self.sessions.create()
        self._execute(session_id, action="create", content="old")
        path = self.root / "sessions" / f"{session_id}.json"
        before = path.read_bytes()
        with patch("agent_harness.session.os.replace", side_effect=OSError("before replace")):
            self._execute(session_id, action="create", content="new")
        self.assertEqual(self._tool_result()["error"]["code"], "todo_save_failed")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual([item["content"] for item in self.sessions.load(session_id)["todos"]], ["old"])
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_failure_after_replace_is_reconciled_from_file(self) -> None:
        session_id = self.sessions.create()
        replace = os.replace

        def replace_then_fail(source, target):
            replace(source, target)
            raise OSError("after replace")

        with patch("agent_harness.session.os.replace", side_effect=replace_then_fail):
            result = self._execute(session_id, action="create", content="persisted")
        self.assertEqual(result["status"], "completed")
        feedback = self._tool_result()["result"]
        self.assertTrue(feedback["recovered"])
        self.assertEqual(self.sessions.load(session_id)["todos"], [feedback["todo"]])

    def test_save_return_without_replacement_is_not_success(self) -> None:
        session_id = self.sessions.create()
        with patch.object(self.sessions, "save", return_value=None):
            self._execute(session_id, action="create", content="not on disk")
        self.assertEqual(self._tool_result()["error"]["code"], "todo_save_failed")
        self.assertEqual(self.sessions.load(session_id)["todos"], [])

    def test_unreadable_authority_after_save_stops_before_retry(self) -> None:
        session_id = self.sessions.create()
        load = self.sessions.load
        self.model.decide.return_value = _todo_call(action="create", content="exists")
        def load_once_unavailable(*args):
            if read.call_count == 4:
                raise SessionError("readback unavailable")
            return load(*args)

        with patch.object(self.sessions, "load", side_effect=load_once_unavailable) as read:
            with self.assertLogs("agent_harness.runtime", level="INFO") as trace:
                result = self.runtime.run("create", session_id)
        self.assertEqual(result["status"], "failed")
        self.assertIn("tool_state_uncertain", " ".join(trace.output))
        self.model.decide.assert_called_once()
        self.assertEqual([item["content"] for item in load(session_id)["todos"]], ["exists"])

    def test_later_model_error_keeps_confirmed_todo_recoverable(self) -> None:
        session_id = self.sessions.create()
        self.model.decide.side_effect = [
            _todo_call(action="create", content="confirmed"),
            ModelError("injected failure"),
        ]
        self.assertEqual(self.runtime.run("create", session_id)["status"], "failed")
        restarted = SessionStore(self.root / "sessions")
        self.assertEqual(
            [item["content"] for item in restarted.load(session_id)["todos"]], ["confirmed"]
        )

    def test_cli_round_trip_through_independent_processes(self) -> None:
        quote = "用户约定：计算结果始终保留两位小数。"
        summary = "A 的历史摘要"
        # 只替换模型入口，使用真实 CLI、Runtime、Tool Manager 和磁盘文件。
        script = f'''
import json
import sys
from unittest.mock import Mock, patch
from agent_harness import cli
arguments = json.loads(sys.argv[-1])
model = Mock()
decisions = iter([
    {{"kind": "tool_calls",
      "tool_calls": [{{"tool_call_id": "cli-call", "name": "todo", "arguments": arguments}}],
      "assistant_message": {{"role": "assistant", "content": None,
         "tool_calls": [{{"id": "cli-call", "type": "function",
            "function": {{"name": "todo", "arguments": json.dumps(arguments)}}}}]}}}},
    {{"kind": "final", "text": "Done"}},
    {{"kind": "final", "text": "[0]"}},
    {{"kind": "final", "text": {summary!r}}},
])
def decide(messages, definitions=()):
    if definitions and messages[-1]["role"] == "tool":
        assert json.loads(messages[-1]["content"])["ok"]
    if definitions and messages[-1]["role"] == "user":
        if arguments["action"] in ("update", "delete"):
            prior = [json.loads(msg["content"]) for msg in messages if msg["role"] == "tool"]
            assert any(result.get("result", {{}}).get("todo", {{}}).get("id") == arguments["todo_id"]
                       for result in prior), "original Todo missing from resumed history"
            assert {quote!r} in messages[0]["content"]
            assert {summary!r} in messages[0]["content"]
        elif arguments["content"] == "买牛奶":
            assert all({quote!r} not in msg.get("content", "") for msg in messages)
    return next(decisions)
model.decide.side_effect = decide
with patch.object(cli, "load_settings"), patch.object(cli, "Model", return_value=model):
    raise SystemExit(cli.main(sys.argv[1:]))
'''

        def child(*args: str) -> dict:
            process = subprocess.run(
                [sys.executable, "-B", *args],
                cwd=self.root,
                capture_output=True,
                encoding="utf-8",
                timeout=30,
            )
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            if "--trace" in args:
                events = [json.loads(line) for line in process.stderr.splitlines()]
                self.assertEqual([event["event"] for event in events], [
                    "request_started", "tool_requested", "tool_started", "tool_result", "request_finished",
                ])
                self.assertEqual(len({event["request_id"] for event in events}), 1)
                self.assertEqual(len({event["tool_call_id"] for event in events[1:4]}), 1)
                self.assertEqual((events[3]["tool_name"], events[3]["status"]), ("todo", "succeeded"))
                self.assertEqual(events[-1]["status"], result["status"])
            else:
                self.assertEqual(process.stderr, "")
            return result

        def restore(session_id: str) -> list[dict]:
            output = child("-m", "agent_harness.cli", "--session", session_id)
            self.assertEqual(set(output), {"session_id", "todos"})
            self.assertEqual(output["session_id"], session_id)
            return output["todos"]

        def act(session_id: str, *, trace: bool = False, **arguments) -> None:
            flags = ("--trace",) if trace else ()
            result = child("-c", script, *flags, "--session", session_id, json.dumps(arguments))
            self.assertEqual(result, {"session_id": session_id, "answer": "Done", "status": "completed"})

        a = child("-m", "agent_harness.cli", "--new-session")["session_id"]
        # 直接准备压缩阈值前的历史，实际压缩由下一个 CLI 进程触发。
        self.sessions.append_messages(a, [
            {"role": "user", "content": text}
            for text in [quote, *(f"较早消息 {i}" for i in range(18))]
        ])
        act(a, trace=True, action="create", content="写论文")
        item_a, = restore(a)
        self.assertEqual(item_a["content"], "写论文")
        state_a = self.sessions.load(a)
        self.assertEqual((state_a["memory"], state_a["summary"]), ([quote], summary))
        self.assertLessEqual(len(state_a["messages"]), 10)

        b = child("-m", "agent_harness.cli", "--new-session")["session_id"]
        self.assertNotEqual(a, b)
        act(b, action="create", content="买牛奶")
        item_b, = restore(b)
        self.assertEqual(item_b["content"], "买牛奶")
        self.assertNotEqual(item_a["id"], item_b["id"])
        state_b = self.sessions.load(b)
        self.assertEqual((state_b["memory"], state_b["summary"]), ([], ""))

        self.assertEqual(restore(a), [item_a])
        self.assertEqual(restore(b), [item_b])
        self.assertEqual((self.sessions.load(a), self.sessions.load(b)), (state_a, state_b))

        act(a, action="update", todo_id=item_a["id"], content="明天写论文")
        self.assertEqual(restore(a), [{**item_a, "content": "明天写论文"}])
        act(a, action="delete", todo_id=item_a["id"])
        self.assertEqual(restore(a), [])
        self.assertEqual(restore(b), [item_b])
        final_a = self.sessions.load(a)
        self.assertEqual((final_a["memory"], final_a["summary"]), ([quote], summary))
        self.assertEqual(self.sessions.load(b), state_b)

    def test_history_followup_and_session_isolation(self) -> None:
        a, b = self.sessions.create(), self.sessions.create()
        self.model.decide.side_effect = [
            {"kind": "final", "text": "22:30"}, _FINAL, _FINAL,
        ]
        self.runtime.run("睡觉时间是 22:30", a)
        self.runtime.run("刚才是什么时间？", a)
        self.runtime.run("另一个会话", b)
        requests = [call.args[0][1:] for call in self.model.decide.call_args_list]
        self.assertEqual(requests[1], [
            {"role": "user", "content": "睡觉时间是 22:30"},
            {"role": "assistant", "content": "22:30"},
            {"role": "user", "content": "刚才是什么时间？"},
        ])
        self.assertEqual(requests[2], [{"role": "user", "content": "另一个会话"}])
        self.assertEqual(len(self.sessions.load(a)["messages"]), 4)
        self.assertEqual(len(self.sessions.load(b)["messages"]), 2)

    def test_todo_followup_and_clarification(self) -> None:
        a = self.sessions.create()
        self._execute(a, action="create", content="写论文")
        item = self.sessions.load(a)["todos"][0]

        def decide(messages, _definitions):
            if messages[-1]["role"] == "tool":
                return _FINAL
            earlier = json.loads(messages[3]["content"])
            self.assertEqual(earlier["result"]["todo"]["id"], item["id"])
            return _todo_call(action="update", todo_id=item["id"], content="明天写论文")

        self.model.decide.side_effect = decide
        self.runtime.run("把刚才那个改到明天", a)
        updated = [{**item, "content": "明天写论文"}]
        self.assertEqual(self.sessions.load(a)["todos"], updated)

        self._execute(a, action="create", content="买牛奶")
        before = self.sessions.load(a)["todos"]
        self.model.decide.side_effect = [{"kind": "final", "text": "请问要改哪一条待办？"}]
        result = self.runtime.run("把其中一条改到后天", a)
        self.assertIn("哪一条", result["answer"])
        self.assertEqual(self.sessions.load(a)["todos"], before)

    def test_multi_tool_and_model_error_save_ordered_group_without_final(self) -> None:
        a = self.sessions.create()
        batch = _todo_call(action="create", content="keep")
        second = _todo_call(action="list")
        second["tool_calls"][0]["tool_call_id"] = "second"
        second["assistant_message"]["tool_calls"][0]["id"] = "second"
        batch["tool_calls"].extend(second["tool_calls"])
        batch["assistant_message"]["tool_calls"].extend(second["assistant_message"]["tool_calls"])
        self.model.decide.side_effect = [batch, ModelError("failed")]
        self.assertEqual(self.runtime.run("create and list", a)["status"], "failed")
        state = self.sessions.load(a)
        self.assertEqual([msg["role"] for msg in state["messages"]], [
            "user", "assistant", "tool", "tool"
        ])
        self.assertEqual([msg["tool_call_id"] for msg in state["messages"][2:]], [
            "todo-call", "second"
        ])
        self.assertEqual(state["todos"][0]["content"], "keep")

    def test_budget_saves_paired_limit_result_and_preserves_todo(self) -> None:
        a = self.sessions.create()
        self.model.decide.side_effect = [
            _todo_call(action="create", content="keep"),
            *[_todo_call(action="list") for _ in range(7)],
        ]
        self.assertEqual(self.runtime.run("repeat", a)["status"], "limit_reached")
        state = self.sessions.load(a)
        self.assertEqual(len(state["messages"]), 17)
        self.assertEqual(state["messages"][-1]["role"], "tool")
        self.assertEqual(json.loads(state["messages"][-1]["content"])["error"]["code"], "limit_reached")
        self.assertEqual(state["todos"][0]["content"], "keep")

    def test_history_append_reconciles_and_retries_only_confirmed_missing(self) -> None:
        for wrote in (False, True):
            with self.subTest(wrote=wrote):
                a = self.sessions.create()
                append = self.sessions.append_messages

                def fault_once(*args, **kwargs):
                    if attempted.call_count == 1:
                        if wrote:
                            append(*args, **kwargs)
                        raise SessionError("injected append error")
                    append(*args, **kwargs)

                self.model.decide.side_effect = [_FINAL]
                with patch.object(self.sessions, "append_messages", side_effect=fault_once) as attempted:
                    self.assertEqual(self.runtime.run("hello", a)["status"], "completed")
                self.assertEqual(attempted.call_count, 1 if wrote else 2)
                self.assertEqual(len(self.sessions.load(a)["messages"]), 2)

    def test_unreadable_history_after_append_failure_stops_without_retry(self) -> None:
        a = self.sessions.create()
        self.model.decide.side_effect = [_FINAL]

        def fault(*args, **kwargs):
            self.sessions.load = Mock(side_effect=SessionError("injected read error"))
            raise SessionError("injected append error")

        original_load = self.sessions.load
        try:
            with patch.object(self.sessions, "append_messages", side_effect=fault) as attempted:
                self.assertEqual(self.runtime.run("hello", a)["status"], "failed")
            attempted.assert_called_once()
        finally:
            self.sessions.load = original_load
        self.assertEqual(self.sessions.load(a)["messages"], [])

    def test_history_append_failure_keeps_confirmed_todo(self) -> None:
        a = self.sessions.create()
        with patch.object(self.sessions, "append_messages", side_effect=SessionError("failed")) as attempted:
            result = self._execute(a, action="create", content="keep")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(attempted.call_count, 2)
        self.assertEqual(self.sessions.load(a)["todos"][0]["content"], "keep")
        self.assertEqual(self.sessions.load(a)["messages"], [])

    def _assert_invalid_tool_history(self, call, tool, error: str) -> None:
        session_id = self.sessions.create()
        history = [{"role": "assistant", "tool_calls": [call]}, tool]
        self.sessions.append_messages(session_id, history)
        self.model.decide.return_value = _FINAL

        result = self.runtime.run("next", session_id)

        self.assertEqual(result["status"], "failed")
        self.assertIn(error, result["answer"])
        self.model.decide.assert_not_called()
        self.assertEqual(self.sessions.load(session_id)["messages"], history)

    def test_history_protocol_requires_string_tool_content(self) -> None:
        call = _todo_call(action="list")["assistant_message"]["tool_calls"][0]
        result = {"role": "tool", "tool_call_id": "todo-call"}
        for tool in (result, {**result, "content": None}, {**result, "content": {}}):
            with self.subTest(tool=tool):
                self._assert_invalid_tool_history(call, tool, "content")

    def test_history_protocol_requires_function_definition(self) -> None:
        tool = {"role": "tool", "tool_call_id": "todo-call", "content": "{}"}
        self._assert_invalid_tool_history({"id": "todo-call", "type": "function"}, tool, "function")

    def test_history_protocol_rejects_malformed_calls(self) -> None:
        call = _todo_call(action="list")["assistant_message"]["tool_calls"][0]
        tool = {"role": "tool", "tool_call_id": "todo-call", "content": "{}"}
        for malformed, field in (
            ({**call, "id": " "}, "ID"),
            ({**call, "type": "custom"}, "type"),
            ({**call, "function": []}, "function"),
            ({**call, "function": {"name": " ", "arguments": "{}"}}, "name"),
            ({**call, "function": {"name": 42, "arguments": "{}"}}, "name"),
            ({**call, "function": {"name": "todo"}}, "arguments"),
            ({**call, "function": {"name": "todo", "arguments": {}}}, "arguments"),
        ):
            with self.subTest(field=field, call=malformed):
                self._assert_invalid_tool_history(malformed, tool, field)

    def test_orphan_history_is_rejected_before_model_call(self) -> None:
        a = self.sessions.create()
        self.sessions.append_messages(a, [{"role": "tool", "tool_call_id": "orphan", "content": "{}"}])
        self.assertEqual(self.runtime.run("hello", a)["status"], "failed")
        self.model.decide.assert_not_called()

    def test_cli_plain_text_still_calls_chat(self) -> None:
        with patch.object(cli, "load_settings"), patch.object(cli, "Model") as factory:
            with patch("builtins.print") as output:
                factory.return_value.chat.return_value = "plain answer"
                self.assertEqual(cli.main(["hello", "world"]), 0)
        factory.return_value.chat.assert_called_once_with("hello world")
        output.assert_called_once_with("plain answer")
