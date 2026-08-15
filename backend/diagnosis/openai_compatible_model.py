"""generic OpenAI-compatible Chat Completions adapter（spec 7.8/R26）。

只用已安装的 OpenAI SDK（AsyncOpenAI + Agents SDK 的
OpenAIChatCompletionsModel）；不引入新 gateway/framework。官方 OpenAI 走
Responses adapter 并钉死 endpoint；DeepSeek 是本 adapter 的预设；任何自定义
地址必须显式选择 openai_compatible 且只从 DIAGOPS_AGENTS_API_KEY 读凭证。
"""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

import openai
from agents import FunctionTool, ModelResponse
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from pydantic import SecretStr

from backend.domain.multi_agent import FailureCategory

COMPATIBLE_ADAPTER_VERSION = "openai-compatible-adapter-v2"
StructuredOutputTransport = Literal["native_json_schema", "strict_output_tool"]
STRICT_TOOL_ENVELOPE_SCHEMA = {
    "type": "object",
    "properties": {"payload_json": {"type": "string"}},
    "required": ["payload_json"],
    "additionalProperties": False,
}


def _salvage_json_text(text: str) -> str:
    """截掉完整 JSON 值之后追加的垃圾（网关联发双份、尾括号等）。

    只在文本整体不是合法 JSON、但以完整 JSON 值开头且其后还有非空白内容时
    改写；截断 JSON、前导垃圾、纯文本一律原样返回。确定性修复，不猜内容。
    """
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass
    try:
        _, end = json.JSONDecoder().raw_decode(text, len(text) - len(stripped))
    except json.JSONDecodeError:
        return text
    if text[end:].strip():
        return text[:end]
    return text


def _salvage_response_json(
    response: ModelResponse, *, structured_output: bool
) -> ModelResponse:
    """清洗响应里契约上必须是 JSON 的字段。

    function_call arguments 恒为 JSON；message 文本只在期望结构化输出
    （output_schema 非空）时才按 JSON salvage，纯文本输出绝不触碰。
    取证取舍：原地改写后持久化的 model event 是清洗后形态，网关原始垃圾
    字节不再可见——与执行语义一致，畸形取证需在适配器之外抓包。
    """
    for item in response.output:
        item_type = getattr(item, "type", None)
        if item_type == "function_call":
            arguments = getattr(item, "arguments", None)
            if isinstance(arguments, str):
                item.arguments = _salvage_json_text(arguments)
        elif structured_output and item_type == "message":
            for content in getattr(item, "content", []):
                text = getattr(content, "text", None)
                if isinstance(text, str):
                    content.text = _salvage_json_text(text)
    return response


def _unwrap_strict_tool_payload(raw_input: str) -> str:
    envelope = json.loads(raw_input)
    if set(envelope) != {"payload_json"} or not isinstance(
        envelope["payload_json"], str
    ):
        raise ValueError("strict tool envelope is invalid")
    return _salvage_json_text(envelope["payload_json"])


def strict_transport_tools(tools: list[Any]) -> list[Any]:
    projected = []
    for tool in tools:
        if (
            isinstance(tool, FunctionTool)
            and tool.params_json_schema != STRICT_TOOL_ENVELOPE_SCHEMA
        ):
            original_invoke = tool.on_invoke_tool
            original_schema = copy.deepcopy(tool.params_json_schema)

            async def invoke(context, raw_input, *, _original=original_invoke):
                return await _original(context, _unwrap_strict_tool_payload(raw_input))

            tool = copy.copy(tool)
            tool.description = (
                f"{tool.description}\n"
                "Encode the function arguments as a JSON string in payload_json. "
                "The decoded JSON must match this schema: "
                f"{json.dumps(original_schema, ensure_ascii=False, sort_keys=True)}"
            )
            tool.params_json_schema = copy.deepcopy(STRICT_TOOL_ENVELOPE_SCHEMA)
            tool.on_invoke_tool = invoke
            tool.strict_json_schema = True
        projected.append(tool)
    return projected


def _restore_strict_tool_arguments(
    response: ModelResponse,
    *,
    output_tool_name: str = "submit_structured_output",
) -> ModelResponse:
    for item in response.output:
        if (
            getattr(item, "type", None) == "function_call"
            and getattr(item, "name", None) != output_tool_name
        ):
            item.arguments = _unwrap_strict_tool_payload(item.arguments)
    return response


