import asyncio
import json
from datetime import UTC, datetime
from threading import Event, Lock

import pytest

from backend.diagnosis.adaptive_tools import AdaptiveToolSession
from backend.domain.agent_findings import AgentName
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceProvider
from backend.domain.tool_calls import ToolCallRecord, ToolCallStatus, ToolSpec
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderResult, ProviderStatus
from backend.runtime.concurrency import RunStepGate
from backend.tools.registry import ToolInvocationResult, ToolRegistry


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_one_run_gate_bounds_mixed_steps_and_isolates_sibling_failure() -> None:
    gate = RunStepGate(2)
    lock = asyncio.Lock()
    release = asyncio.Event()
    active = 0
    peak = 0
    completed = []

    async def step(kind: str, *, fails: bool = False) -> None:
        nonlocal active, peak
        async with gate.slot():
            async with lock:
                active += 1
                peak = max(peak, active)
            await release.wait()
            async with lock:
                active -= 1
            if fails:
                raise RuntimeError(kind)
            completed.append(kind)

    tasks = [
        asyncio.create_task(step("provider")),
        asyncio.create_task(step("specialist", fails=True)),
        asyncio.create_task(step("adaptive-tool")),
        asyncio.create_task(step("conflict-review")),
    ]
    await asyncio.sleep(0)
    assert peak == 2
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert peak == 2
    assert sorted(completed) == ["adaptive-tool", "conflict-review", "provider"]
    assert sum(isinstance(result, RuntimeError) for result in results) == 1


@pytest.mark.anyio
async def test_run_gate_is_reentrant_for_nested_specialist_tool_call() -> None:
    gate = RunStepGate(1)

    async with gate.slot():
        async with gate.slot():
            await asyncio.sleep(0)


@pytest.mark.anyio
async def test_provider_and_adaptive_tool_share_one_real_run_gate() -> None:
    gate = RunStepGate(2)
    lock = Lock()
    release = Event()
    saturated = Event()
    active = 0
    peak = 0

    def bounded_work() -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                saturated.set()
        assert release.wait(5)
        with lock:
            active -= 1

    class BlockingProvider:
        def __init__(self, provider):
            self.provider = provider

        def collect(self, _event):
            bounded_work()
            return ProviderResult(provider=self.provider, status=ProviderStatus.SUCCESS)

    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service="checkout",
        environment="prod",
        severity=Severity.CRITICAL,
        title="errors",
        description="errors",
        started_at=datetime(2026, 7, 17, tzinfo=UTC),
    )
    providers = ProviderRegistry(
        [
            BlockingProvider(EvidenceProvider.LOG),
            BlockingProvider(EvidenceProvider.METRIC),
        ]
    )
    tools = ToolRegistry()

    def read_logs(**kwargs):
        bounded_work()
        return ToolInvocationResult(
            call=ToolCallRecord(
                task_id=kwargs["task_id"],
                agent_name=kwargs["agent_name"],
                tool_name=kwargs["tool_name"],
                input=kwargs["input"],
                status=ToolCallStatus.SUCCESS,
            ),
            evidence=[],
            provider_results=[],
        )

    tools.register(
        ToolSpec(name="read_logs", description="Read logs", read_only=True),
        read_logs,
    )
    session = AdaptiveToolSession(
        event=event,
        seed_evidence=[],
        registry=tools,
        task_ids={AgentName.LOG: "task-log"},
        parallel_limit=gate,
    )
    payload = json.dumps(
        {
            "start_time": "2026-07-16T23:50:00Z",
            "end_time": "2026-07-17T00:10:00Z",
            "reason": "test",
        }
    )
    provider_task = asyncio.create_task(
        providers.collect_results_async(event, parallel_limit=gate)
    )
    tool_task = asyncio.create_task(
        session.invoke(AgentName.LOG, "read_logs", payload, 1)
    )

    assert await asyncio.to_thread(saturated.wait, 5)
    assert peak == 2
    release.set()
    await asyncio.gather(provider_task, tool_task)
    assert peak == 2
