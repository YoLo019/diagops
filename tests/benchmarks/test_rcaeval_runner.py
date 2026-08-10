import json
from pathlib import Path

import pytest

from backend.benchmarks.rcaeval.models import (
    EndpointCapabilityIdentity,
    EvaluationBudget,
    RcaEvalConfiguration,
    RcaEvalPartition,
    RuntimeCaseEntry,
    RuntimeManifest,
    materialize_ss30_configurations,
)
from backend.benchmarks.rcaeval.providers import incident_event_for_case
from backend.benchmarks.rcaeval.runner import (
    RcaEvalCaseRunner,
    validate_configuration_set,
)
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.runtime import RuntimeEventType, RuntimeRunStatus
from backend.runtime.sqlite_store import SQLiteRuntimeStore


def test_ss30_materializes_four_fair_configurations():
    configurations = materialize_ss30_configurations(
        single_budget=4_000,
        multi_budget=10_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
    )

    assert set(configurations) == set(RcaEvalConfiguration)
    assert configurations[RcaEvalConfiguration.SINGLE_INTENDED].token_budget == 4_000
    assert configurations[RcaEvalConfiguration.MULTI_INTENDED].token_budget == 10_000
    assert configurations[RcaEvalConfiguration.SINGLE_EQUAL_TOKEN].token_budget == 12_000
    assert configurations[RcaEvalConfiguration.MULTI_EQUAL_TOKEN].token_budget == 12_000
    assert {item.max_turns for item in configurations.values()} == {8}
    assert {item.tool_budget for item in configurations.values()} == {8}
    assert {item.timeout_seconds for item in configurations.values()} == {120}
    validate_configuration_set(configurations)


def test_configuration_set_rejects_over_three_x_and_mixed_non_token_limits():
    configurations = materialize_ss30_configurations(
        single_budget=4_000,
        multi_budget=10_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
    )
    configurations[RcaEvalConfiguration.MULTI_INTENDED] = configurations[
        RcaEvalConfiguration.MULTI_INTENDED
    ].model_copy(update={"token_budget": 12_001})
    with pytest.raises(ValueError, match="3x"):
        validate_configuration_set(configurations)

    configurations = materialize_ss30_configurations(
        single_budget=4_000,
        multi_budget=10_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
    )
    configurations[RcaEvalConfiguration.MULTI_EQUAL_TOKEN] = configurations[
        RcaEvalConfiguration.MULTI_EQUAL_TOKEN
    ].model_copy(update={"tool_budget": 9})
    with pytest.raises(ValueError, match="tool|identity"):
        validate_configuration_set(configurations)


def _write_runtime_package(root: Path) -> RuntimeCaseEntry:
    case = RuntimeCaseEntry(
        case_id="re2-aaaaaaaaaaaaaaaa",
        partition=RcaEvalPartition.OB30,
        files=["telemetry-00.csv"],
    )
    case_dir = root / "cases" / case.case_id
    case_dir.mkdir(parents=True)
    (case_dir / "telemetry-00.csv").write_text(
        "time,container_name,message,level,req_path,error\n"
        "12:00,checkout,timeout contacting payment,ERROR,/checkout,true\n",
        encoding="utf-8",
    )
    manifest = RuntimeManifest(
        upstream_repository="https://example.invalid/rcaeval",
        upstream_revision="a" * 40,
        upstream_artifact="fixture",
        archive_sha256="b" * 64,
        selection_seed="fixture-seed",
        partition_counts={
            RcaEvalPartition.OB30: 1,
            RcaEvalPartition.SS30: 0,
            RcaEvalPartition.TT90: 0,
        },
        cases=[case],
        manifest_hash="c" * 64,
    )
    (root / "manifest.json").write_text(
        json.dumps(manifest.model_dump(mode="json")), encoding="utf-8"
    )
    return case


def _single_turn(**kwargs):
    output_type = kwargs["output_type"].__name__
    if output_type == "LeadPlanningOutput":
        return {
            "decision": {
                "action": "investigate",
                "summary": "Inspect bounded offline evidence.",
                "task_ids": ["single-task"],
                "candidate_ids": [],
                "selected_skills": ["first_failure_timeline@1.0.0"],
            },
            "tasks": [
                {
                    "id": "single-task",
                    "title": "Inspect evidence",
                    "description": "Use the frozen read-only tools.",
                    "tool_names": ["read_logs"],
                    "information_gap": "affected service and mechanism",
                }
            ],
        }
    assert output_type == "InvestigatorOutput"
    return {"summary": "No supported candidate.", "findings": [], "candidates": []}