class OpenAICompatibleChatCompletionsModel(OpenAIChatCompletionsModel):
    """请求级 client 的 generic Chat Completions adapter。

    与 DeepSeek 预设共享同一语义：同步 orchestrator 可能跨 event loop 复用
    adapter，因此不在实例上持有异步 transport；每次请求在当前 loop 创建并
    显式关闭 client，取消/超时后没有晚到的 transport 提交。
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        timeout_seconds: float | None = None,
        max_retries: int = 2,
        strict_feature_validation: bool = False,
        structured_output_transport: StructuredOutputTransport = "native_json_schema",
    ) -> None:
        self.model = model
        self._api_key = SecretStr(api_key)
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._strict_feature_validation = strict_feature_validation
        self.structured_output_transport = structured_output_transport

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        """在当前 event loop 内完成一次非流式请求并释放 transport。"""
        client = self._create_client()
        try:
            delegate = self._create_delegate(client)
            output_schema = args[4] if len(args) >= 5 else kwargs.get("output_schema")
            if self.structured_output_transport == "strict_output_tool":
                args = list(args)
                kwargs = dict(kwargs)
                if len(args) >= 5:
                    args[3] = strict_transport_tools(args[3])
                    args[4] = None
                else:
                    kwargs["tools"] = strict_transport_tools(kwargs.get("tools", []))
                    kwargs["output_schema"] = None
            response = await delegate.get_response(*args, **kwargs)
            if isinstance(response, ModelResponse):
                # 先 salvage 再拆 strict envelope：垃圾可能附在 envelope 外层。
                response = _salvage_response_json(
                    response, structured_output=output_schema is not None
                )
                if self.structured_output_transport == "strict_output_tool":
                    return _restore_strict_tool_arguments(response)
            return response
        finally:
            await client.close()

    async def stream_response(self, *args: Any, **kwargs: Any):
        """在消费或取消流后，于创建 transport 的 event loop 内关闭它。

        注意：本路径未做 JSON salvage；V11 当前全部走非流式 Runner.run，
        未来启用 run_streamed 前必须先在流式聚合处补齐等价清洗。
        """
        client = self._create_client()
        try:
            delegate = self._create_delegate(client)
            async for event in delegate.stream_response(*args, **kwargs):
                yield event
        finally:
            await client.close()

    def _create_client(self) -> AsyncOpenAI:
        kwargs: dict[str, Any] = {
            "api_key": self._api_key.get_secret_value(),
            "base_url": self._base_url,
            "max_retries": self._max_retries,
        }
        if self._timeout_seconds is not None:
            kwargs["timeout"] = self._timeout_seconds
        return AsyncOpenAI(**kwargs)

    def _create_delegate(self, client: AsyncOpenAI) -> OpenAIChatCompletionsModel:
        return OpenAIChatCompletionsModel(
            model=self.model,
            openai_client=client,
            strict_feature_validation=self._strict_feature_validation,
        )

    def _supports_default_prompt_cache_key(self) -> bool:
        # Agents SDK 的默认 cache key 仅针对官方 OpenAI endpoint。
        return False

    def clone_for_model(
        self,
        model_name: str,
        *,
        max_retries: int | None = None,
    ) -> OpenAICompatibleChatCompletionsModel:
        """为冻结 Run 创建不共享传输状态的同凭据 adapter。"""
        return type(self)(
            model=model_name,
            api_key=self._api_key.get_secret_value(),
            base_url=self._base_url,
            timeout_seconds=self._timeout_seconds,
            max_retries=(
                self._max_retries if max_retries is None else max_retries
            ),
            strict_feature_validation=self._strict_feature_validation,
            structured_output_transport=self.structured_output_transport,
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"


def create_openai_compatible_model(
    model_name: str | None,
    api_key: str | None,
    base_url: str | None,
    *,
    timeout_seconds: float | None = None,
    max_retries: int = 2,
    structured_output_transport: StructuredOutputTransport = "native_json_schema",
) -> OpenAICompatibleChatCompletionsModel | None:
    """缺少 model/key/canonical base_url 时不创建 client；绝不读官方凭证。"""
    model = (model_name or "").strip()
    key = (api_key or "").strip()
    url = (base_url or "").strip()
    if not model or not key or not url:
        return None
    return OpenAICompatibleChatCompletionsModel(
        model=model,
        api_key=key,
        base_url=url,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
        structured_output_transport=structured_output_transport,
    )


def classify_compatible_failure(exc: BaseException) -> FailureCategory:
    """把 OpenAI SDK 异常映射到稳定失败分类，不解析异常文本。"""
    if isinstance(exc, openai.AuthenticationError):
        return FailureCategory.AUTHENTICATION
    if isinstance(exc, openai.RateLimitError):
        return FailureCategory.RATE_LIMIT
    if isinstance(exc, (openai.APITimeoutError, TimeoutError)):
        return FailureCategory.TIMEOUT
    if isinstance(exc, openai.APIConnectionError):
        return FailureCategory.TRANSPORT
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        if status == 402:
            return FailureCategory.QUOTA
        return FailureCategory.TRANSPORT
    return FailureCategory.UNKNOWN
