"""Opt-in DeepSeek integration probe using only synthetic CLI data."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from agent_harness.config import ConfigError, load_settings


class ProbeError(Exception):
    pass


def require(condition: bool, label: str) -> None:
    if not condition:
        raise ProbeError(label)


def cli(directory: Path, env: dict[str, str], *args: str) -> tuple[dict, list[dict]]:
    completed = subprocess.run(
        [sys.executable, "-B", "-m", "agent_harness.cli", *args],
        cwd=directory, env=env, capture_output=True, encoding="utf-8", timeout=270,
    )
    try:
        events = [json.loads(line) for line in completed.stderr.splitlines()]
        output = json.loads(completed.stdout) if completed.stdout else {}
    except ValueError:
        raise ProbeError("cli_output_is_not_json") from None

    if completed.returncode:
        allowed = {"config_error", "model_timeout", "model_connection_error", "model_api_error",
                   "model_invalid_response", "model_request_error", "session_error"}
        codes = [event.get("error_code") for event in events]
        reason = next((code for code in codes if code in allowed), "request_failed")
        if output.get("status") == "limit_reached":
            reason = "limit_reached"
        raise ProbeError(f"cli_exit_{completed.returncode}:{reason}")
    return output, events


def answer_json(output: dict) -> dict:
    answer = output.get("answer", "").strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", answer, flags=re.S | re.I)
    try:
        result = json.loads(fence.group(1) if fence else answer)
    except ValueError:
        raise ProbeError("final_answer_is_not_json") from None
    require(isinstance(result, dict), "final_answer_is_not_object")
    return result


def verify_trace(events: list[dict], session_id: str, names: list[str]) -> dict:
    require(bool(events) and events[-1].get("event") == "request_finished", "missing_terminal_trace")
    require(all(event.get("session_id") == session_id for event in events), "trace_session_mismatch")
    require(len({event["request_id"] for event in events}) == 1, "trace_request_mismatch")
    require([event["sequence"] for event in events] == list(range(1, len(events) + 1)),
            "trace_sequence_mismatch")
    tool_results = [event for event in events if event["event"] == "tool_result"]
    require([event["tool_name"] for event in tool_results] == names, "unexpected_tool_order")
    for result in tool_results:
        phases = [event["event"] for event in events if event.get("tool_call_id") == result["tool_call_id"]]
        require(phases == ["tool_requested", "tool_started", "tool_result"], "incomplete_tool_trace")
        require(result["started"] and result["status"] == "succeeded", "tool_did_not_succeed")
    finished = events[-1]
    require(finished["status"] == "completed", "trace_status_mismatch")
    return {
        "decisions": finished["model_calls"],
        "tools": [{"id": event["tool_call_id"], "name": event["tool_name"],
                   "decision": event["decision"], "status": event["status"]}
                  for event in tool_results],
    }


def tool_payloads(messages: list[dict], names: list[str]) -> list[dict]:
    pending = [(call["id"], call["function"]["name"]) for message in messages
               for call in message.get("tool_calls", [])]
    results = [message for message in messages if message["role"] == "tool"]
    require([name for _, name in pending] == names, "unexpected_persisted_tools")
    require([call_id for call_id, _ in pending] == [result["tool_call_id"] for result in results],
            "persisted_call_id_mismatch")
    payloads = [json.loads(result["content"]) for result in results]
    require(all(result["ok"] for result in payloads), "persisted_tool_failure")
    return [result["result"] for result in payloads]


def run() -> None:
    settings = load_settings()
    endpoint = urlsplit(settings.base_url)
    require(endpoint.scheme == "https" and endpoint.hostname == "api.deepseek.com"
            and endpoint.username is None and endpoint.password is None,
            "probe_requires_official_deepseek_https_endpoint")
    env = os.environ.copy()
    env.pop("OPENAI_LOG", None)
    env.update(DEEPSEEK_API_KEY=settings.api_key, DEEPSEEK_BASE_URL=settings.base_url,
               DEEPSEEK_MODEL=settings.model)

    with TemporaryDirectory(prefix="agent-harness-live-") as temp:
        directory = Path(temp)
        opened, _ = cli(directory, env, "--new-session")
        session_id = opened.get("session_id", "")
        require(bool(re.fullmatch(r"[0-9a-f]{32}", session_id)), "invalid_generated_session_id")
        path = directory / "sessions" / f"{session_id}.json"
        first, first_trace = cli(
            directory, env, "--trace", "--session", session_id,
            "先用 calculator 计算 (17+23)*3。收到工具结果后用 todo create 创建一条待办，"
            "内容为 T11- 拼接该整数。最后只返回 JSON 对象，键为 value（整数）和 todo_id（新待办 ID）。",
        )
        first_evidence = verify_trace(first_trace, session_id, ["calculator", "todo"])
        require(first_evidence["tools"][0]["decision"] < first_evidence["tools"][1]["decision"],
                "calculator_result_did_not_precede_todo_decision")
        initial = json.loads(path.read_text(encoding="utf-8"))
        require(len(initial["todos"]) == 1, "expected_one_persisted_todo")
        todo = initial["todos"][0]
        require(todo["content"] == "T11-120" and re.fullmatch(r"[0-9a-f]{32}", todo["id"]),
                "unexpected_persisted_todo")
        calculator, created = tool_payloads(initial["messages"], ["calculator", "todo"])
        require(calculator.get("value") == 120 and created.get("todo") == todo, "tool_disk_mismatch")
        require(first.get("status") == "completed" and first.get("session_id") == session_id
                and answer_json(first) == {"value": 120, "todo_id": todo["id"]}
                and initial["messages"][-1]["content"] == first["answer"], "first_answer_mismatch")

        followup, followup_trace = cli(
            directory, env, "--trace", "--session", session_id,
            "刚才创建的是哪条待办？调用 todo list 查询本会话后只返回 JSON 对象，"
            "键为 content 和 todo_id。",
        )
        followup_evidence = verify_trace(followup_trace, session_id, ["todo"])
        final = json.loads(path.read_text(encoding="utf-8"))
        start = len(initial["messages"])
        listed, = tool_payloads(final["messages"][start:], ["todo"])
        require(final["todos"] == initial["todos"] and listed.get("todos") == [todo]
                and final["messages"][:start] == initial["messages"], "resumed_state_mismatch")
        require(followup.get("status") == "completed" and followup.get("session_id") == session_id
                and answer_json(followup) == {"content": "T11-120", "todo_id": todo["id"]}
                and final["messages"][-1]["content"] == followup["answer"], "followup_answer_mismatch")
        print(json.dumps({
            "status": "passed", "model": settings.model, "calculator": 120,
            "todo": {"content": "T11-120", "id_consistent": True},
            "first": first_evidence, "followup": followup_evidence,
            "persisted_messages": [start, len(final["messages"])],
        }))


if __name__ == "__main__":
    try:
        run()
    except ProbeError as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        raise SystemExit(1) from None
    except ConfigError:
        print(json.dumps({"status": "failed", "reason": "missing_configuration"}))
        raise SystemExit(1) from None
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        print(json.dumps({"status": "failed", "reason": "probe_environment_or_protocol_error"}))
        raise SystemExit(1) from None
