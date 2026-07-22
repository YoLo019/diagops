from dataclasses import dataclass
from typing import Any, Literal

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from pydantic import SecretStr

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


@dataclass(frozen=True)
class DeepSeekCapabilityProfile:
    """记录 V8.1 依赖的官方 DeepSeek 能力，避免通过付费请求动态探测。"""

    official_models: tuple[str, ...]
    chat_completions: bool
    tool_calls: bool
    json_object: bool
    local_schema_validation: bool
    strict_function_schemas: bool

    @property
    def required_capabilities_available(self) -> bool:
        """仅当稳定 endpoint 满足运行时必需能力时才允许创建 client。"""
        return all(
            (
                self.chat_completions,
                self.tool_calls,
                self.json_object,
                self.local_schema_validation,
            )
        )


DEEPSEEK_CAPABILITIES = DeepSeekCapabilityProfile(
    official_models=("deepseek-v4-flash", "deepseek-v4-pro"),
    chat_completions=True,
    tool_calls=True,
    json_object=True,
    local_schema_validation=True,
    strict_function_schemas=False,
)


class DeepSeekChatCompletionsModel(OpenAIChatCompletionsModel):
    """通过请求级 client 复用 Agents SDK Chat Completions 适配逻辑。"""

    def __init__(self, *, model: str, api_key: str) -> None:
        # 同一 adapter 会被同步 orchestrator 跨 event loop 复用，不能持有异步传输。
        self.model = model
        self._api_key = SecretStr(api_key)

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        """在当前 event loop 内完成一次非流式请求并释放 transport。"""
        client = self._create_client()
        try:
            delegate = self._create_delegate(client)
            return await delegate.get_response(*args, **kwargs)
        finally:
            await client.close()

    async def stream_response(self, *args: Any, **kwargs: Any):
        """在消费或取消流后，于创建 transport 的 event loop 内关闭它。"""
        client = self._create_client()
        try:
            delegate = self._create_delegate(client)
            async for event in delegate.stream_response(*args, **kwargs):
                yield event
        finally:
            await client.close()

    def _create_client(self) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self._api_key.get_secret_value(),
            base_url=DEEPSEEK_BASE_URL,
        )

    def _create_delegate(self, client: AsyncOpenAI) -> OpenAIChatCompletionsModel:
        return OpenAIChatCompletionsModel(
            model=self.model,
            openai_client=client,
            strict_feature_validation=True,
        )

    def _supports_default_prompt_cache_key(self) -> bool:
        # Agents SDK 的默认 cache key 仅针对官方 OpenAI endpoint。
        return False

    def clone_for_model(self, model_name: str) -> "DeepSeekChatCompletionsModel":
        """为冻结 Run 创建不共享传输状态的同凭据 adapter。"""
        return type(self)(
            model=model_name,
            api_key=self._api_key.get_secret_value(),
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(model={self.model!r})"


def implementation_status() -> Literal["implemented", "unsupported"]:
    """返回 key-free 能力合同状态，不发起网络请求。"""
    return (
        "implemented"
        if DEEPSEEK_CAPABILITIES.required_capabilities_available
        else "unsupported"
    )


def create_deepseek_model(
    model_name: str | None,
    api_key: str | None,
) -> DeepSeekChatCompletionsModel | None:
    """创建固定官方 endpoint 的模型；缺少配置或能力时不创建 client。"""
    model = (model_name or "").strip()
    key = (api_key or "").strip()
    if not model or not key or implementation_status() == "unsupported":
        return None
    return DeepSeekChatCompletionsModel(
        model=model,
        api_key=key,
    )
