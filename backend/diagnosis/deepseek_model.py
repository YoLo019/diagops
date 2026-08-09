from dataclasses import dataclass
from typing import Literal

from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI

from backend.diagnosis.openai_compatible_model import (
    OpenAICompatibleChatCompletionsModel,
)

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


class DeepSeekChatCompletionsModel(OpenAICompatibleChatCompletionsModel):
    """共享 compatible adapter 的 DeepSeek 预设；序列化身份保持 deepseek 不变。"""

    def __init__(self, *, model: str, api_key: str) -> None:
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            strict_feature_validation=True,
        )

    def _create_client(self) -> AsyncOpenAI:
        # DeepSeek 预设只发送官方 endpoint 支持的字段，不带超时/重试覆盖。
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

    def clone_for_model(self, model_name: str) -> "DeepSeekChatCompletionsModel":
        """为冻结 Run 创建不共享传输状态的同凭据 adapter。"""
        return type(self)(
            model=model_name,
            api_key=self._api_key.get_secret_value(),
        )


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
