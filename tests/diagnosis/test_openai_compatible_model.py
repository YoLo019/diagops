"""generic compatible adapter 的 fake-endpoint 失败矩阵（不接触真实模型/网络）。"""

import asyncio
import time

import httpx
import openai
import pytest
from agents import FunctionTool, ModelResponse, ModelSettings, Usage
from agents.models.interface import ModelTracing
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

import backend.diagnosis.openai_compatible_model as compatible_model
from backend.diagnosis.agents_runtime import _disables_sdk_tracing, _is_deepseek
from backend.diagnosis.openai_compatible_model import (
    OpenAICompatibleChatCompletionsModel,
    classify_compatible_failure,
    create_openai_compatible_model,
)
from backend.domain.multi_agent import FailureCategory

_BASE_URL = "http://127.0.0.1:8000/v1"


def _create(**overrides):
    values = {
        "model_name": "compat-model",
        "api_key": "local-secret",
        "base_url": _BASE_URL,
    }
    values.update(overrides)
    return create_openai_compatible_model(**values)


@pytest.mark.anyio
async def test_strict_output_tool_transport_omits_native_response_format(monkeypatch):
    captured = {}

    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            captured.update(
                {
                    "system_instructions": system_instructions,
                    "input": input,
                    "model_settings": model_settings,
                    "tools": tools,
                    "output_schema": output_schema,
                    "handoffs": handoffs,
                    "tracing": tracing,
                    **kwargs,
                }
            )
            return "response"

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = _create(structured_output_transport="strict_output_tool")

    async def invoke(_context, _raw_input):
        return "ok"

    tool = FunctionTool(
        name="read_logs",
        description="Read logs.",
        params_json_schema={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "maxLength": 240},
                "keywords": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "maxItems": 8,
                },
            },
            "required": ["reason", "keywords"],
            "additionalProperties": False,
        },
        on_invoke_tool=invoke,
    )
    async def submit(_context, raw_input):
        return raw_input

    output_tool = FunctionTool(
        name="submit_structured_output",
        description="Submit output.",
        params_json_schema=compatible_model.STRICT_TOOL_ENVELOPE_SCHEMA,
        on_invoke_tool=submit,
        strict_json_schema=True,
    )
    assert await model.get_response(
        None, "input", object(), [tool, output_tool], object(), [], object()
    ) == "response"
    assert captured["output_schema"] is None
    sent_tool = captured["tools"][0]
    assert set(sent_tool.params_json_schema["properties"]) == {"payload_json"}
    assert '"reason"' in sent_tool.description
    assert '"keywords"' in sent_tool.description
    assert await sent_tool.on_invoke_tool(
        None,
        '{"payload_json":"{\\"reason\\":\\"inspect\\",\\"keywords\\":[]}"}',
    ) == "ok"
    sent_output_tool = captured["tools"][1]
    assert await sent_output_tool.on_invoke_tool(
        None, '{"payload_json":"{\\"value\\":\\"valid\\"}"}'
    ) == '{"payload_json":"{\\"value\\":\\"valid\\"}"}'
    assert tool.params_json_schema["properties"]["reason"]["maxLength"] == 240


def test_clone_preserves_structured_output_transport():
    model = _create(structured_output_transport="strict_output_tool")

    clone = model.clone_for_model("other-model")

    assert clone.structured_output_transport == "strict_output_tool"


@pytest.mark.anyio
async def test_strict_output_tool_transport_restores_tool_arguments(monkeypatch):
    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        arguments=(
                            '{"payload_json":"{\\"reason\\":\\"inspect\\"}"}'
                        ),
                        call_id="call-1",
                        name="read_logs",
                        type="function_call",
                    ),
                    ResponseFunctionToolCall(
                        arguments=(
                            '{"payload_json":"{\\"value\\":\\"valid\\"}"}'
                        ),
                        call_id="call-2",
                        name="submit_structured_output",
                        type="function_call",
                    ),
                ],
                usage=Usage(),
                response_id=None,
            )

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)

    response = await _create(
        structured_output_transport="strict_output_tool"
    ).get_response(None, "input", object(), [], object(), [], object())

    assert response.output[0].arguments == '{"reason":"inspect"}'
    assert response.output[1].arguments == (
        '{"payload_json":"{\\"value\\":\\"valid\\"}"}'
    )


def test_create_requires_model_key_and_canonical_url():
    assert _create(model_name=None) is None
    assert _create(api_key="") is None
    assert _create(base_url=None) is None

    model = _create()
    assert isinstance(model, OpenAICompatibleChatCompletionsModel)
    assert model.model == "compat-model"
    assert "local-secret" not in repr(model)


