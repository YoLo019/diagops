import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from backend.config.settings import AgentsSettings, AppSettings, RuntimeSettings, StorageSettings
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.runtime import RuntimeAttempt, RuntimeRunStatus
from backend.runtime.store import RuntimeContractError, RuntimePersistenceError
from backend.services.container import AppContainer
from tests.reports.test_v11_product_integration import _event


@pytest.mark.anyio
async def test_fast_restart_reaudits_expired_lease_without_automatic_resume(tmp_path, monkeypatch):
    settings = AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'restart.db'}"),
        agents=AgentsSettings(enabled=True, model="offline-test"),
        runtime=RuntimeSettings(heartbeat_seconds=1),
    )
    previous = AppContainer(settings)
    record = previous.repository.save(InvestigationRecord(event=_event()))
    run = previous.create_runtime_run(
        record.id, strategy="adaptive", run_reason="initial", execution_contract_version="v11",
    )
    leased, _ = previous.runtime_store.acquire_lease_and_create_attempt(
        run.id, attempt=RuntimeAttempt(run_id=run.id, attempt_number=1, status="running"),
        owner="dead-process", expected_status=RuntimeRunStatus.CREATED,
    )
    previous.close()
    container = AppContainer(settings)
    audit = container.runtime_store.audit_expired_leases
    recovered = asyncio.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    def clocked_audit(now):
        nonlocal calls
        calls += 1
        if calls == 1:
            return audit(now)
        if calls == 2:
            raise RuntimePersistenceError("transient audit failure")
        changed = audit(leased.lease_expires_at + timedelta(seconds=1))
        if changed:
            loop.call_soon_threadsafe(recovered.set)
        return changed

    monkeypatch.setattr(container.runtime_store, "audit_expired_leases", clocked_audit)
    try:
        await container.startup()
        audit_task = container._lease_audit_task
        assert container.runtime_store.get_run(run.id).status == RuntimeRunStatus.RUNNING
        await asyncio.wait_for(recovered.wait(), timeout=5)
        assert container.runtime_store.get_run(run.id).status == RuntimeRunStatus.INTERRUPTED
        assert len(container.runtime_store.list_attempts(run.id)) == 1
        assert not container.runtime_manager._tasks

        coordinator = container._build_runtime_coordinator(run.id)
        execute = AsyncMock(return_value=container.runtime_store.get_run(run.id))
        monkeypatch.setattr(coordinator, "_execute_owned", execute)
        await coordinator.resume(run.id, owner="manual-resume")
        execute.assert_awaited_once()
        assert len(container.runtime_store.list_attempts(run.id)) == 2
    finally:
        await container.shutdown()
    assert audit_task.done()
    assert container.engine is None


@pytest.mark.anyio
@pytest.mark.parametrize("sqlite", [False, True])
async def test_admission_failure_does_not_leave_pending_investigation(tmp_path, sqlite):
    container = AppContainer(AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'admission.db'}" if sqlite else "memory://"),
        agents=AgentsSettings(enabled=True, provider="deepseek", model="offline-test"),
    ))
    try:
        with pytest.raises(RuntimeContractError, match="certified openai_compatible"):
            await container.run_investigation(_event(), execution_contract_version="v11")
        record, = container.repository.list()
        assert record.status == InvestigationStatus.FAILED
        assert record.failure_reason == "runtime run creation failed"
        assert container.runtime_store.list_runs(record.id) == []

        before = record.model_dump(mode="json")
        with pytest.raises(RuntimeContractError, match="certified openai_compatible"):
            container.create_runtime_run(
                record.id, strategy="adaptive", run_reason="initial",
                execution_contract_version="v11",
            )
        assert len(container.repository.list()) == 1
        assert container.repository.get(record.id).model_dump(mode="json") == before
    finally:
        await container.shutdown()


@pytest.mark.anyio
async def test_failed_rerun_creation_marks_only_new_projection_failed(monkeypatch):
    container = AppContainer(AppSettings(
        storage=StorageSettings(url="memory://"),
        agents=AgentsSettings(enabled=True, model="offline-test"),
    ))
    original = container.repository.save(InvestigationRecord(
        event=_event(), status=InvestigationStatus.COMPLETED,
    ))

    def reject(_run):
        raise RuntimePersistenceError("unsafe provider or database message")

    monkeypatch.setattr(container.runtime_store, "create_run", reject)
    try:
        with pytest.raises(RuntimePersistenceError):
            container.create_runtime_run(
                original.id, strategy="adaptive", run_reason="initial",
                execution_contract_version="v11",
            )
        records = container.repository.list()
        assert len(records) == 2
        assert container.repository.get(original.id) == original
        linked = next(item for item in records if item.id != original.id)
        assert linked.status == InvestigationStatus.FAILED
        assert linked.source_investigation_id == original.id
        assert linked.failure_reason == "runtime run creation failed"
    finally:
        await container.shutdown()
