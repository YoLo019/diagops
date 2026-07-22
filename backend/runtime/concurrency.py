from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar


class RunStepGate:
    """统一限制单个 Run 的外部并行步骤；同一 Task 的嵌套工具调用可重入。"""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("run step concurrency limit must be positive")
        self.limit = limit
        self._semaphore = asyncio.Semaphore(limit)
        self._depth: ContextVar[int] = ContextVar(
            f"run_step_gate_depth_{id(self)}", default=0
        )

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        depth = self._depth.get()
        token = self._depth.set(depth + 1)
        acquired = depth == 0
        if acquired:
            await self._semaphore.acquire()
        try:
            yield
        finally:
            self._depth.reset(token)
            if acquired:
                self._semaphore.release()