def test_adapter_does_not_read_official_provider_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-official-secret-1234567890")
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def close(self):
            pass

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", FakeClient)
    model = _create(timeout_seconds=12.0, max_retries=1)

    model._create_client()

    # 凭证只来自显式参数（DIAGOPS_AGENTS_API_KEY 注入），绝不读官方环境变量。
    assert captured == {
        "api_key": "local-secret",
        "base_url": _BASE_URL,
        "max_retries": 1,
        "timeout": 12.0,
    }


def test_v11_model_clone_explicitly_disables_provider_retry(monkeypatch):
    captured = {}

    class FakeClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", FakeClient)
    model = _create(max_retries=2)
    v11_model = model.clone_for_model("compat-model", max_retries=0)

    v11_model._create_client()

    assert captured["max_retries"] == 0


@pytest.mark.anyio
async def test_get_response_closes_request_client_on_success_and_error(monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.loop = asyncio.get_running_loop()
            self.closed = False
            clients.append(self)

        async def close(self):
            assert asyncio.get_running_loop() is self.loop
            self.closed = True

    class FakeDelegate:
        def __init__(self, *, model, openai_client, strict_feature_validation):
            assert model == "compat-model"
            assert strict_feature_validation is False

        async def get_response(self, *_args, **_kwargs):
            return "response"

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = _create()

    assert await model.get_response(None, "input", object(), [], None, [], object()) == "response"
    assert len(clients) == 1 and clients[0].closed is True

    class FailingDelegate(FakeDelegate):
        async def get_response(self, *_args, **_kwargs):
            raise RuntimeError("provider failed")

    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FailingDelegate)
    with pytest.raises(RuntimeError, match="provider failed"):
        await model.get_response(None, "input", object(), [], None, [], object())
    assert len(clients) == 2 and clients[1].closed is True


def test_adapter_supports_sequential_event_loops(monkeypatch):
    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.loop = asyncio.get_running_loop()
            self.closed = False
            clients.append(self)

        async def close(self):
            assert asyncio.get_running_loop() is self.loop
            self.closed = True

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return asyncio.get_running_loop()

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = _create()

    async def invoke():
        return await model.get_response(None, "input", object(), [], None, [], object())

    first = asyncio.run(invoke())
    second = asyncio.run(invoke())

    assert first is not second
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_clone_for_model_shares_no_transport():
    model = _create()

    clone = model.clone_for_model("other-model")

    assert isinstance(clone, OpenAICompatibleChatCompletionsModel)
    assert clone is not model
    assert clone.model == "other-model"
    assert clone._base_url == model._base_url
    assert "local-secret" not in repr(clone)


def test_tracing_and_deepseek_boundaries():
    model = _create()

    # generic endpoint 禁用 SDK 云 tracing，但绝不发送 DeepSeek-only 请求字段。
    assert _disables_sdk_tracing(model) is True
    assert _is_deepseek(model) is False


def _real_client(handler) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key="local-secret",
        base_url=_BASE_URL,
        max_retries=0,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_BASE_URL
        ),
    )


@pytest.mark.anyio
async def test_fake_endpoint_failure_categories():
    async def classify(status: int) -> FailureCategory:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"error": {"message": "x"}})

        client = _real_client(handler)
        try:
            await client.chat.completions.create(
                model="compat-model", messages=[{"role": "user", "content": "hi"}]
            )
            raise AssertionError("request must fail")
        except openai.APIError as exc:
            return classify_compatible_failure(exc)
        finally:
            await client.close()

    assert await classify(401) == FailureCategory.AUTHENTICATION
    assert await classify(429) == FailureCategory.RATE_LIMIT
    assert await classify(500) == FailureCategory.TRANSPORT


@pytest.mark.anyio
async def test_fake_endpoint_timeout_and_connection_categories():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow endpoint")

    client = _real_client(timeout_handler)
    try:
        with pytest.raises(openai.APITimeoutError):
            await client.chat.completions.create(
                model="compat-model", messages=[{"role": "user", "content": "hi"}]
            )
    finally:
        await client.close()

    def connection_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = _real_client(connection_handler)
    try:
        with pytest.raises(openai.APIConnectionError) as captured:
            await client.chat.completions.create(
                model="compat-model", messages=[{"role": "user", "content": "hi"}]
            )
        assert classify_compatible_failure(captured.value) == FailureCategory.TRANSPORT
    finally:
        await client.close()


