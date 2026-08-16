import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimePhase,
    RuntimeRunStatus,
)
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.phases import (
    BusinessMutation,
    PhaseOutput,
    durable_model_reservations,
    durable_token_usage,
)
from backend.runtime.store import InMemoryRuntimeStore
from backend.runtime.writer import RuntimeWriter
from tests.runtime.test_v11_isolation_red import _contract, _v11_run


class V11PhaseExecutor:
    """按 V11 profile 提交最小合法业务投影的确定性执行器。"""

    def __init__(self, repository, run_id: str, *, fail_investigation: bool = False) -> None:
        self.repository = repository
        self.run_id = run_id
        self.fail_investigation = fail_investigation
        self.phases: list[RuntimePhase] = []

    async def execute_phase(self, phase_input) -> PhaseOutput:
        self.phases.append(phase_input.phase)
        mutation = BusinessMutation(investigation_id="inv-1")
        if phase_input.phase == RuntimePhase.INTAKE:
            record = self.repository.get("inv-1")
            mutation = BusinessMutation(
                investigation_id="inv-1",
                investigation=record.model_copy(
                    update={"active_runtime_run_id": self.run_id}
                ),
                activate_projection=True,
            )
        elif phase_input.phase == RuntimePhase.FINALIZE and self.fail_investigation:
            # 模拟 V11 RESULT_VALIDATION 失败语义：业务投影在 FINALIZE 前收敛 failed。
            record = self.repository.get("inv-1")
            mutation = BusinessMutation(
                investigation_id="inv-1",
                investigation=record.model_copy(
                    update={
                        "status": InvestigationStatus.FAILED,
                        "failure_reason": "output_validation",
                    }
                ),
            )
        return PhaseOutput(
            business_mutation=mutation,
            status="completed",
            safe_payload={"status": "completed"},
            # fake 执行器不消耗工具/Token；V11 精确校验要求剩余预算与持久消耗一致。
            resume_state=phase_input.resume_state.model_copy(
                update={"remaining_tool_budget": 8, "remaining_token_budget": 1000}
            ),
        )


class BlockingV11PhaseExecutor(V11PhaseExecutor):
    """在指定 phase 前阻塞，便于在受控时点推进 deadline 或请求取消。"""

    def __init__(self, repository, run_id: str, *, block_phase: RuntimePhase) -> None:
        super().__init__(repository, run_id)
        self.block_phase = block_phase
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute_phase(self, phase_input) -> PhaseOutput:
        if phase_input.phase == self.block_phase:
            self.started.set()
            await self.release.wait()
        return await super().execute_phase(phase_input)


class InvestigatorFailureV11PhaseExecutor(V11PhaseExecutor):
    async def execute_phase(self, phase_input) -> PhaseOutput:
        if phase_input.phase == RuntimePhase.INVESTIGATOR_ROUND_1:
            self.phases.append(phase_input.phase)
            record = self.repository.get("inv-1")
            mutation = BusinessMutation(
                investigation_id="inv-1",
                investigation=record.model_copy(
                    update={
                        "status": InvestigationStatus.FAILED,
                        "failure_reason": "all investigators failed",
                    }
                ),
            )
            return PhaseOutput(
                business_mutation=mutation,
                status="completed",
                safe_payload={"status": "failed"},
                resume_state=phase_input.resume_state.model_copy(
                    update={"remaining_tool_budget": 8, "remaining_token_budget": 1000}
                ),
            )
        return await super().execute_phase(phase_input)


class DeadlineRecordingExecutor(V11PhaseExecutor):
    """在 INTAKE 提交前快照协调器缓存的绝对 deadline（finally 会弹出该缓存）。"""

    def __init__(self, repository, run_id: str, coordinator) -> None:
        super().__init__(repository, run_id)
        self.coordinator = coordinator
        self.observed_deadline: float | None = None

    async def execute_phase(self, phase_input) -> PhaseOutput:
        if phase_input.phase == RuntimePhase.INTAKE:
            self.observed_deadline = self.coordinator._deadlines.get(self.run_id)
        return await super().execute_phase(phase_input)


def _v11_services(run_id: str = "run-v11-deadline", **run_updates):
    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-1",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="v11 deadline",
                description="v11 deadline budget",
                started_at=datetime(2026, 8, 6, tzinfo=UTC),
            ),
        )
    )
    store = InMemoryRuntimeStore(repository, lease_seconds=30)
    run = store.create_run(_v11_run(run_id=run_id, **run_updates))
    executor = V11PhaseExecutor(repository, run.id)
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=executor,
        heartbeat_seconds=1,
    )
    return repository, store, run, executor, coordinator


