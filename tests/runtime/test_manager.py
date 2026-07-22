import asyncio

import pytest

from backend.runtime.manager import RuntimeManager


class FakeCoordinator:
    def __init__(self, tracker, *, fail: bool = False) -> None:
        self.tracker = tracker
        self.fail = fail
        self.cancelled = False

    async def execute(self, run_id: str, owner: str) -> None:
        del owner
        self.tracker["active"] += 1
        self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
        self.tracker["started"].append(run_id)
        self.tracker["ready"].set()
        await self.tracker["release"].wait()
        self.tracker["active"] -= 1
        if self.fail:
            raise RuntimeError("isolated failure")
        self.tracker["completed"].append(run_id)

    async def request_cancel(self, run_id: str) -> None:
        del run_id
        self.cancelled = True

    async def shutdown(self) -> None:
        return None


@pytest.mark.anyio
async def test_four_runs_overlap_fifth_waits_and_failure_is_isolated() -> None:
    tracker = {
        "active": 0,
        "peak": 0,
        "started": [],
        "completed": [],
        "ready": asyncio.Event(),
        "release": asyncio.Event(),
    }
    coordinators = {
        f"run-{index}": FakeCoordinator(tracker, fail=index == 2)
        for index in range(5)
    }
    manager = RuntimeManager(
        coordinator_factory=coordinators.__getitem__,
        max_concurrent_runs=4,
    )

    tasks = [await manager.start(run_id) for run_id in coordinators]
    while len(tracker["started"]) < 4:
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert tracker["peak"] == 4
    assert "run-4" not in tracker["started"]

    tracker["release"].set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(isinstance(item, RuntimeError) for item in results) == 1
    assert sorted(tracker["completed"]) == ["run-0", "run-1", "run-3", "run-4"]
    await manager.shutdown()


@pytest.mark.anyio
async def test_shutdown_stops_execution_without_requesting_business_cancel() -> None:
    tracker = {
        "active": 0,
        "peak": 0,
        "started": [],
        "completed": [],
        "ready": asyncio.Event(),
        "release": asyncio.Event(),
    }
    coordinator = FakeCoordinator(tracker)
    manager = RuntimeManager(
        coordinator_factory=lambda _run_id: coordinator,
        shutdown_timeout_seconds=0.01,
    )
    task = await manager.start("run-1")
    await tracker["ready"].wait()

    await manager.shutdown()

    assert coordinator.cancelled is False
    assert task.cancelled()


@pytest.mark.anyio
async def test_completed_task_and_coordinator_are_reclaimed_immediately() -> None:
    tracker = {
        "active": 0,
        "peak": 0,
        "started": [],
        "completed": [],
        "ready": asyncio.Event(),
        "release": asyncio.Event(),
    }
    tracker["release"].set()
    manager = RuntimeManager(
        coordinator_factory=lambda _run_id: FakeCoordinator(tracker),
    )

    task = await manager.start("run-finished")
    await task
    await asyncio.sleep(0)

    assert "run-finished" not in manager._tasks
    assert "run-finished" not in manager._coordinators
    await manager.shutdown()


def test_runtime_manager_does_not_expose_duplicate_step_scheduler() -> None:
    manager = RuntimeManager(
        coordinator_factory=lambda _run_id: FakeCoordinator({}),
        max_concurrent_runs=4,
    )

    assert not hasattr(manager, "run_parallel_steps")
    assert not hasattr(manager, "_step_limits")
    assert not hasattr(manager, "fault_injector")