def test_classification_never_leaks_secret():
    exc = openai.AuthenticationError(
        message="invalid api key local-secret",
        response=httpx.Response(401, request=httpx.Request("POST", "http://x")),
        body=None,
    )

    category = classify_compatible_failure(exc)

    assert category == FailureCategory.AUTHENTICATION
    assert "local-secret" not in str(category)


# ---------------------------------------------------------------------------
# spec 7.8 / plan T6 step 3-4：local slow-endpoint 取消语义证据。
# live 认证不声明远端停止计算，本地必须证明：client cancellation、deadline
# enforcement、no late commit、transport cleanup。
# ---------------------------------------------------------------------------

_SLOW_COMPLETION = {
    "id": "chatcmpl-slow",
    "object": "chat.completion",
    "created": 0,
    "model": "compat-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "late"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class _SlowEndpoint:
    """30 秒后才响应的 fake endpoint，记录取消是否传播到在途请求。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.observed_cancel = False

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            # 取消必须穿透 SDK/httpx 到达在途 handler，而不是只停在本层。
            self.observed_cancel = True
            raise
        return httpx.Response(200, json=_SLOW_COMPLETION)


def _patch_real_sdk_transport(monkeypatch, handler) -> list[httpx.AsyncClient]:
    """让 adapter 的每请求 client 走真实 OpenAI SDK + httpx MockTransport。

    只替换 AsyncOpenAI 工厂以注入 mock transport，delegate 与 SDK 请求路径
    保持真实，取消语义才有证明力；强制 max_retries=0 保证单次在途请求。
    """
    http_clients: list[httpx.AsyncClient] = []

    def factory(**kwargs):
        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_BASE_URL
        )
        kwargs["http_client"] = http_client
        kwargs["max_retries"] = 0
        http_clients.append(http_client)
        return openai.AsyncOpenAI(**kwargs)

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", factory)
    return http_clients


def _real_call(model):
    """经 adapter 驱动真实 delegate 的一次非流式 Chat Completions 请求。"""
    return model.get_response(
        None, "hi", ModelSettings(), [], None, [], ModelTracing.DISABLED
    )


@pytest.mark.anyio
async def test_slow_endpoint_client_cancellation_reaches_inflight_request(monkeypatch):
    endpoint = _SlowEndpoint()
    http_clients = _patch_real_sdk_transport(monkeypatch, endpoint.handler)
    model = _create(timeout_seconds=30.0)

    task = asyncio.create_task(_real_call(model))
    await asyncio.wait_for(endpoint.started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 取消传播到在途 HTTP 请求，且取消路径上 transport 已关闭。
    assert endpoint.observed_cancel is True
    assert len(http_clients) == 1
    assert http_clients[0].is_closed is True


@pytest.mark.anyio
async def test_slow_endpoint_deadline_enforced_via_wait_for(monkeypatch):
    endpoint = _SlowEndpoint()
    http_clients = _patch_real_sdk_transport(monkeypatch, endpoint.handler)
    model = _create(timeout_seconds=30.0)

    started_at = time.monotonic()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(_real_call(model), timeout=0.5)
    elapsed = time.monotonic() - started_at

    # 30 秒慢 endpoint 被 0.5 秒 deadline 截断；清理等待不允许拖住 deadline。
    assert elapsed < 5
    assert endpoint.observed_cancel is True
    assert http_clients[0].is_closed is True


@pytest.mark.anyio
async def test_slow_endpoint_no_late_commit_after_cancellation(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    commits: list[str] = []
    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.closed = False
            clients.append(self)

        async def close(self):
            self.closed = True

    class SlowDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            entered.set()
            await release.wait()
            # 晚到响应的提交点：若取消语义生效，这里永远不可达。
            commits.append("late-response-committed")
            return "late"

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", SlowDelegate)
    model = _create()

    task = asyncio.create_task(model.get_response(None, "input", object(), [], None, [], object()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # 慢 endpoint 在取消之后才交付响应，不允许产生任何提交/副作用。
    release.set()
    await asyncio.sleep(0.1)
    assert task.cancelled() is True
    assert commits == []
    assert clients[0].closed is True


@pytest.mark.anyio
async def test_slow_endpoint_cancellation_closes_request_transport(monkeypatch):
    entered = asyncio.Event()
    clients = []

    class FakeClient:
        def __init__(self, **_kwargs):
            self.loop = asyncio.get_running_loop()
            self.closed = False
            clients.append(self)

        async def close(self):
            # transport 必须在创建它的请求 loop 上关闭，即使路径已被取消。
            assert asyncio.get_running_loop() is self.loop
            self.closed = True

    class HangingDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()
            return None

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", HangingDelegate)
    model = _create()

    task = asyncio.create_task(model.get_response(None, "input", object(), [], None, [], object()))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(clients) == 1
    assert clients[0].closed is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"a": 1}', '{"a": 1}'),
        ('{"a": 1}{"a": 1}', '{"a": 1}'),
        ('{"a": 1}}', '{"a": 1}'),
        ('  {"a": 1} trailing', '  {"a": 1}'),
        ('[1, 2][3]', '[1, 2]'),
        ('{"a": "unterminated', '{"a": "unterminated'),
        ('garbage {"a": 1}', 'garbage {"a": 1}'),
        ("plain text", "plain text"),
    ],
)
def test_salvage_json_text(raw, expected):
    # 网关/模型在完整 JSON 后追加垃圾（双份拼接、多余括号）时截到第一个完整值；
    # 截断 JSON、前导垃圾、纯文本一律原样返回。
    assert compatible_model._salvage_json_text(raw) == expected


@pytest.mark.anyio
async def test_get_response_salvages_trailing_garbage_in_structured_output(monkeypatch):
    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="msg-1",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"decision":"conclude"}{"decision":"conclude"}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(),
                response_id=None,
            )

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = _create()

    structured = await model.get_response(
        None, "input", object(), [], {"type": "object"}, [], object()
    )
    assert structured.output[0].content[0].text == '{"decision":"conclude"}'

    unstructured = await model.get_response(
        None, "input", object(), [], None, [], object()
    )
    assert (
        unstructured.output[0].content[0].text
        == '{"decision":"conclude"}{"decision":"conclude"}'
    )


@pytest.mark.anyio
async def test_get_response_salvages_tool_call_arguments(monkeypatch):
    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        arguments='{"reason":"inspect"}}{"reason":"inspect"}',
                        call_id="call-1",
                        name="read_logs",
                        type="function_call",
                    )
                ],
                usage=Usage(),
                response_id=None,
            )

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)

    response = await _create().get_response(
        None, "input", object(), [], None, [], object()
    )

    assert response.output[0].arguments == '{"reason":"inspect"}'


@pytest.mark.anyio
async def test_strict_transport_salvages_dirty_envelope_arguments(monkeypatch):
    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        arguments=(
                            '{"payload_json":"{\\"reason\\":\\"inspect\\"}"}'
                            + '{"payload_json":"{\\"reason\\":\\"inspect\\"}"}'
                        ),
                        call_id="call-1",
                        name="read_logs",
                        type="function_call",
                    ),
                    ResponseFunctionToolCall(
                        arguments=(
                            '{"payload_json":"{\\"reason\\":\\"inspect\\"}'
                            + ' trailing"}'
                        ),
                        call_id="call-2",
                        name="read_logs",
                        type="function_call",
                    ),
                ],
                usage=Usage(),
                response_id=None,
            )

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)

    response = await _create(
        structured_output_transport="strict_output_tool"
    ).get_response(None, "input", object(), [], object(), [], object())

    # 第一份：envelope 外层被双份拼接，salvage 截断后正常拆封。
    assert response.output[0].arguments == '{"reason":"inspect"}'
    # 第二份：envelope 合法但 payload 内层带垃圾，拆封时 salvage 内层。
    assert response.output[1].arguments == '{"reason":"inspect"}'


@pytest.mark.anyio
async def test_get_response_salvages_structured_output_with_kwargs_call(monkeypatch):
    # V11 生产路径经 _V11BudgetedModel 以全关键字转发，必须覆盖 kwargs 分支。
    class FakeClient:
        async def close(self):
            pass

    class FakeDelegate:
        def __init__(self, **_kwargs):
            pass

        async def get_response(self, *_args, **_kwargs):
            return ModelResponse(
                output=[
                    ResponseOutputMessage(
                        id="msg-1",
                        content=[
                            ResponseOutputText(
                                annotations=[],
                                text='{"decision":"conclude"}}',
                                type="output_text",
                            )
                        ],
                        role="assistant",
                        status="completed",
                        type="message",
                    )
                ],
                usage=Usage(),
                response_id=None,
            )

    monkeypatch.setattr(compatible_model, "AsyncOpenAI", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(compatible_model, "OpenAIChatCompletionsModel", FakeDelegate)
    model = _create()

    response = await model.get_response(
        system_instructions=None,
        input="input",
        model_settings=object(),
        tools=[],
        output_schema={"type": "object"},
        handoffs=[],
        tracing=object(),
    )

    assert response.output[0].content[0].text == '{"decision":"conclude"}'
