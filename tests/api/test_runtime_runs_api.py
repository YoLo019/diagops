import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api.runtime_runs import format_sse_event
from backend.config.settings import (
    AgentsSettings,
    AppSettings,
    BenchmarkSettings,
    StorageSettings,
)
from backend.db.models import InvestigationRecord
from backend.db.schema import runtime_attempts, runtime_events
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import ExecutionContractVersion, InvestigationStrategy
from backend.domain.runtime import (
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.main import app
from backend.services.container import get_container, reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


def _record(investigation_id: str) -> InvestigationRecord:
    return InvestigationRecord(
        id=investigation_id,
        event=IncidentEvent(
            source=IncidentSource.MANUAL,
            service="checkout-service",
            environment="prod",
            severity=Severity.WARNING,
            title="runtime api",
            description="runtime api contract",
            started_at=datetime(2026, 7, 18, tzinfo=UTC),
        ),
    )


def _stored_run(
    investigation_id: str,
    *,
    run_id: str,
    status: RuntimeRunStatus = RuntimeRunStatus.COMPLETED,
) -> RuntimeRun:
    container = get_container()
    container.repository.save(_record(investigation_id))
    return container.runtime_store.create_run(
        RuntimeRun(
            id=run_id,
            investigation_id=investigation_id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=status,
            run_reason=RuntimeRunReason.INITIAL,
        )
    )


def test_create_runtime_run_returns_202_and_frozen_metadata() -> None:
    container = get_container()
    container.repository.save(_record("inv-api"))
    parent = _stored_run("inv-api", run_id="run-api-parent")

    with TestClient(app) as client:
        response = client.post(
            "/investigations/inv-api/runtime-runs",
            json={
                "strategy": "adaptive",
                "run_reason": "manual_rerun",
                "parent_run_id": parent.id,
                "model_provider": "openai",
                "model_name": "gpt-test",
                "prompt_version": "v9-safe",
            },
        )

    assert response.status_code == 202
    body = response.json()
    assert body["investigation_id"] == "inv-api"
    assert body["strategy"] == "adaptive"
    assert body["model_provider"] == "openai"
    assert body["model_name"] == "gpt-test"
    assert body["prompt_version"] == "v9-safe"


def test_container_freezes_effective_agent_configuration_and_budgets() -> None:
    container = reset_container(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(
                enabled=True,
                model="gpt-4.1-mini",
                max_total_tool_calls=7,
                timeout_seconds=23,
            ),
        )
    )
    container.repository.save(_record("inv-frozen-config"))

    run = container.create_runtime_run(
        "inv-frozen-config",
        strategy=InvestigationStrategy.ADAPTIVE,
        run_reason=RuntimeRunReason.INITIAL,
    )

    assert run.model_provider.value == "openai"
    assert run.model_name == "gpt-4.1-mini"
    assert run.prompt_version == "v9"
    assert run.tool_budget == 7
    assert run.token_budget is None
    assert run.timeout_seconds == 23