@pytest.mark.anyio
async def test_v11_deadline_exceeded_fails_run_with_timeout_category() -> None:
    _repository, store, run, executor, coordinator = _v11_services()
    blocking = BlockingV11PhaseExecutor(
        executor.repository, run.id, block_phase=RuntimePhase.INTAKE
    )
    coordinator.phase_executor = blocking

    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    await blocking.started.wait()
    # 绝对 deadline 到期：把缓存 deadline 推到过去，等价于 monotonic 越过 deadline。
    coordinator._deadlines[run.id] = time.monotonic() - 1
    blocking.release.set()

    result = await execution

    assert result.status == RuntimeRunStatus.FAILED
    assert result.failure_category == RuntimeFailureCategory.TIMEOUT
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.FAILED
    assert store.list_checkpoints(run.id) == []
    timeout_events = [
        event
        for event in store.list_events(run.id)
        if event.safe_payload.get("failure_category") == "timeout"
    ]
    assert timeout_events
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_v11_cancelling_run_with_expired_deadline_converges_terminal() -> None:
    _repository, store, run, executor, coordinator = _v11_services(
        run_id="run-v11-cancel-deadline"
    )
    blocking = BlockingV11PhaseExecutor(
        executor.repository, run.id, block_phase=RuntimePhase.INTAKE
    )
    coordinator.phase_executor = blocking

    execution = asyncio.create_task(coordinator.execute(run.id, owner="worker-a"))
    await blocking.started.wait()
    await coordinator.request_cancel(run.id)
    assert store.get_run(run.id).status == RuntimeRunStatus.CANCELLING
    coordinator._deadlines[run.id] = time.monotonic() - 1
    blocking.release.set()

    result = await execution

    # CANCELLING 遇到 deadline 必须收敛终态，不得滞留直至 lease 过期。
    assert result.status == RuntimeRunStatus.CANCELLED
    assert store.get_run(run.id).status == RuntimeRunStatus.CANCELLED
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.CANCELLED
    await coordinator.shutdown()


def test_deadline_for_uses_absolute_start_and_never_extends() -> None:
    past = datetime.now(UTC) - timedelta(seconds=100)
    run = _v11_run(
        run_id="run-deadline-absolute",
        created_at=past,
        started_at=past,
    )

    deadline = RuntimeCoordinator._deadline_for(run)
    remaining = deadline - time.monotonic()

    # 剩余窗口 = timeout(120) - elapsed(100) ≈ 20s，绝不重置为完整 120s。
    assert 0 <= remaining <= 21
    later = RuntimeCoordinator._deadline_for(run)
    assert later <= deadline + 0.001


@pytest.mark.anyio
async def test_v11_resume_keeps_absolute_deadline_across_attempts() -> None:
    past = datetime.now(UTC) - timedelta(seconds=100)
    repository, store, run, executor, coordinator = _v11_services(
        run_id="run-v11-resume-deadline",
        created_at=past,
        started_at=past,
    )
    leased, _attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    # 模拟进程崩溃：lease 过期审计把首个 Attempt 收敛为 INTERRUPTED。
    store.audit_expired_leases(datetime.now(UTC) + timedelta(seconds=31))
    assert store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
    recording = DeadlineRecordingExecutor(repository, run.id, coordinator)
    coordinator.phase_executor = recording

    result = await coordinator.resume(run.id, owner="worker-b")

    assert result.status == RuntimeRunStatus.COMPLETED
    assert recording.observed_deadline is not None
    # resume 后的 deadline 仍锚定最初 started_at（≈ monotonic+20），未重置为 +120。
    assert recording.observed_deadline - time.monotonic() <= 21
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_v11_failed_investigation_after_phase_loop_fails_run_with_output_validation() -> None:
    repository, store, run, _executor, coordinator = _v11_services(
        run_id="run-v11-output-validation"
    )
    coordinator.phase_executor = V11PhaseExecutor(
        repository, run.id, fail_investigation=True
    )

    result = await coordinator.execute(run.id, owner="worker-a")

    assert result.status == RuntimeRunStatus.FAILED
    assert result.failure_category == RuntimeFailureCategory.OUTPUT_VALIDATION
    assert store.list_attempts(run.id)[0].status == RuntimeAttemptStatus.FAILED
    failure_events = [
        event
        for event in store.list_events(run.id)
        if event.safe_payload.get("failure_category") == "output_validation"
    ]
    assert failure_events
    await coordinator.shutdown()


@pytest.mark.anyio
async def test_v11_required_investigator_failure_stops_before_critic_or_lead() -> None:
    repository, store, run, executor, coordinator = _v11_services(
        run_id="run-v11-investigator-terminal"
    )
    failing = InvestigatorFailureV11PhaseExecutor(repository, run.id)
    coordinator.phase_executor = failing

    result = await coordinator.execute(run.id, owner="worker-a")

    assert result.status == RuntimeRunStatus.FAILED
    assert failing.phases == [
        RuntimePhase.INTAKE,
        RuntimePhase.EVIDENCE_COLLECTION,
        RuntimePhase.LEAD_PLANNING,
        RuntimePhase.INVESTIGATOR_ROUND_1,
    ]
    assert RuntimePhase.CRITIC_REVIEW not in failing.phases
    assert RuntimePhase.LEAD_ADJUDICATION not in failing.phases
    assert repository.get("inv-1").multi_agent_run is None
    await coordinator.shutdown()


