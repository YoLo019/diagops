from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from backend.domain.runtime import RuntimeRunStatus
from backend.runtime.store import RuntimeConflict


class RuntimeManager:
    """进程内 Run 调度器；Phase 内并发由 DiagnosisPhaseExecutor 负责。"""

    def __init__(
        self,
        *,
        coordinator_factory: Callable[[str], Any],
        max_concurrent_runs: int = 4,
        shutdown_timeout_seconds: float = 10,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("runtime concurrency limit must be positive")
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        self._coordinator_factory = coordinator_factory
        self._run_limit = asyncio.Semaphore(max_concurrent_runs)
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._coordinators: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._accepting = True

    async def startup(self) -> None:
        """允许 lifespan 重启调度器；不会自动恢复任何历史 Run。"""
        async with self._lock:
            self._accepting = True

    async def start(self, run_id: str) -> asyncio.Task[Any]:
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("runtime manager is shutting down")
            existing = self._tasks.get(run_id)
            if existing is not None and not existing.done():
                return existing
            coordinator = self._coordinator_factory(run_id)
            self._coordinators[run_id] = coordinator
            task = asyncio.create_task(
                self._run(run_id, coordinator), name=f"runtime-run-{run_id}"
            )
            task.add_done_callback(self._observe_task)
            self._tasks[run_id] = task
            return task

    async def resume(self, run_id: str) -> asyncio.Task[Any]:
        async with self._lock:
            if not self._accepting:
                raise RuntimeError("runtime manager is shutting down")
            existing = self._tasks.get(run_id)
            if existing is not None and not existing.done():
                return existing
            coordinator = self._coordinator_factory(run_id)
            store = getattr(coordinator, "store", None)
            if store is not None:
                run = store.get_run(run_id)
                if (
                    run.status == RuntimeRunStatus.INTERRUPTED
                    and not run.is_v11
                ):
                    attempts = store.list_attempts(run_id)
                    if not attempts:
                        raise RuntimeConflict(
                            "legacy recovery rejected; create an explicit V11 live rerun"
                        )
                    store.append_recovery_rejection(
                        run_id,
                        attempt_id=attempts[-1].id,
                        checkpoint_id=run.latest_checkpoint_id,
                        message="create an explicit V11 live rerun",
                    )
                    raise RuntimeConflict(
                        "legacy recovery rejected; create an explicit V11 live rerun"
                    )
            self._coordinators[run_id] = coordinator
            task = asyncio.create_task(
                self._run_resume(run_id, coordinator),
                name=f"runtime-resume-{run_id}",
            )
            task.add_done_callback(self._observe_task)
            self._tasks[run_id] = task
            return task

    async def cancel(self, run_id: str):
        async with self._lock:
            coordinator = self._coordinators.get(run_id)
            if coordinator is None:
                coordinator = self._coordinator_factory(run_id)
        return await coordinator.request_cancel(run_id)

    async def shutdown(self) -> None:
        async with self._lock:
            if not self._accepting and not self._tasks:
                return
            self._accepting = False
            tasks = list(self._tasks.items())
            coordinators = dict(self._coordinators)
        pending = [task for _run_id, task in tasks if not task.done()]
        if pending:
            _done, remaining = await asyncio.wait(
                pending, timeout=self._shutdown_timeout_seconds
            )
            for task in remaining:
                task.cancel()
            if remaining:
                await asyncio.gather(*remaining, return_exceptions=True)
        for coordinator in coordinators.values():
            await coordinator.shutdown()
        async with self._lock:
            self._tasks.clear()
            self._coordinators.clear()

    async def _run(self, run_id: str, coordinator):
        owner = f"runtime-{uuid4().hex}"
        try:
            async with self._run_limit:
                return await coordinator.execute(run_id, owner=owner)
        finally:
            await self._forget(run_id)

    async def _run_resume(self, run_id: str, coordinator):
        owner = f"runtime-{uuid4().hex}"
        try:
            async with self._run_limit:
                return await coordinator.resume(run_id, owner=owner)
        finally:
            await self._forget(run_id)

    async def _forget(self, run_id: str) -> None:
        task = asyncio.current_task()
        async with self._lock:
            if self._tasks.get(run_id) is not task:
                return
            self._tasks.pop(run_id, None)
            self._coordinators.pop(run_id, None)

    @staticmethod
    def _observe_task(task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
