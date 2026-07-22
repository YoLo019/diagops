from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeEventType,
    RuntimePhase,
    RuntimeRun,
)
from backend.domain.tool_calls import ToolCallRecord
from backend.runtime.faults import NoFaultInjector
from backend.runtime.phases import PhaseCommit, ToolCommit
from backend.runtime.store import (
    RuntimePersistenceError,
    RuntimeStore,
    RuntimeTerminalCommit,
)


@dataclass(slots=True)
class WriterCommand:
    payload: PhaseCommit | RuntimeEventCommand | RuntimeTerminalCommit | ToolCommit
    future: asyncio.Future[
        RuntimeCheckpoint | RuntimeEvent | RuntimeRun | ToolCallRecord
    ]


@dataclass(frozen=True, slots=True)
class RuntimeEventCommand:
    run_id: str
    attempt_id: str
    lease_owner: str
    lease_version: int
    event_type: RuntimeEventType
    actor_type: RuntimeActorType
    phase: RuntimePhase | None = None
    actor_name: str | None = None
    task_id: str | None = None
    execution_id: str | None = None
    tool_call_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    safe_payload: dict[str, Any] | None = None
    schema_version: int = 1


class RuntimeWriter:
    """串行化短事务写入；外部 Agent、Provider 与 Tool I/O 不进入队列。"""

    def __init__(self, store: RuntimeStore, *, max_queue_size: int = 1024) -> None:
        self._store = store
        self._max_queue_size = max_queue_size
        self._queue: asyncio.Queue[WriterCommand | None] = asyncio.Queue(
            maxsize=max_queue_size
        )
        self._consumer: asyncio.Task[None] | None = None
        self._accepting = False
        self._state_lock = asyncio.Lock()
        self.fault_injector = NoFaultInjector()

    async def start(self) -> None:
        async with self._state_lock:
            if self._consumer is not None:
                if not self._consumer.done():
                    return
                # 无 lifespan 的兼容测试客户端会在每次请求后关闭其事件循环。
                with suppress(asyncio.CancelledError, Exception):
                    self._consumer.result()
                self._consumer = None
                if not self._queue.empty():
                    raise RuntimePersistenceError(
                        "runtime writer loop closed with pending commands"
                    )
                self._queue = asyncio.Queue(maxsize=self._max_queue_size)
            self._accepting = True
            self._consumer = asyncio.create_task(self._consume())

    async def submit(self, commit: PhaseCommit) -> RuntimeCheckpoint:
        result = await self._submit(commit)
        if not isinstance(result, RuntimeCheckpoint):
            raise RuntimePersistenceError("runtime writer returned an invalid result")
        return result

    async def submit_event(self, command: RuntimeEventCommand) -> RuntimeEvent:
        result = await self._submit(command)
        if not isinstance(result, RuntimeEvent):
            raise RuntimePersistenceError("runtime writer returned an invalid event")
        return result

    async def submit_terminal(self, commit: RuntimeTerminalCommit) -> RuntimeRun:
        result = await self._submit(commit)
        if not isinstance(result, RuntimeRun):
            raise RuntimePersistenceError("runtime writer returned an invalid run")
        return result

    async def submit_tool(self, commit: ToolCommit) -> ToolCallRecord:
        result = await self._submit(commit)
        if not isinstance(result, ToolCallRecord):
            raise RuntimePersistenceError("runtime writer returned an invalid tool call")
        return result

    async def _submit(
        self,
        payload: PhaseCommit | RuntimeEventCommand | RuntimeTerminalCommit | ToolCommit,
    ) -> RuntimeCheckpoint | RuntimeEvent | RuntimeRun | ToolCallRecord:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[
            RuntimeCheckpoint | RuntimeEvent | RuntimeRun | ToolCallRecord
        ]
        future = loop.create_future()
        # 检查与入队共用状态锁，保证 shutdown 的 sentinel 不会越过已接受命令。
        async with self._state_lock:
            if not self._accepting or self._consumer is None:
                raise RuntimePersistenceError("runtime writer is not accepting results")
            await self._queue.put(WriterCommand(payload=payload, future=future))
        return await future

    async def shutdown(self) -> None:
        async with self._state_lock:
            consumer = self._consumer
            if consumer is None:
                return
            if self._accepting:
                self._accepting = False
                await self._queue.put(None)
        await consumer
        self._consumer = None

    async def _consume(self) -> None:
        while True:
            command = await self._queue.get()
            try:
                if command is None:
                    return
                try:
                    if isinstance(command.payload, PhaseCommit):
                        result = self._store.commit_phase(command.payload)
                    elif isinstance(command.payload, RuntimeTerminalCommit):
                        result = self._store.commit_terminal(command.payload)
                    elif isinstance(command.payload, ToolCommit):
                        result = self._store.commit_tool(command.payload)
                    else:
                        result = self._store.append_event_command(command.payload)
                except Exception as exc:
                    if not command.future.done():
                        command.future.set_exception(exc)
                else:
                    if not command.future.done():
                        command.future.set_result(result)
            finally:
                self._queue.task_done()
