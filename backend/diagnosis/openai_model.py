from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from agents.models.openai_responses import OpenAIResponsesModel
from openai import AsyncOpenAI


def transport_timeout_seconds(overall_timeout_seconds: float) -> float:
    """为 SDK 取消和 client 清理预留最多五秒。"""
    return max(1.0, overall_timeout_seconds - 5.0)


@asynccontextmanager
async def openai_responses_model(
    model_name: str,
    overall_timeout_seconds: float,
) -> AsyncIterator[OpenAIResponsesModel]:
    """创建由当前运行独占且最多自动重试两次的 OpenAI Responses model。"""
    client = AsyncOpenAI(
        timeout=transport_timeout_seconds(overall_timeout_seconds),
        max_retries=2,
    )
    try:
        yield OpenAIResponsesModel(model=model_name, openai_client=client)
    finally:
        await client.close()
