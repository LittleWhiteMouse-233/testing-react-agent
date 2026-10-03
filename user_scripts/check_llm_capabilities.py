"""独立检测 LLM 协议、消息、工具和结构化输出；逐 case 保存原始记录。"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
import getpass
import json
import math
from pathlib import Path
import re
from time import perf_counter
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit
import warnings

from anthropic import APIConnectionError as AnthropicConnectionError
from anthropic import APIResponseValidationError as AnthropicResponseValidationError
from anthropic import APITimeoutError as AnthropicTimeoutError
from anthropic import transform_schema
from langchain_anthropic import ChatAnthropic
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import (
    AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage,
    message_to_dict, messages_to_dict,
)
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.runnables import Runnable
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langsmith import tracing_context
from openai import APIConnectionError as OpenAIConnectionError
from openai import APIResponseValidationError as OpenAIResponseValidationError
from openai import APITimeoutError as OpenAITimeoutError
from pydantic import (
    BaseModel, Field, FiniteFloat, SecretStr, TypeAdapter, ValidationError,
    computed_field, field_serializer,
)

# Editable fixtures. The PNG displays "12  7  25"; use raw Base64, not a data: URL.
NUMBERS = [12, 7, 25]
IMAGE_MIME_TYPE = "image/png"
IMAGE_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAhYAAABvCAAAAACVntYvAAABU0lEQVR42u3cMRKAIBAEQf7/aY2MiLAOWaQn"
    "puTEDtV2SV3NEQgLYSEshIWwEBbCQlgIC2EhLISFsJCwEBbCQlgIC2EhLISFsNCvWbSnocV9i294pNhRZ98X"
    "FlhggQUWWGCBBRZYYIEFFlhggcW+LIYuWrUm5HnE7t6vqZ0ZCyywwAILLLDAAgsssMACCyywwGJfFuUbx7LI"
    "94oFFlhggQUWWGCBBRZYYIEFFlhggcVnp0bD1FFr30HEAgsssMACCyywwAILLLDAAgsssDiWBQ05o67/NBkLLL"
    "DAAgsssMACCyywwAILLLDAIpWFD00DJ8TChFhggQUWWGCBBRZYYIEFFlhgcQKLhf+pP5PFuzP8+qU9LLDAAgss"
    "sMACCyywwAILLLDAAotUFjowLISFsBAWwkJYCAthISyEhbAQFsJCWEhYCAthISyEhbAQFsJCWGjDbj1J4ZgHd"
    "J5GAAAAAElFTkSuQmCC"
)
RECOGNITION_REQUEST = (
    "以 json 结构化文本输出：简短说明收到的内容、（如果收到了数字）收到的数字列表、（如果收到了数字）数字求和。"
    "如果没有提供明确的 schema，则以你认为合理的 json 结构输出。"
    "如果本轮需要调用工具，也必须同时输出以上要求的文本内容，不能无说明直接发起 Tool Call。"
)
PROCESSING_REQUEST = "请对这些数字执行 Tool B 的计算。"
IMAGE_PLACEHOLDER = "<数字图片.png 的占位符>"

ProtocolName = Literal["chat", "responses", "messages"]
OutputMode = Literal["plain", "prompt_json", "json_mode", "json_schema"]
CaseStatus = Literal["PASS", "WARNING", "FAIL", "ERROR", "NA", "MANUAL"]
ProbeDiagnosticCategory = Literal["configuration", "validation", "http", "connection", "timeout", "client", "tool"]
ProbeDiagnosticSeverity = Literal["error", "warning"]
CheckName = Literal["text", "json", "tools"]
PROTOCOLS: tuple[ProtocolName, ...] = ("chat", "responses", "messages")
NumberSequence = Annotated[list[FiniteFloat], Field(min_length=1)]
NUMBER_SEQUENCE_ADAPTER = TypeAdapter(NumberSequence)


@tool("A")
def process_a(numbers: NumberSequence) -> float:
    """对输入的数字序列执行 A 处理并返回结果。"""
    return math.fsum(numbers) + 1


@tool("B")
def process_b(numbers: NumberSequence) -> float:
    """对输入的数字序列执行 B 处理并返回结果。"""
    return math.fsum(numbers) - 1


TOOLS = [process_a, process_b]


class NumberRecognition(BaseModel):
    """Shared output contract; arithmetic and recognition accuracy are not graded."""

    description: str = Field(description="收到的内容的简短描述")
    num: list[float] = Field(description="按顺序识别出的数字")
    sum: float = Field(description="识别出的数字之和")


RECOGNITION_PARSER = PydanticOutputParser(pydantic_object=NumberRecognition)


class ProbeDiagnostic(BaseModel):
    """Exception and validation facts, including non-fatal format warnings."""

    category: ProbeDiagnosticCategory
    severity: ProbeDiagnosticSeverity = "error"
    type: str
    message: str
    http_status: int | None = None


class CaseCallRecord(BaseModel):
    """One invocation's inputs, raw response and independent validation results."""

    messages: list[BaseMessage]
    output_mode: OutputMode
    expected_tools: tuple[str, ...]
    response: AIMessage | None = None
    tool_results: list[ToolMessage] = Field(default_factory=list)
    # An empty diagnostic list means the check passed; absent means not checked.
    checks: dict[CheckName, list[ProbeDiagnostic]] = Field(default_factory=dict)
    elapsed_seconds: float = 0

    @field_serializer("messages", "tool_results")
    def serialize_messages(self, messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
        return messages_to_dict(messages)

    @field_serializer("response")
    def serialize_response(self, response: AIMessage | None) -> dict[str, Any] | None:
        return message_to_dict(response) if response is not None else None


class CaseRecord(BaseModel):
    """Canonical run record; summary status is derived, never separately updated."""

    protocol: ProtocolName
    case_id: str
    case_description: str  # Historical snapshot of the registry's description.
    model_name: str
    base_url: str
    started_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    calls: list[CaseCallRecord] = Field(default_factory=list)
    error: ProbeDiagnostic | None = None
    skipped_because: str | None = None
    manual_review: bool = False

    @computed_field
    @property
    def status(self) -> CaseStatus:
        if self.skipped_because is not None:
            return "NA"
        if self.error is not None:
            return "FAIL" if self.error.category == "validation" else "ERROR"
        check_severities = {
            diagnostic.severity
            for call_record in self.calls
            for diagnostics in call_record.checks.values()
            for diagnostic in diagnostics
        }
        if "error" in check_severities:
            return "FAIL"
        if "warning" in check_severities:
            return "WARNING"
        return "MANUAL" if self.manual_review else "PASS"


def describe_exception(
    exception: Exception,
    *,
    category: ProbeDiagnosticCategory | None = None,
    severity: ProbeDiagnosticSeverity = "error",
) -> ProbeDiagnostic:
    http_status = getattr(exception, "status_code", None)
    if category is None:
        if isinstance(exception, (
            OutputParserException, ValidationError,
            OpenAIResponseValidationError, AnthropicResponseValidationError,
        )):
            category = "validation"
        elif isinstance(exception, (TimeoutError, OpenAITimeoutError, AnthropicTimeoutError)):
            category = "timeout"
        elif isinstance(exception, (OpenAIConnectionError, AnthropicConnectionError)):
            category = "connection"
        elif isinstance(http_status, int) and http_status >= 400:
            category = "http"
        else:
            category = "client"
    return ProbeDiagnostic(
        category=category, severity=severity, type=type(exception).__name__, message=str(exception),
        http_status=http_status if isinstance(http_status, int) else None,
    )


def read_number_fixture() -> list[float]:
    return NUMBER_SEQUENCE_ADAPTER.validate_python(NUMBERS, strict=True)


def build_image_messages() -> list[BaseMessage]:
    encoded_image = "".join(IMAGE_BASE64.split())
    if IMAGE_MIME_TYPE not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
        raise ValueError("IMAGE_MIME_TYPE 必须为 PNG/JPEG/WebP/GIF 对应 MIME")
    if not encoded_image or not base64.b64decode(encoded_image, validate=True):
        raise ValueError("IMAGE_BASE64 不能为空")
    return [HumanMessage(content=[
        {"type": "image", "base64": encoded_image, "mime_type": IMAGE_MIME_TYPE},
        {"type": "text", "text": RECOGNITION_REQUEST},
    ])]


def build_number_messages(*instructions: str, include_json_instructions: bool = False) -> list[BaseMessage]:
    messages: list[BaseMessage] = []
    if include_json_instructions:
        messages.append(SystemMessage(content=RECOGNITION_PARSER.get_format_instructions()))
    messages.append(HumanMessage(content=f"输入数字：{json.dumps(read_number_fixture())}\n" + "\n".join(instructions)))
    return messages


def create_probe_model(
    protocol: ProtocolName, base_url: str, model_name: str, api_key: str, timeout_seconds: float,
) -> BaseChatModel:
    if protocol == "messages":
        return ChatAnthropic(
            model_name=model_name, base_url=base_url, api_key=SecretStr(api_key),
            max_tokens_to_sample=2048, timeout=timeout_seconds, max_retries=0, stop=None,
        )
    return ChatOpenAI(
        model=model_name, base_url=base_url + "/v1", api_key=SecretStr(api_key),
        timeout=timeout_seconds, max_retries=0, max_completion_tokens=2048,
        use_responses_api=protocol == "responses", use_previous_response_id=False, store=False,
    )


def bind_probe_model(
    model: BaseChatModel, protocol: ProtocolName, output_mode: OutputMode, tools_enabled: bool,
) -> Runnable[LanguageModelInput, Any]:
    """Adapt public framework bindings; Any is the framework's include_raw envelope."""
    if output_mode == "json_mode":
        if protocol == "messages" or not tools_enabled:
            # Anthropic's real UserWarning is raised by the runner's warning policy.
            return model.with_structured_output(NumberRecognition, method="json_mode", include_raw=True)
        # with_structured_output(json_mode, tools=...) does not bind tools.
        # The Chat Completions SDK parser requires strict tools with response_format.
        return model.bind_tools(TOOLS, strict=True if protocol == "chat" else None).bind(
            response_format={"type": "json_object"},
        )
    if output_mode == "json_schema":
        if protocol == "messages" and tools_enabled:
            # ChatAnthropic's structured wrapper does not bind tools; use its public
            # native format binding and the same Pydantic parser on the returned text.
            return model.bind_tools(TOOLS, output_config={
                "format": {"type": "json_schema", "schema": transform_schema(NumberRecognition)},
            })
        binding_options: dict[str, Any] = {} if protocol == "messages" else {"strict": True}
        if tools_enabled:
            binding_options["tools"] = TOOLS
        return model.with_structured_output(
            NumberRecognition, method="json_schema", include_raw=True, **binding_options,
        )
    return model.bind_tools(TOOLS) if tools_enabled else model


def validate_json_response(model_output: Any, ai_response: AIMessage) -> list[ProbeDiagnostic]:
    """Check framework schema results, then warn if the whole body is not JSON."""
    parsed_response = None
    if isinstance(model_output, dict):
        parsing_error = model_output.get("parsing_error")
        if isinstance(parsing_error, Exception):
            return [describe_exception(parsing_error, category="validation")]
        parsed_response = model_output.get("parsed")
    try:
        if not isinstance(parsed_response, NumberRecognition):
            RECOGNITION_PARSER.invoke(ai_response)
    except (OutputParserException, ValidationError) as exc:
        return [describe_exception(exc, category="validation")]
    # Whole-body purity affects severity, without changing framework parsing.
    try:
        json.loads(ai_response.text)
    except json.JSONDecodeError as exc:
        return [describe_exception(exc, category="validation", severity="warning")]
    return []


async def validate_tool_calls(call_record: CaseCallRecord, ai_response: AIMessage) -> list[ProbeDiagnostic]:
    diagnostics: list[ProbeDiagnostic] = []
    missing_tools = set(call_record.expected_tools) - {tool_call["name"] for tool_call in ai_response.tool_calls}
    if missing_tools:
        diagnostics.append(describe_exception(
            ValueError("缺少所需工具：" + ", ".join(sorted(missing_tools))), category="validation",
        ))
    for invalid_call in ai_response.invalid_tool_calls:
        diagnostics.append(ProbeDiagnostic(
            category="validation", type="InvalidToolCall",
            message=invalid_call.get("error") or "工具调用参数无法解析",
        ))
    if ai_response.tool_calls:
        # ToolNode needs a public graph runtime; it owns dispatch and argument validation.
        graph = StateGraph(MessagesState)
        graph.add_node("tools", ToolNode(TOOLS))
        graph.add_edge(START, "tools")
        graph.add_edge("tools", END)
        tool_state = await graph.compile().ainvoke({"messages": [ai_response.model_copy(deep=True)]})
        call_record.tool_results = cast(list[ToolMessage], tool_state["messages"][1:])
        for tool_result in call_record.tool_results:
            if tool_result.status == "error":
                diagnostics.append(ProbeDiagnostic(category="validation", type="ToolMessage", message=tool_result.text))
    return diagnostics


class ProbeSession:
    """Own clients and this invocation's record table, without case-specific branches."""

    def __init__(self, api_key: str, timeout_seconds: float) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.case_records: dict[tuple[ProtocolName, str], CaseRecord] = {}
        self.models_by_protocol: dict[ProtocolName, BaseChatModel] = {}

    async def invoke_model(
        self, case_record: CaseRecord, messages: list[BaseMessage], *,
        output_mode: OutputMode = "plain", expected_tools: tuple[str, ...] = (),
        require_text: bool = False,
    ) -> None:
        call_record = CaseCallRecord(
            messages=[message.model_copy(deep=True) for message in messages],
            output_mode=output_mode, expected_tools=expected_tools,
        )
        case_record.calls.append(call_record)
        started_at_seconds = perf_counter()
        exception_category: ProbeDiagnosticCategory | None = None
        try:
            if case_record.protocol not in self.models_by_protocol:
                self.models_by_protocol[case_record.protocol] = create_probe_model(
                    case_record.protocol, case_record.base_url, case_record.model_name,
                    self.api_key, self.timeout_seconds,
                )
            bound_model = bind_probe_model(
                self.models_by_protocol[case_record.protocol], case_record.protocol, output_mode, bool(expected_tools),
            )
            model_output = await asyncio.wait_for(bound_model.ainvoke(messages), timeout=self.timeout_seconds)
            ai_response = model_output["raw"] if isinstance(model_output, dict) else model_output
            if not isinstance(ai_response, AIMessage):
                raise TypeError("客户端未返回 AIMessage")
            call_record.response = ai_response
            if require_text:
                call_record.checks["text"] = []
                if not ai_response.text.strip():
                    call_record.checks["text"].append(
                        describe_exception(ValueError("回复正文为空"), category="validation"),
                    )
            if output_mode != "plain":
                call_record.checks["json"] = validate_json_response(model_output, ai_response)
            if expected_tools:
                exception_category = "tool"
                call_record.checks["tools"] = await validate_tool_calls(call_record, ai_response)
        except Exception as exc:
            case_record.error = describe_exception(exc, category=exception_category)
            raise
        finally:
            call_record.elapsed_seconds = perf_counter() - started_at_seconds


async def probe_image_recognition(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(case_record, build_image_messages(), require_text=True)


async def probe_image_history_followup(session: ProbeSession, case_record: CaseRecord) -> None:
    recognition_record = session.case_records.get((case_record.protocol, "case1"))
    if recognition_record is None:
        await probe_image_recognition(session, case_record)
        if case_record.status != "PASS":
            return
        recognition_call = case_record.calls[-1]
    else:
        recognition_call = recognition_record.calls[-1]
    messages = [message.model_copy(deep=True) for message in recognition_call.messages]
    for message in messages:
        if isinstance(message.content, list):
            message.content = [
                {"type": "text", "text": IMAGE_PLACEHOLDER}
                if isinstance(block, dict) and block.get("type") == "image" else block
                for block in message.content
            ]
    assert recognition_call.response is not None
    messages.extend([recognition_call.response.model_copy(deep=True), HumanMessage(content="数字总数是多少？")])
    await session.invoke_model(case_record, messages, require_text=True)


async def probe_prompted_json(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(
        case_record, build_number_messages(RECOGNITION_REQUEST, include_json_instructions=True),
        output_mode="prompt_json",
    )


async def probe_json_mode(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(
        case_record, build_number_messages(RECOGNITION_REQUEST, include_json_instructions=True),
        output_mode="json_mode",
    )


async def probe_json_schema(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(case_record, build_number_messages(RECOGNITION_REQUEST), output_mode="json_schema")


async def probe_tool_b(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(case_record, build_number_messages(PROCESSING_REQUEST), expected_tools=("B",))


async def probe_tools_a_and_b(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(
        case_record, build_number_messages("请对这些数字同时执行 Tool A 和 Tool B 的计算。"),
        expected_tools=("A", "B"),
    )


async def probe_tool_b_with_prompted_json(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(
        case_record, build_number_messages(RECOGNITION_REQUEST, PROCESSING_REQUEST, include_json_instructions=True),
        output_mode="prompt_json", expected_tools=("B",),
    )


async def probe_tool_b_with_json_mode(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(
        case_record, build_number_messages(RECOGNITION_REQUEST, PROCESSING_REQUEST, include_json_instructions=True),
        output_mode="json_mode", expected_tools=("B",),
    )


async def probe_tool_b_with_json_schema(session: ProbeSession, case_record: CaseRecord) -> None:
    await session.invoke_model(
        case_record, build_number_messages(RECOGNITION_REQUEST, PROCESSING_REQUEST),
        output_mode="json_schema", expected_tools=("B",),
    )


async def probe_ai_tail(session: ProbeSession, case_record: CaseRecord) -> None:
    numbers = read_number_fixture()
    prefilled_recognition = NumberRecognition(description="收到一些数字", num=numbers, sum=math.fsum(numbers))
    messages = build_number_messages(RECOGNITION_REQUEST)
    messages.append(AIMessage(content=prefilled_recognition.model_dump_json() + "\n另外还需要输出所有数字的总数"))
    await session.invoke_model(case_record, messages)
    case_record.manual_review = True


@dataclass(frozen=True)
class CaseDefinition:
    case_id: str
    description: str
    execute: Callable[[ProbeSession, CaseRecord], Awaitable[None]]
    skip_if_failed: str | None = None


CASES = (
    CaseDefinition("case1", "识图：非空正文，内容人工核验", probe_image_recognition),
    CaseDefinition("case1a", "图源替换与历史计数", probe_image_history_followup, "case1"),
    CaseDefinition("case2.1", "JSON：提示词要求", probe_prompted_json),
    CaseDefinition("case2.2", "JSON：json_mode", probe_json_mode),
    CaseDefinition("case2.3", "JSON：json_schema", probe_json_schema),
    CaseDefinition("case3", "ToolCall：B", probe_tool_b),
    CaseDefinition("case3a", "Multi-ToolCall：A 和 B", probe_tools_a_and_b, "case3"),
    CaseDefinition("case4.1", "ToolCall + JSON：提示词要求", probe_tool_b_with_prompted_json, "case3"),
    CaseDefinition("case4.2", "ToolCall + JSON：json_mode", probe_tool_b_with_json_mode, "case3"),
    CaseDefinition("case4.3", "ToolCall + JSON：json_schema", probe_tool_b_with_json_schema, "case3"),
    CaseDefinition("case5", "AI Tail：人工检查", probe_ai_tail),
)


def save_case_record(case_record: CaseRecord, report_directory: Path) -> Path:
    report_path = report_directory / case_record.protocol / f"{case_record.case_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as report_file:
        report_file.write(case_record.model_dump_json(indent=2) + "\n")
    return report_path


def format_diagnostic(diagnostic: ProbeDiagnostic) -> str:
    summary = f"{diagnostic.category}/{diagnostic.type}"
    if diagnostic.severity == "warning":
        summary = f"WARNING {summary}"
    if diagnostic.http_status is not None:
        summary += f" (HTTP {diagnostic.http_status})"
    return summary


def format_case_result(case_record: CaseRecord) -> str:
    details: list[str] = []
    if case_record.error is not None:
        details.append(format_diagnostic(case_record.error))
    for call_record in case_record.calls:
        for check_name, diagnostics in call_record.checks.items():
            details.extend(f"{check_name}: {format_diagnostic(diagnostic)}" for diagnostic in diagnostics)
    if case_record.skipped_because is not None:
        details.append(f"跳过: {case_record.skipped_because}")
    return f"{case_record.status} [{'; '.join(details)}]" if details else case_record.status


async def run_probes(args: argparse.Namespace, api_key: str) -> list[CaseRecord]:
    session = ProbeSession(api_key, args.timeout)
    safe_model_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", args.model).strip(". ") or "model"
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%f")
    report_directory = Path(__file__).resolve().parent / f"{safe_model_name}_llm_check_{timestamp}"
    with tracing_context(enabled=False):
        for protocol in PROTOCOLS:
            if args.protocol and protocol not in args.protocol:
                continue
            for case_definition in CASES:
                if args.case_ids and case_definition.case_id not in args.case_ids:
                    continue
                case_record = CaseRecord(
                    protocol=protocol, case_id=case_definition.case_id,
                    case_description=case_definition.description, model_name=args.model, base_url=args.base_url,
                )
                print(f"开始 {protocol} / {case_record.case_id}: {case_record.case_description}", flush=True)
                dependency_record = (
                    session.case_records.get((protocol, case_definition.skip_if_failed))
                    if case_definition.skip_if_failed is not None else None
                )
                if dependency_record is not None and dependency_record.status != "PASS":
                    case_record.skipped_because = f"{dependency_record.case_id}: {dependency_record.status}"
                else:
                    try:
                        with warnings.catch_warnings():
                            warnings.simplefilter("error", UserWarning)
                            await case_definition.execute(session, case_record)
                    except Exception as exc:
                        if case_record.error is None:
                            case_record.error = describe_exception(exc, category="configuration")
                session.case_records[(protocol, case_record.case_id)] = case_record
                report_path = save_case_record(case_record, report_directory)
                print(
                    f"结束 {protocol} / {case_record.case_id}: {format_case_result(case_record)} -> {report_path}",
                    flush=True,
                )
    print("\n总览（PASS 仅表示通过自动检查）", flush=True)
    for case_record in session.case_records.values():
        print(f"{case_record.protocol:9} | {case_record.case_id:8} | {format_case_result(case_record)}", flush=True)
    return list(session.case_records.values())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="公共接口根地址，不含 /v1 或具体接口路径；未提供时交互输入")
    parser.add_argument("--model", help="模型名称；未提供时交互输入")
    parser.add_argument("--api-key", help="API Key；未提供时隐藏输入")
    parser.add_argument("--protocol", choices=PROTOCOLS, nargs="+", action="extend")
    parser.add_argument("--case", dest="case_ids", choices=[case.case_id for case in CASES], nargs="+", action="extend")
    parser.add_argument("--timeout", type=float, default=60, help="每次模型调用超时秒数，默认 60")
    parser.add_argument("--list", action="store_true", help="列出协议与 case，不发起检测")
    args = parser.parse_args(argv)
    if args.list:
        print("协议：" + ", ".join(PROTOCOLS))
        for case_definition in CASES:
            print(f"{case_definition.case_id:8} {case_definition.description}")
        return 0
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout 必须为正的有限秒数")
    try:
        args.model = (args.model if args.model is not None else input("Model name：")).strip()
        args.base_url = (args.base_url if args.base_url is not None else input("Base URL：")).strip().rstrip("/")
        if not args.model:
            parser.error("--model 不能为空")
        try:
            parsed_base_url = urlsplit(args.base_url)
        except ValueError as exc:
            parser.error(str(exc))
        if (
            parsed_base_url.scheme not in {"https", "http"}
            or not parsed_base_url.hostname
            or parsed_base_url.username or parsed_base_url.password
            or parsed_base_url.query or parsed_base_url.fragment
            or parsed_base_url.path.endswith(("/v1", "/messages", "/responses", "/chat/completions"))
        ):
            parser.error("--base-url 必须为不含凭据、查询参数、/v1 或具体接口路径的 HTTP(S) 根地址")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            api_key = (args.api_key if args.api_key is not None else getpass.getpass("API Key（隐藏输入）：")).strip()
        if not api_key:
            parser.error("API Key 不能为空")
        asyncio.run(run_probes(args, api_key))
    except (KeyboardInterrupt, EOFError):
        print("\n已停止。")
        return 130
    except (OSError, getpass.GetPassWarning) as exc:
        print(f"{type(exc).__name__}: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