def _multi_turn(**kwargs):
    if kwargs["output_type"].__name__ == "LeadAdjudicationOutput":
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "No supported candidate.",
                "stop_reason": "insufficient_evidence",
            }
        }
    return _single_turn(**kwargs)


def test_single_control_runs_through_persisted_sqlite_v11_runtime(tmp_path: Path):
    runtime_package = tmp_path / "runtime"
    case = _write_runtime_package(runtime_package)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    runtime_store = SQLiteRuntimeStore(engine, repository)
    runner = RcaEvalCaseRunner(
        runtime_package=runtime_package,
        model="bounded-test-model",
        capability=EndpointCapabilityIdentity(
            provider="deepseek",
            model="bounded-test-model",
            api_mode="chat_completions",
            endpoint_id="bounded-test-endpoint",
            artifact_hash="d" * 64,
        ),
        repository=repository,
        runtime_store=runtime_store,
        turn=_single_turn,
    )
    budget = EvaluationBudget(
        configuration=RcaEvalConfiguration.SINGLE_INTENDED,
        token_budget=4_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
        max_investigators=1,
        max_rounds=1,
    )

    prediction = runner.run_case(case, budget)

    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert prediction.completed is True
    assert prediction.candidates == []
    assert persisted.status == RuntimeRunStatus.COMPLETED
    assert persisted.execution_contract["execution_contract_digest"] == (
        prediction.execution_contract_hash
    )
    actors = [
        event.safe_payload.get("actor")
        for event in runtime_store.list_events(persisted.id)
        if event.event_type == RuntimeEventType.MODEL_COMPLETED
    ]
    assert "CriticAgent" not in actors
    assert len(actors) == 2
    plan = repository.get_plan(persisted.investigation_id)
    assert plan is not None
    assert plan.lead_decision is not None
    assert plan.lead_decision.selected_skills == ["first_failure_timeline@1.0.0"]
    assert runtime_store.get_benchmark_replay_locator(persisted.id) == {
        "schema_version": 1,
        "benchmark": "rcaeval-re2-v11",
        "case_id": case.case_id,
        "partition": case.partition.value,
        "configuration": budget.configuration.value,
        "runtime_manifest_hash": "c" * 64,
        "execution_contract_hash": prediction.execution_contract_hash,
    }


def test_multi_configuration_uses_same_persisted_production_entry(tmp_path: Path):
    runtime_package = tmp_path / "runtime"
    case = _write_runtime_package(runtime_package)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    initialize_database(engine)
    repository = SQLiteInvestigationRepository(engine)
    runtime_store = SQLiteRuntimeStore(engine, repository)
    runner = RcaEvalCaseRunner(
        runtime_package=runtime_package,
        model="bounded-test-model",
        capability=EndpointCapabilityIdentity(
            provider="deepseek",
            model="bounded-test-model",
            api_mode="chat_completions",
            endpoint_id="bounded-test-endpoint",
            artifact_hash="d" * 64,
        ),
        repository=repository,
        runtime_store=runtime_store,
        turn=_multi_turn,
    )
    budget = EvaluationBudget(
        configuration=RcaEvalConfiguration.MULTI_INTENDED,
        token_budget=10_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
        max_investigators=3,
        max_rounds=2,
    )

    prediction = runner.run_case(case, budget)

    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert prediction.completed is True
    assert persisted.status == RuntimeRunStatus.COMPLETED
    assert persisted.execution_contract["limits"]["max_investigators"] == 3
    assert persisted.execution_contract["limits"]["max_rounds"] == 2


def test_prediction_event_exposes_no_dataset_or_system_identity(tmp_path: Path):
    runtime_package = tmp_path / "runtime"
    case = _write_runtime_package(runtime_package)

    event = incident_event_for_case(
        runtime_package / "cases" / case.case_id,
        case.case_id,
    )
    model_input = event.model_dump_json().casefold()

    assert "rcaeval" not in model_input
    assert "sock shop" not in model_input
    assert "train ticket" not in model_input
    assert "online boutique" not in model_input