@pytest.mark.parametrize(
    "payload",
    [
        {"strategy": "fixed", "run_reason": "replay"},
        {
            "strategy": "fixed",
            "run_reason": "initial",
            "model_name": "sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        },
        {
            "strategy": "fixed",
            "run_reason": "initial",
            "prompt_version": "secret=benchmark-token",
        },
    ],
)
def test_create_api_rejects_illegal_or_sensitive_runtime_metadata(payload) -> None:
    container = get_container()
    container.repository.save(_record("inv-api-invalid"))

    with TestClient(app) as client:
        response = client.post(
            "/investigations/inv-api-invalid/runtime-runs",
            json=payload,
        )

    assert response.status_code == 422


def test_history_detail_and_event_pagination_are_scoped() -> None:
    _stored_run("inv-history", run_id="run-history")

    with TestClient(app) as client:
        history = client.get("/investigations/inv-history/runtime-runs")
        detail = client.get("/runtime-runs/run-history")
        events = client.get("/runtime-runs/run-history/events?after=2&limit=5")

    assert history.status_code == 200
    assert [item["id"] for item in history.json()] == ["run-history"]
    assert detail.status_code == 200
    assert detail.json()["id"] == "run-history"
    assert detail.json()["attempts"] == []
    assert detail.json()["checkpoints"] == []
    assert events.status_code == 200
    assert events.json() == []


def test_runtime_routes_map_not_found_conflict_and_invalid_pagination() -> None:
    _stored_run("inv-terminal", run_id="run-terminal")

    with TestClient(app) as client:
        missing_investigation = client.post(
            "/investigations/inv-missing/runtime-runs",
            json={"strategy": "fixed", "run_reason": "initial"},
        )
        missing_run = client.get("/runtime-runs/run-missing")
        cancel_terminal = client.post("/runtime-runs/run-terminal/cancel")
        resume_terminal = client.post("/runtime-runs/run-terminal/resume")
        invalid_page = client.get(
            "/runtime-runs/run-terminal/events?after=-1&limit=0"
        )

    assert missing_investigation.status_code == 404
    assert missing_run.status_code == 404
    assert cancel_terminal.status_code == 409
    assert resume_terminal.status_code == 409
    assert invalid_page.status_code == 422


def test_parent_and_diff_relationships_reject_other_investigations() -> None:
    _stored_run("inv-left", run_id="run-left")
    _stored_run("inv-right", run_id="run-right")

    with TestClient(app) as client:
        bad_parent = client.post(
            "/investigations/inv-left/runtime-runs",
            json={
                "strategy": "fixed",
                "run_reason": "additional_evidence",
                "parent_run_id": "run-right",
            },
        )
        bad_diff = client.get(
            "/runtime-runs/run-left/diff?against_run_id=run-right"
        )

    assert bad_parent.status_code == 409
    assert bad_diff.status_code == 409


def test_replay_and_diff_requests_are_routed_to_runtime_services(monkeypatch) -> None:
    _stored_run("inv-routing", run_id="run-routing")
    container = get_container()
    calls: list[tuple[str, str]] = []

    def replay(run_id: str):
        calls.append(("replay", run_id))
        return {"replay_run_id": "replay-1", "source_run_id": run_id, "valid": True}

    def diff(run_id: str, against_run_id: str):
        calls.append((run_id, against_run_id))
        return {"run_id": run_id, "against_run_id": against_run_id, "sections": {}}

    monkeypatch.setattr(container, "replay_run", replay)
    monkeypatch.setattr(container, "diff_runs", diff)

    with TestClient(app) as client:
        replay_response = client.post("/runtime-runs/run-routing/replay")
        diff_response = client.get(
            "/runtime-runs/run-routing/diff?against_run_id=run-routing"
        )

    assert replay_response.status_code == 200
    assert diff_response.status_code == 200
    assert calls == [("replay", "run-routing"), ("run-routing", "run-routing")]


def test_runtime_disabled_returns_503_for_additive_routes() -> None:
    container = reset_container()
    container.settings.runtime.enabled = False
    container.repository.save(_record("inv-disabled"))

    with TestClient(app) as client:
        response = client.get("/investigations/inv-disabled/runtime-runs")

    assert response.status_code == 503


def test_openrca_run_is_replayable_across_container_instances(
    tmp_path: Path,
) -> None:
    settings = AppSettings(
        storage=StorageSettings(url=f"sqlite:///{tmp_path / 'runtime.db'}"),
        benchmark=BenchmarkSettings(results_path=tmp_path / "results"),
    )
    first = reset_container(settings)
    record = _record("inv-openrca").model_copy(
        update={"event": _record("unused").event.model_copy(update={"environment": "openrca"})}
    )
    first.repository.save(record)
    source = first.runtime_store.create_run(
        RuntimeRun(
            id="run-openrca-persisted",
            investigation_id=record.id,
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=RuntimeRunStatus.COMPLETED,
            run_reason=RuntimeRunReason.INITIAL,
            completed_at=datetime.now(UTC),
        )
    )
    run_dir = settings.benchmark.results_path / "run-frozen"
    run_dir.mkdir(parents=True)
    with (run_dir / "fixed-predictions.csv").open(
        "w", encoding="utf-8", newline=""
    ) as file:
        writer = csv.DictWriter(file, fieldnames=("prediction", "metadata"))
        writer.writeheader()
        writer.writerow(
            {
                "prediction": json.dumps(
                    {
                        "1": {
                            "root cause occurrence datetime": "2026-07-18 00:00:00",
                            "root cause component": "checkout-service",
                            "root cause reason": "deployment regression",
                        }
                    }
                ),
                "metadata": json.dumps({"runtime_run_id": source.id}),
            }
        )

    second = reset_container(settings)
    with TestClient(app) as client:
        response = client.post(f"/runtime-runs/{source.id}/replay")

    assert response.status_code == 200
    assert response.json()["benchmark_evaluation"] == "not_available"
    assert response.json()["valid"] is False
    assert "benchmark_artifact_missing" in response.json()["validation_errors"]
    assert second.runtime_store.get_run(source.id).status == RuntimeRunStatus.COMPLETED


def test_future_runtime_event_is_opaque_across_sqlite_api_sse_and_replay(
    tmp_path,
) -> None:
    container = reset_container(
        AppSettings(
            storage=StorageSettings(url=f"sqlite:///{tmp_path / 'future-event.db'}")
        )
    )
    container.repository.save(_record("inv-future-event"))
    run = container.runtime_store.create_run(
        RuntimeRun(
            id="run-future-event",
            investigation_id="inv-future-event",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.FIXED,
            status=RuntimeRunStatus.COMPLETED,
            run_reason=RuntimeRunReason.INITIAL,
            completed_at=datetime(2026, 7, 18, tzinfo=UTC),
        )
    )
    occurred_at = datetime(2026, 7, 18, tzinfo=UTC).isoformat()
    with container.runtime_store.engine.begin() as connection:
        connection.execute(
            runtime_attempts.insert().values(
                id="attempt-future-event",
                run_id=run.id,
                attempt_number=1,
                status="completed",
                started_at=occurred_at,
                completed_at=occurred_at,
            )
        )
        connection.execute(
            runtime_events.insert().values(
                id="event-future",
                run_id=run.id,
                attempt_id="attempt-future-event",
                sequence=1,
                event_type="agent.quantum_completed",
                phase="quantum_analysis",
                actor_type="quantum_worker",
                actor_name="FutureAgent",
                evidence_ids=[],
                safe_payload={
                    "status": "observed",
                    "metadata": {"release_channel": "canary"},
                },
                occurred_at=occurred_at,
                schema_version=2,
            )
        )

    with TestClient(app) as client:
        response = client.get(f"/runtime-runs/{run.id}/events")

    assert response.status_code == 200
    assert response.json()[0]["event_type"] == "agent.quantum_completed"
    assert response.json()[0]["safe_payload"]["metadata"] == {
        "release_channel": "canary"
    }
    event = container.runtime_store.list_events(run.id)[0]
    frame = format_sse_event(event)
    assert "event: runtime.opaque" in frame
    assert '"event_type":"agent.quantum_completed"' in frame
    replay = container.replay_run(run.id)
    assert replay.valid is False
    assert "unsupported_schema" in replay.validation_errors
    assert "unsupported_event_type" in replay.validation_errors


def test_app_lifespan_disposes_its_sqlite_engine(tmp_path: Path) -> None:
    container = reset_container(
        AppSettings(
            storage=StorageSettings(url=f"sqlite:///{tmp_path / 'lifespan.db'}")
        )
    )
    assert container.engine is not None

    with TestClient(app):
        pass

    assert container.engine is None


def test_v11_explicit_execution_tuple_mismatch_is_422_before_linked_run() -> None:
    container = get_container()
    container.repository.save(_record("inv-api-v11"))

    with TestClient(app) as client:
        response = client.post(
            "/investigations/inv-api-v11/runtime-runs",
            json={
                "strategy": "adaptive",
                "run_reason": "initial",
                "execution_contract_version": ExecutionContractVersion.V11.value,
                "model_name": "caller-selected-model",
            },
        )

    assert response.status_code == 422
    assert [item.id for item in container.repository.list()] == ["inv-api-v11"]


@pytest.mark.parametrize(
    "extra_field",
    [
        {"authority_mode": "agent"},
        {"endpoint": "https://caller-selected.example/v1"},
        {"base_url": "https://caller-selected.example/v1"},
        {"capability_artifact_hash": "ab" * 32},
    ],
)
def test_run_create_rejects_client_owned_execution_identity(extra_field) -> None:
    container = get_container()
    container.repository.save(_record("inv-api-owned"))

    with TestClient(app) as client:
        response = client.post(
            "/investigations/inv-api-owned/runtime-runs",
            json={
                "strategy": "adaptive",
                "run_reason": "initial",
                "execution_contract_version": ExecutionContractVersion.V11.value,
                **extra_field,
            },
        )

    assert response.status_code == 422
    assert [item.id for item in container.repository.list()] == ["inv-api-owned"]


def _compatible_container(monkeypatch, tmp_path: Path):
    from backend.config.settings import OpenAICompatibleSettings
    from backend.domain.multi_agent import ModelProvider
    from backend.services.container import AppContainer

    monkeypatch.setenv("DIAGOPS_AGENTS_API_KEY", "local-secret")
    container = reset_container(
        AppSettings(
            storage=StorageSettings(url="memory://"),
            agents=AgentsSettings(
                enabled=True,
                provider=ModelProvider.OPENAI_COMPATIBLE,
                model="compat-model",
                openai_compatible=OpenAICompatibleSettings(
                    base_url="http://127.0.0.1:8000/v1"
                ),
            ),
        )
    )
    monkeypatch.setattr(
        AppContainer, "_capability_directory", staticmethod(lambda: tmp_path)
    )
    return container


def test_v11_compatible_tuple_requires_certification(monkeypatch, tmp_path) -> None:
    container = _compatible_container(monkeypatch, tmp_path)
    container.repository.save(_record("inv-api-compat"))

    with TestClient(app) as client:
        response = client.post(
            "/investigations/inv-api-compat/runtime-runs",
            json={
                "strategy": "adaptive",
                "run_reason": "initial",
                "execution_contract_version": ExecutionContractVersion.V11.value,
            },
        )

    assert response.status_code == 422
    assert "capability" in response.json()["detail"]
    assert [item.id for item in container.repository.list()] == ["inv-api-compat"]


def test_v11_certified_compatible_tuple_freezes_endpoint_identity(
    monkeypatch, tmp_path
) -> None:
    from backend.config.settings import endpoint_id
    from backend.services.model_capability import (
        REQUIRED_CONTRACTS,
        ModelCapabilityArtifact,
        capability_manifest_hash,
        current_execution_environment,
        write_capability_artifact,
    )

    container = _compatible_container(monkeypatch, tmp_path)
    container.repository.save(_record("inv-api-certified"))
    identity = endpoint_id("http://127.0.0.1:8000/v1")
    artifact_path = write_capability_artifact(
        tmp_path,
        ModelCapabilityArtifact(
            provider="openai_compatible",
            model="compat-model",
            endpoint_id=identity,
            adapter_version="openai-compatible-adapter-v1",
            openai_sdk_version="1.0.0",
            agents_sdk_version="0.18.1",
            tested_parallelism=2,
            capability_manifest_hash=capability_manifest_hash(),
            required_contracts=REQUIRED_CONTRACTS,
            code_revision="a" * 40,
            source_manifest_hash="b" * 64,
            execution_environment=current_execution_environment(),
            tested_at=datetime(2026, 8, 7, 12, 0, tzinfo=UTC),
            result="passed",
            observations=[],
        ),
    )
    import json as json_module

    artifact_hash = json_module.loads(artifact_path.read_text())["artifact_hash"]

    run = container.create_runtime_run(
        "inv-api-certified",
        strategy=InvestigationStrategy.ADAPTIVE,
        run_reason=RuntimeRunReason.INITIAL,
        execution_contract_version=ExecutionContractVersion.V11,
    )

    assert run.execution_contract["endpoint_id"] == identity
    assert run.execution_contract["capability_artifact_hash"] == artifact_hash
    assert run.execution_contract["api_mode"] == "chat_completions"
    assert "127.0.0.1" not in json.dumps(run.execution_contract)