def test_v11_run_rejects_timeout_seconds_above_hard_deadline() -> None:
    # 运营噪声链：V11 run deadline 上限 120→300（domain 常量
    # V11_RUN_DEADLINE_MAX_SECONDS；产品/server 创建路径仍在 container 层
    # clamp 到 120）。300 合法，301 仍 fail-closed。
    run = _v11_run(
        run_id="run-v11-timeout-authorized-300",
        timeout_seconds=300,
        execution_contract={**_contract(), "timeout_seconds": 300.0},
    )
    assert run.timeout_seconds == 300

    with pytest.raises(ValidationError, match="hard deadline"):
        _v11_run(
            run_id="run-v11-timeout-overflow",
            timeout_seconds=301,
            execution_contract={**_contract(), "timeout_seconds": 301.0},
        )


def test_v11_model_turn_budget_is_durable_across_invocations_and_exhaustion() -> None:
    _repository, store, run, _executor, _coordinator = _v11_services(
        run_id="run-v11-model-turn-budget"
    )
    leased, _attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )

    assert leased.remaining_model_turns == 8
    assert store.reserve_model_turn(
        run.id, owner="worker-a", lease_version=leased.lease_version
    ) == 7
    assert store.get_run(run.id).remaining_model_turns == 7
    # A fresh invocation must consume the same persisted run budget, not reset to 8.
    assert store.reserve_model_turn(
        run.id, owner="worker-a", lease_version=leased.lease_version
    ) == 6
    assert store.get_run(run.id).remaining_model_turns == 6


def test_v11_started_model_reservation_is_durable_for_resume_reconciliation() -> None:
    started = RuntimeEvent(
        run_id="run-token-reservation",
        attempt_id="attempt-1",
        sequence=1,
        event_type=RuntimeEventType.MODEL_STARTED,
        actor_type=RuntimeActorType.MODEL,
        execution_id="execution-1",
        safe_payload={
            "status": "started",
            "input_tokens": 0,
            "output_tokens": 0,
            "logical_call_id": "logical-call-1",
            "reservation_id": "reservation-1",
            "reservation_status": "reserved",
            "reserved_tokens": 100,
            "input_estimate": 10,
        },
    )

    assert durable_token_usage([started], "run-token-reservation") == 0
    assert durable_model_reservations([started], "run-token-reservation") == {
        "reservation-1": {
            "logical_call_id": "logical-call-1",
            "reserved_tokens": 100,
            "input_estimate": 10,
            "attempt_id": "attempt-1",
            "execution_id": "execution-1",
        }
    }


def test_v11_rejected_model_response_durably_exhausts_token_budget() -> None:
    _repository, store, run, _executor, coordinator = _v11_services(
        run_id="run-token-budget-rejected"
    )
    _leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    store._append_event_for_test(
        RuntimeEvent(
            run_id=run.id,
            attempt_id=attempt.id,
            sequence=1,
            event_type=RuntimeEventType.MODEL_FAILED,
            actor_type=RuntimeActorType.MODEL,
            execution_id="execution-rejected",
            safe_payload={
                "status": "failed",
                "input_tokens": 80,
                "output_tokens": 30,
                "logical_call_id": "logical-call-rejected",
                "reservation_id": "reservation-rejected",
                "reservation_status": "rejected",
                "reserved_tokens": 90,
                "input_estimate": 10,
                "budget_overrun_tokens": 20,
            },
        )
    )

    recovery_state = coordinator._effective_resume_state(run, checkpoint=None)

    assert recovery_state.remaining_token_budget == 0


@pytest.mark.anyio
async def test_v11_resume_releases_crash_window_reservation_once() -> None:
    repository, store, run, _executor, coordinator = _v11_services(
        run_id="run-token-reservation-resume"
    )
    leased, attempt = store.acquire_lease_and_create_attempt(
        run.id,
        attempt=RuntimeAttempt(
            run_id=run.id,
            attempt_number=1,
            status=RuntimeAttemptStatus.RUNNING,
        ),
        owner="worker-a",
        expected_status=RuntimeRunStatus.CREATED,
    )
    await coordinator.writer.start()
    await coordinator._persist_model_event(
        leased,
        attempt,
        "worker-a",
        RuntimePhase.LEAD_PLANNING,
        "execution-crash-window",
        "started",
        actor_name="LeadAgent",
        safe_payload={
            "logical_call_id": "logical-crash-window",
            "reservation_id": "reservation-crash-window",
            "reservation_status": "reserved",
            "reserved_tokens": 100,
            "input_estimate": 10,
            "attempt": 1,
        },
    )
    assert durable_model_reservations(store.list_events(run.id), run.id)

    store.audit_expired_leases(datetime.now(UTC) + timedelta(seconds=31))
    result = await coordinator.resume(run.id, owner="worker-b")

    assert result.status == RuntimeRunStatus.COMPLETED
    events = store.list_events(run.id)
    releases = [
        event
        for event in events
        if event.safe_payload.get("reservation_id") == "reservation-crash-window"
        and event.safe_payload.get("reservation_status") == "released"
    ]
    assert len(releases) == 1
    assert durable_model_reservations(events, run.id) == {}
    await coordinator.shutdown()
