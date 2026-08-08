"""generic OpenAI-compatible Chat Completions adapter（spec 7.8/R26）。

只用已安装的 OpenAI SDK（AsyncOpenAI + Agents SDK 的
OpenAIChatCompletionsModel）；不引入新 gateway/framework。官方 OpenAI 走
Responses adapter 并钉死 endpoint；DeepSeek 是本 adapter 的预设；任何自定义
地址必须显式选择 openai_compatible 且只从 DIAGOPS_AGENTS_API_KEY 读凭证。
"""

from __future__ import annotations

from typing import Any

import openai
from agents.models.openai_chatcompletions import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from pydantic import SecretStr

from backend.domain.multi_agent import FailureCategory

COMPATIBLE_ADAPTER_VERSION = "openai-compatible-adapter-v1"


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
    ) -> None:
        self.model = model
        self._api_key = SecretStr(api_key)
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._strict_feature_validation = strict_feature_validation

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
