"""Requirements-based checks using real LangChain clients and a local HTTP transport."""
from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

import httpx
from langchain_core.utils.json import parse_json_markdown
import pytest

from user_scripts import check_llm_capabilities as probe


CASE_IDS = ("case1", "case1a", "case2.1", "case2.2", "case2.3", "case3", "case3a",
            "case4.1", "case4.2", "case4.3", "case5")
PROTOCOLS = ("chat", "responses", "messages")
JSON_REPLY = '{"description":"收到数字","num":[12,7,25],"sum":44}'
VISION_REPLY = "收到一些数字：12、7、25，求和等于 44。"
API_KEY = "private-probe-token"
ToolArguments = dict[str, object] | str


@dataclass
class FakeProviderGateway:
    """Queue protocol-valid HTTP replies without replacing model/framework behavior."""

    base_url: str
    requests: list[httpx.Request] = field(default_factory=list)
    pending_replies: deque[httpx.Response | httpx.TransportError] = field(default_factory=deque)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert self.pending_replies, "The case sent an unplanned request or retried"
        reply = self.pending_replies.popleft()
        if isinstance(reply, httpx.TransportError):
            raise reply
        return reply

    def queue_model_reply(
        self, protocol: str, text: str = JSON_REPLY, tool_calls: Sequence[tuple[str, ToolArguments]] = (),
    ) -> None:
        if protocol == "chat":
            assistant_message: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_message["tool_calls"] = [
                    {"id": f"call-{index}", "type": "function", "function": {
                        "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                    }} for index, (name, arguments) in enumerate(tool_calls)
                ]
            response_payload = {"id": "completion-1", "object": "chat.completion", "created": 0,
                                "model": "probe-model", "choices": [{"index": 0, "message": assistant_message,
                                "finish_reason": "tool_calls" if tool_calls else "stop"}]}
        elif protocol == "messages":
            content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
            content.extend({"type": "tool_use", "id": f"call-{index}", "name": name, "input": arguments}
                           for index, (name, arguments) in enumerate(tool_calls))
            response_payload = {"id": "message-1", "type": "message", "role": "assistant", "model": "probe-model",
                                "content": content, "stop_reason": "tool_use" if tool_calls else "end_turn",
                                "usage": {"input_tokens": 20, "output_tokens": 10}}
        else:
            output: list[dict[str, Any]] = []
            if text:
                output.append({"id": "message-1", "type": "message", "role": "assistant", "status": "completed",
                               "content": [{"type": "output_text", "text": text, "annotations": []}]})
            output.extend({"type": "function_call", "id": f"function-{index}", "call_id": f"call-{index}",
                           "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
                           "status": "completed"} for index, (name, arguments) in enumerate(tool_calls))
            response_payload = {"id": "response-1", "object": "response", "created_at": 0,
                                "status": "completed", "model": "probe-model", "output": output}
        self.pending_replies.append(httpx.Response(200, json=response_payload))


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeProviderGateway]:
    gateway = FakeProviderGateway(f"https://provider.invalid/{uuid4().hex}")
    transport = httpx.MockTransport(gateway.handle_request)
    original_init = httpx.AsyncClient.__init__

    def initialize_mock_transport(client: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        # Anthropic prebuilds proxy mounts from the machine's environment/registry.
        # Replace every HTTP route, while retaining the real client's lifecycle.
        original_init(client, *args, **{**kwargs, "transport": transport, "mounts": {},
                                        "proxy": None, "trust_env": False})

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize_mock_transport)
    monkeypatch.setattr(probe, "__file__", str(tmp_path / "check_llm_capabilities.py"))
    yield gateway
    assert not gateway.pending_replies, "A required request was not made"


async def run_cases(
    gateway: FakeProviderGateway, *case_ids: str, protocols: Sequence[str] = ("chat",),
) -> list[probe.CaseRecord]:
    arguments = argparse.Namespace(
        base_url=gateway.base_url, model="vendor/model:trial",
        protocol=list(protocols), case_ids=list(case_ids), timeout=5,
    )
    return await probe.run_probes(arguments, API_KEY)


def read_request_messages(request: httpx.Request, protocol: str) -> list[dict[str, Any]]:
    payload = json.loads(request.content)
    return payload["input"] if protocol == "responses" else payload["messages"]


def extract_message_text(message: dict[str, Any]) -> str:
    content = message["content"]
    if isinstance(content, str):
        return content
    return "\n".join(block["text"] for block in content if block["type"] in {"text", "input_text", "output_text"})


def test_listing_exposes_spec_cases_without_prompting(capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main(["--list"]) == 0
    output = capsys.readouterr().out
    assert all(case_id in output for case_id in CASE_IDS)
    assert all(protocol in output for protocol in PROTOCOLS)


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("case_id", CASE_IDS)
async def test_case_contracts(protocol: str, case_id: str, gateway: FakeProviderGateway, tmp_path: Path,
                              capsys: pytest.CaptureFixture[str]) -> None:
    tool_calls: list[tuple[str, ToolArguments]] = []
    if case_id == "case3a":
        tool_calls = [("A", {"numbers": [12, 7, 25]}), ("B", {"numbers": [12, 7, 25]})]
    elif case_id in {"case3", "case4.1", "case4.2", "case4.3"}:
        tool_calls = [("B", {"numbers": [12, 7, 25]})]
    unsupported_json_mode = protocol == "messages" and case_id in {"case2.2", "case4.2"}
    if not unsupported_json_mode:
        if case_id == "case1a":
            gateway.queue_model_reply(protocol, VISION_REPLY)
        response_text = "" if case_id in {"case3", "case3a"} else JSON_REPLY
        gateway.queue_model_reply(protocol, response_text, tool_calls)
    record, = await run_cases(gateway, case_id, protocols=[protocol])
    assert record.case_id == case_id

    report_path, = tmp_path.rglob("*.json")
    report_text = report_path.read_text(encoding="utf-8")
    report_document = json.loads(report_text)
    assert report_document["status"] == record.status and report_document["case_id"] == case_id
    assert report_path.name == f"{case_id}.json" and report_path.parent.name == protocol
    assert re.fullmatch(r"vendor_model_trial_llm_check_\d{8}T\d{12}", report_path.parent.parent.name)
    assert report_path.parent.parent.parent == tmp_path
    case_description = next(case.description for case in probe.CASES if case.case_id == case_id)
    assert report_document["case_description"] == case_description
    assert API_KEY not in report_text
    terminal_output = capsys.readouterr().out
    assert case_description in terminal_output.splitlines()[0]
    assert "总览" in terminal_output and record.status in terminal_output
    assert JSON_REPLY not in terminal_output and VISION_REPLY not in terminal_output
    assert probe.IMAGE_BASE64 not in terminal_output and API_KEY not in terminal_output

    if unsupported_json_mode:
        assert record.status == "ERROR"
        assert record.error is not None
        assert record.error.type == "UserWarning"
        assert record.error.category == "client"
        assert not gateway.requests
        assert "client/UserWarning" in terminal_output
        return

    assert record.status == ("MANUAL" if case_id == "case5" else "PASS")
    assert all(not results for call in record.calls for results in call.checks.values())
    assert report_document["calls"][-1]["response"]["type"] == "ai"
    assert len(gateway.requests) == (2 if case_id == "case1a" else 1)
    request = gateway.requests[-1]
    request_payload = json.loads(request.content)
    message_history = read_request_messages(request, protocol)
    user_messages = [message for message in message_history if message["role"] == "user"]
    assert user_messages and all(extract_message_text(message).strip() for message in user_messages)
    assert request.url.path.endswith({"chat": "/v1/chat/completions", "responses": "/v1/responses", "messages": "/v1/messages"}[protocol])
    assert "parallel_tool_calls" not in request_payload
    assert "tool_choice" not in request_payload
    assert len(request_payload.get("tools", [])) == (2 if tool_calls else 0)
    if protocol == "responses":
        assert request_payload["store"] is False and "previous_response_id" not in request_payload
    if case_id in {"case2.1", "case2.2", "case4.1", "case4.2"}:
        if protocol == "messages":
            format_instructions = extract_message_text({"content": request_payload["system"]})
        else:
            assert message_history[0]["role"] == "system"
            format_instructions = extract_message_text(message_history[0])
        requested_schema = parse_json_markdown(format_instructions)
        recognition_schema = probe.NumberRecognition.model_json_schema()
        assert requested_schema["properties"] == recognition_schema["properties"]
        assert requested_schema["required"] == recognition_schema["required"]
    if case_id in {"case2.2", "case4.2", "case2.3", "case4.3"}:
        if protocol == "messages":
            output_format = request_payload["output_config"]["format"]
        elif protocol == "responses":
            output_format = request_payload["text"]["format"]
        else:
            output_format = request_payload["response_format"]
        assert output_format["type"] == ("json_object" if case_id.endswith(".2") else "json_schema")
        if protocol == "messages":
            assert output_format["schema"]["additionalProperties"] is False
    if case_id == "case5":
        assert [message["role"] for message in message_history] == ["user", "assistant"]
        prefilled_text = extract_message_text(message_history[-1])
        prefilled_json, json_end = json.JSONDecoder().raw_decode(prefilled_text)
        probe.NumberRecognition.model_validate(prefilled_json)
        assert prefilled_text[json_end:].strip()
        assert "response_format" not in request_payload
    if case_id in {"case1", "case1a"}:
        image_request_messages = read_request_messages(gateway.requests[0], protocol)
        image_block_types = {"image", "image_url", "input_image"}
        encoded_image = "".join(probe.IMAGE_BASE64.split())
        assert any(block["type"] in image_block_types for block in image_request_messages[0]["content"])
        assert encoded_image in gateway.requests[0].content.decode()
        assert encoded_image in report_text
        if case_id == "case1a":
            assert [message["role"] for message in message_history] == ["user", "assistant", "user"]
            assert all(block["type"] not in image_block_types for block in message_history[0]["content"])
            assert encoded_image not in request.content.decode()
            recognition_response = record.calls[0].response
            assert recognition_response is not None
            assert extract_message_text(message_history[1]) == recognition_response.text
    if tool_calls:
        assert len(record.calls[-1].tool_results) == len(tool_calls)
        assert all(message.status == "success" for message in record.calls[-1].tool_results)
        assert [float(message.text) for message in record.calls[-1].tool_results] == ([45, 43] if case_id == "case3a" else [43])
        serialized_tool_results = report_document["calls"][-1]["tool_results"]
        assert [result["type"] for result in serialized_tool_results] == ["tool"] * len(tool_calls)


async def test_reports_share_one_run_directory_and_group_by_protocol(gateway: FakeProviderGateway, tmp_path: Path) -> None:
    for protocol in PROTOCOLS:
        gateway.queue_model_reply(protocol)
        gateway.queue_model_reply(protocol)
    await run_cases(gateway, "case2.1", "case5", protocols=PROTOCOLS)
    directories = list(tmp_path.glob("vendor_model_trial_llm_check_*"))
    assert len(directories) == 1 and directories[0].is_dir()
    assert {path.relative_to(directories[0]).as_posix() for path in directories[0].rglob("*.json")} == {
        f"{protocol}/{case_id}.json" for protocol in PROTOCOLS for case_id in ("case2.1", "case5")
    }
    original_reports = {path: path.read_text(encoding="utf-8") for path in directories[0].rglob("*.json")}
    gateway.queue_model_reply("chat")
    await run_cases(gateway, "case5")
    assert len(list(tmp_path.glob("vendor_model_trial_llm_check_*"))) == 2
    assert all(path.read_text(encoding="utf-8") == contents for path, contents in original_reports.items())
    assert not list(tmp_path.glob("*.json"))


@pytest.mark.parametrize("protocol", PROTOCOLS)
async def test_image_history_is_reused_without_mutating_original(protocol: str, gateway: FakeProviderGateway) -> None:
    gateway.queue_model_reply(protocol, VISION_REPLY)
    gateway.queue_model_reply(protocol, "总数是 3 个。")
    records = await run_cases(gateway, "case1a", "case1", "case1", protocols=[protocol])
    assert [record.case_id for record in records] == ["case1", "case1a"]
    assert [record.status for record in records] == ["PASS", "PASS"]
    assert len(gateway.requests) == 2
    assert [len(record.calls) for record in records] == [1, 1]
    recognition_call = records[0].calls[0]
    followup_call = records[1].calls[0]
    original_content = recognition_call.messages[0].content
    followup_content = followup_call.messages[0].content
    assert isinstance(original_content, list) and isinstance(followup_content, list)
    image_block = original_content[0]
    placeholder_block = followup_content[0]
    assert isinstance(image_block, dict) and image_block["type"] == "image"
    assert image_block["base64"] == "".join(probe.IMAGE_BASE64.split())
    assert isinstance(placeholder_block, dict) and placeholder_block["type"] == "text"
    assert placeholder_block["text"].strip()
    assert followup_content[1:] == original_content[1:]
    assert [message.type for message in followup_call.messages] == ["human", "ai", "human"]
    assert followup_call.messages[1] == recognition_call.response


@pytest.mark.parametrize("failed_case,dependents", [("case1", ["case1a"]),
                                                   ("case3", ["case3a", "case4.1", "case4.2", "case4.3"])])
async def test_failed_record_skips_only_selected_dependents(failed_case: str, dependents: list[str],
                                                          gateway: FakeProviderGateway, tmp_path: Path) -> None:
    gateway.pending_replies.append(httpx.Response(400, json={"error": {"message": "provider rejection"}}))
    gateway.queue_model_reply("chat")
    records = await run_cases(gateway, failed_case, *dependents, "case5")
    assert [record.status for record in records] == ["ERROR", *("NA" for _ in dependents), "MANUAL"]
    assert len(gateway.requests) == 2
    saved = list(tmp_path.rglob("*.json"))
    assert len(saved) == len(records)
    for record in records:
        document = json.loads(next(path for path in saved if path.stem == record.case_id).read_text(encoding="utf-8"))
        assert document["status"] == record.status
        assert document["case_description"] == next(case.description for case in probe.CASES if case.case_id == record.case_id)


async def test_records_do_not_gate_other_protocols(gateway: FakeProviderGateway) -> None:
    gateway.pending_replies.append(httpx.Response(400, json={"error": {"message": "no vision"}}))
    gateway.queue_model_reply("responses", VISION_REPLY)
    gateway.queue_model_reply("responses", "总数是 3 个。")
    records = await run_cases(gateway, "case1", "case1a", protocols=["responses", "chat", "chat"])
    assert [(record.protocol, record.status) for record in records] == [
        ("chat", "ERROR"), ("chat", "NA"), ("responses", "PASS"), ("responses", "PASS")]


@pytest.mark.parametrize("case_id,text,tool_calls,json_passed,tools_passed", [
    ("case4.1", '{"description":"incomplete"}', [("B", {"numbers": [12, 7, 25]})], False, True),
    ("case4.2", JSON_REPLY, [], True, False),
    ("case4.3", "", [("B", {"numbers": [12, 7, 25]})], False, True),
    ("case4.1", JSON_REPLY, [("B", {"numbers": []})], True, False),
    ("case4.1", JSON_REPLY, [("B", '{"numbers":')], True, False),
    ("case4.1", JSON_REPLY, [("missing", {"numbers": [1]})], True, False),
    ("case4.1", '{"description":"incomplete"}', [], False, False),
])
async def test_json_and_tool_checks_are_independent(case_id: str, text: str,
                                                  tool_calls: list[tuple[str, ToolArguments]], json_passed: bool,
                                                  tools_passed: bool, gateway: FakeProviderGateway,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    gateway.queue_model_reply("chat", text, tool_calls)
    record = (await run_cases(gateway, case_id))[0]
    assert record.status == "FAIL"
    assert not record.error
    assert (not record.calls[0].checks["json"]) is json_passed
    assert (not record.calls[0].checks["tools"]) is tools_passed
    assert record.calls[0].response is not None and record.calls[0].response.content == text
    assert len(gateway.requests) == 1
    terminal_output = capsys.readouterr().out
    for check_name, failures in record.calls[0].checks.items():
        for failure in failures:
            assert f"{check_name}: {failure.category}/{failure.type}" in terminal_output


async def test_structure_checks_do_not_grade_arithmetic_or_restrict_call_count(gateway: FakeProviderGateway) -> None:
    gateway.queue_model_reply("chat", '{"description":"valid structure","num":[999],"sum":-1}',
                    [("B", {"numbers": [1]}), ("B", {"numbers": [2]})])
    assert (await run_cases(gateway, "case4.2"))[0].status == "PASS"


@pytest.mark.parametrize("protocol,case_id", [
    ("chat", "case2.1"), ("responses", "case2.1"), ("messages", "case2.1"),
    ("chat", "case2.2"), ("responses", "case2.2"), ("messages", "case2.3"),
    ("chat", "case4.1"), ("responses", "case4.1"), ("messages", "case4.1"),
    ("chat", "case4.2"), ("responses", "case4.2"), ("messages", "case4.3"),
])
@pytest.mark.parametrize("text", [
    JSON_REPLY + "\n以上是识别结果。",
    f"```json\n{JSON_REPLY}\n```",
    f"识别结果如下：\n```json\n{JSON_REPLY}\n```\n以上是收到的数字。",
], ids=["suffix", "fenced", "commented_fence"])
async def test_json_extra_text_is_warning(protocol: str, case_id: str, text: str, gateway: FakeProviderGateway,
                                         tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tool_calls: list[tuple[str, ToolArguments]] = []
    if case_id.startswith("case4"):
        tool_calls = [("B", {"numbers": [12, 7, 25]})]
    gateway.queue_model_reply(protocol, text, tool_calls)
    record = (await run_cases(gateway, case_id, protocols=[protocol]))[0]
    assert record.status == "WARNING"
    call_record = record.calls[0]
    warning, = call_record.checks["json"]
    assert warning.severity == "warning" and warning.category == "validation"
    assert warning.type == "JSONDecodeError" and warning.message and warning.http_status is None
    assert record.error is None and call_record.response is not None and call_record.response.text == text
    if tool_calls:
        assert not call_record.checks["tools"] and call_record.tool_results[0].status == "success"
    assert len(gateway.requests) == 1
    document = json.loads(next(tmp_path.rglob("*.json")).read_text(encoding="utf-8"))
    assert document["status"] == "WARNING"
    assert document["calls"][0]["checks"]["json"] == [warning.model_dump()]
    terminal_output = capsys.readouterr().out
    assert "json: WARNING validation/JSONDecodeError" in terminal_output
    assert text not in terminal_output


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("text", [
    " \n" + JSON_REPLY + "\t\r\n ",
    json.dumps({"description": "收到数字；说明内包含 ``` 标记及换行\n", "num": [12, 7, 25], "sum": 44}),
], ids=["whitespace", "text_inside_field"])
async def test_pure_json_allows_whitespace_and_text_inside_fields(protocol: str, text: str,
                                                                 gateway: FakeProviderGateway) -> None:
    gateway.queue_model_reply(protocol, text)
    record = (await run_cases(gateway, "case2.1", protocols=[protocol]))[0]
    assert record.status == "PASS" and not record.calls[0].checks["json"]


@pytest.mark.parametrize("tool_calls", [[], [("B", {"numbers": []})]])
async def test_json_warning_does_not_mask_tool_failure(tool_calls: list[tuple[str, ToolArguments]],
                                                     gateway: FakeProviderGateway,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    gateway.queue_model_reply("chat", JSON_REPLY + "\n附加说明。", tool_calls)
    record = (await run_cases(gateway, "case4.1"))[0]
    assert record.status == "FAIL" and record.calls[0].checks["json"][0].severity == "warning"
    assert record.calls[0].checks["tools"]
    assert all(failure.severity == "error" for failure in record.calls[0].checks["tools"])
    terminal_output = capsys.readouterr().out
    assert "FAIL" in terminal_output and "WARNING" in terminal_output


async def test_json_purity_does_not_grade_manual_ai_tail(gateway: FakeProviderGateway) -> None:
    gateway.queue_model_reply("chat", JSON_REPLY + "\n总数是 3。")
    record = (await run_cases(gateway, "case5"))[0]
    assert record.status == "MANUAL"
    assert not record.calls[0].checks and len(gateway.requests) == 1


@pytest.mark.parametrize("fixture_name,fixture_value,case_ids", [
    ("IMAGE_BASE64", "", ["case1", "case2.1"]),
    ("IMAGE_BASE64", "not base64!", ["case1a", "case2.1"]),
    ("NUMBERS", [], ["case1", "case3"]),
    ("NUMBERS", [float("nan")], ["case1", "case2.2"]),
    ("NUMBERS", ["12"], ["case1", "case5"]),
])
async def test_invalid_fixtures_do_not_send_requests_or_block_unrelated_cases(
    fixture_name: str, fixture_value: object, case_ids: list[str], gateway: FakeProviderGateway,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(probe, fixture_name, fixture_value)
    gateway.queue_model_reply("chat")
    records = await run_cases(gateway, *case_ids)
    failed = records[0] if fixture_name == "IMAGE_BASE64" else records[1]
    succeeded = records[1] if fixture_name == "IMAGE_BASE64" else records[0]
    assert failed.status == "ERROR" and failed.error is not None and failed.error.category == "configuration"
    assert succeeded.status == "PASS" and len(gateway.requests) == 1
    assert f"configuration/{failed.error.type}" in capsys.readouterr().out


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_http_failures_preserve_errors_without_retrying(status: int, gateway: FakeProviderGateway,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    gateway.pending_replies.append(httpx.Response(status, json={"error": {"message": "original provider detail"}}))
    gateway.queue_model_reply("chat")
    records = await run_cases(gateway, "case2.2", "case5")
    assert records[0].status == "ERROR" and records[0].error is not None and records[0].error.category == "http"
    assert records[0].error.severity == "error"
    assert records[0].error.http_status == status
    assert "original provider detail" in records[0].error.message
    assert records[1].status == "MANUAL" and len(gateway.requests) == 2
    terminal_output = capsys.readouterr().out
    assert f"http/{records[0].error.type} (HTTP {status})" in terminal_output


@pytest.mark.parametrize("exception,category", [(httpx.ConnectError("cannot connect"), "connection"),
                                                (httpx.ReadTimeout("read timed out"), "timeout")])
async def test_transport_errors_are_not_http_rejections(exception: httpx.TransportError, category: str,
                                                       gateway: FakeProviderGateway,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    gateway.pending_replies.append(exception)
    record = (await run_cases(gateway, "case3"))[0]
    assert record.status == "ERROR" and record.error is not None and record.error.category == category
    assert record.error.http_status is None and len(gateway.requests) == 1
    assert f"{category}/{record.error.type}" in capsys.readouterr().out


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize("text", ['{"description":"missing numbers"}',
                                 '说明：\n```json\n{"description":"missing numbers"}\n```\n附加文字。'])
async def test_parser_failures_preserve_reply_and_are_not_http_errors(protocol: str, text: str,
                                                                     gateway: FakeProviderGateway) -> None:
    gateway.queue_model_reply(protocol, text)
    record = (await run_cases(gateway, "case2.1", protocols=[protocol]))[0]
    assert record.status == "FAIL"
    assert len(record.calls[0].checks["json"]) == 1
    failure = record.calls[0].checks["json"][0]
    assert failure.category == "validation" and failure.severity == "error"
    assert record.calls[0].response is not None and record.calls[0].response.text == text


def test_cli_accepts_lists_and_prompts_only_for_missing_fields(gateway: FakeProviderGateway,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    gateway.queue_model_reply("chat", tool_calls=[("B", {"numbers": [12, 7, 25]})])
    prompts: list[str] = []

    def ask_model(prompt: str) -> str:
        prompts.append(prompt)
        return "probe-model"

    def ask_secret(prompt: str) -> str:
        prompts.append(prompt)
        return API_KEY

    monkeypatch.setattr("builtins.input", ask_model)
    monkeypatch.setattr(probe.getpass, "getpass", ask_secret)
    assert probe.main([
        "--base-url", gateway.base_url,
        "--protocol", "chat", "chat", "--case", "case3", "case3",
    ]) == 0
    assert len(prompts) == 2 and len(gateway.requests) == 1


def test_explicit_credentials_need_no_prompt(gateway: FakeProviderGateway) -> None:
    gateway.queue_model_reply("chat")
    assert probe.main([
        "--base-url", gateway.base_url, "--model", "probe-model",
        "--api-key", API_KEY, "--protocol", "chat", "--case", "case5",
    ]) == 0


def test_report_write_failure_stops_later_requests(gateway: FakeProviderGateway, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    gateway.queue_model_reply("chat")
    blocked_directory = tmp_path / "not-a-directory"
    blocked_directory.write_text("file", encoding="utf-8")
    monkeypatch.setattr(probe, "__file__", str(blocked_directory / "probe.py"))
    assert probe.main([
        "--base-url", gateway.base_url, "--model", "probe-model",
        "--api-key", API_KEY, "--protocol", "chat", "--case", "case2.1", "case5",
    ]) == 1
    assert len(gateway.requests) == 1
    assert "Error" in capsys.readouterr().out


@pytest.mark.parametrize("base_url", ["https://[invalid", "https://provider.invalid/v1", "not-a-url"])
def test_invalid_base_url_is_a_parameter_error(base_url: str) -> None:
    with pytest.raises(SystemExit) as failure:
        probe.main(["--base-url", base_url, "--model", "probe-model", "--api-key", API_KEY])
    assert failure.value.code == 2
