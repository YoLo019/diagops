import hashlib
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
    if output_type == "SingleControlOutput":
        return {
            "planning": {
                "decision": {
                    "action": "investigate",
                    "summary": "Inspect bounded offline evidence.",
                    "task_ids": ["single-control-task"],
                    "candidate_ids": [],
                    "selected_skills": ["first_failure_timeline@1.0.0"],
                },
                "tasks": [
                    {
                        "id": "single-control-task",
                        "title": "Inspect evidence",
                        "description": "Use the frozen read-only tools.",
                        "tool_names": ["read_logs"],
                        "information_gap": "affected service and mechanism",
                    }
                ],
            },
            "investigator": {
                "summary": "No supported candidate.",
                "findings": [],
                "candidates": [],
            },
        }
    raise AssertionError(f"unexpected single output type: {output_type}")


def _multi_turn(**kwargs):
    output_type = kwargs["output_type"].__name__
    if output_type == "LeadPlanningOutput":
        return {
            "decision": {
                "action": "investigate",
                "summary": "Inspect bounded offline evidence.",
                "task_ids": ["multi-task"],
                "candidate_ids": [],
            },
            "tasks": [
                {
                    "id": "multi-task",
                    "title": "Inspect evidence",
                    "description": "Use the frozen read-only tools.",
                    "tool_names": ["read_logs"],
                    "information_gap": "affected service and mechanism",
                }
            ],
        }
    if output_type == "LeadAdjudicationOutput":
        return {
            "decision": {
                "action": "inconclusive",
                "summary": "No supported candidate.",
                "stop_reason": "insufficient_evidence",
            }
        }
    assert output_type == "InvestigatorOutput"
    return {"summary": "No supported candidate.", "findings": [], "candidates": []}


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
    assert len(actors) == 1
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


def test_configuration_set_rejects_flattened_and_swapped_topology():
    base = materialize_ss30_configurations(
        single_budget=4_000,
        multi_budget=10_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=120,
    )
    flattened = {
        key: value.model_copy(update={"max_investigators": 3, "max_rounds": 2})
        for key, value in base.items()
    }
    with pytest.raises(ValueError, match="topology"):
        validate_configuration_set(flattened)

    swapped = dict(
        materialize_ss30_configurations(
            single_budget=4_000,
            multi_budget=10_000,
            max_turns=8,
            tool_budget=8,
            timeout_seconds=120,
        )
    )
    swapped[RcaEvalConfiguration.SINGLE_INTENDED] = swapped[
        RcaEvalConfiguration.SINGLE_INTENDED
    ].model_copy(update={"max_investigators": 3, "max_rounds": 2})
    swapped[RcaEvalConfiguration.MULTI_INTENDED] = swapped[
        RcaEvalConfiguration.MULTI_INTENDED
    ].model_copy(update={"max_investigators": 1, "max_rounds": 1})
    with pytest.raises(ValueError, match="topology"):
        validate_configuration_set(swapped)


def _side_directory(root: Path, files: dict[str, bytes], sums_bytes: bytes):
    side = root / "side"
    side.mkdir(parents=True, exist_ok=True)
    for relative, content in files.items():
        path = side / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (side / "SHA256SUMS").write_bytes(sums_bytes)
    return side


def test_side_checksum_rejects_noncanonical_path_spellings(tmp_path: Path):
    from backend.benchmarks.rcaeval.runner import _verify_side_directory

    digest = hashlib.sha256(b"{}").hexdigest()
    malformed = [
        f"{digest}  ./predictions.json\n",
        f"{digest}  ../predictions.json\n",
        f"{digest}  /predictions.json\n",
        f"{digest}  PREDICTIONS.JSON\n",
        f"{digest} predictions.json\n",
        f"{digest}  predictions.json",
        f"{digest}  predictions.json\n{digest}  predictions.json\n",
    ]
    for index, line in enumerate(malformed):
        sums = line.encode("utf-8")
        side = _side_directory(tmp_path / str(index), {"predictions.json": b"{}"}, sums)
        with pytest.raises(ValueError, match="canonical|checksum|malformed|escapes|differs"):
            _verify_side_directory(side, hashlib.sha256(sums).hexdigest())


def test_side_checksum_rejects_duplicate_unsorted_and_set_drift(tmp_path: Path):
    from backend.benchmarks.rcaeval.runner import _verify_side_directory

    digest_a = hashlib.sha256(b"a").hexdigest()
    digest_b = hashlib.sha256(b"b").hexdigest()
    files = {"a.json": b"a", "b.json": b"b"}
    cases = [
        # duplicate entries
        f"{digest_a}  a.json\n{digest_a}  a.json\n{digest_b}  b.json\n",
        # unsorted serialization
        f"{digest_b}  b.json\n{digest_a}  a.json\n",
        # missing on-disk entry
        f"{digest_a}  a.json\n{digest_b}  b.json\n{digest_b}  c.json\n",
        # extra on-disk file not covered by sums
        f"{digest_a}  a.json\n",
    ]
    for index, text in enumerate(cases):
        sums = text.encode("utf-8")
        side = _side_directory(tmp_path / str(index), files, sums)
        with pytest.raises(ValueError, match="duplicate|sorted|differs|checksum"):
            _verify_side_directory(side, hashlib.sha256(sums).hexdigest())


def test_side_checksum_rejects_post_freeze_content_tamper(tmp_path: Path):
    from backend.benchmarks.rcaeval.runner import _verify_side_directory

    digest = hashlib.sha256(b"{}").hexdigest()
    sums = f"{digest}  predictions.json\n".encode()
    side = _side_directory(tmp_path, {"predictions.json": b"{}"}, sums)
    bundle_hash = hashlib.sha256(sums).hexdigest()
    _verify_side_directory(side, bundle_hash)
    (side / "predictions.json").write_bytes(b'{"tampered": true}')
    with pytest.raises(ValueError, match="changed|checksum|content"):
        _verify_side_directory(side, bundle_hash)


def test_frozen_run_identity_fails_closed_without_dependency_lock(tmp_path: Path):
    from backend.benchmarks.rcaeval.runner import frozen_run_identity
    from backend.services.source_identity import write_source_manifest

    diagnosis = tmp_path / "backend" / "diagnosis"
    diagnosis.mkdir(parents=True)
    (diagnosis / "v11_runtime.py").write_text("# fake runtime\n", encoding="utf-8")
    write_source_manifest(tmp_path)
    capability = EndpointCapabilityIdentity(
        provider="openai_compatible",
        model="frozen-model",
        api_mode="chat_completions",
        endpoint_id="endpoint",
        artifact_hash="1" * 64,
    )
    kwargs = {
        "runtime_manifest_hash": "2" * 64,
        "capability": capability,
        "tool_manifest_hash_value": "3" * 64,
        "skill_catalog_hash_value": "4" * 64,
        "repository_root": tmp_path,
    }
    with pytest.raises(ValueError, match="dependency lock"):
        frozen_run_identity(**kwargs)

    (tmp_path / "uv.lock").write_text("lock\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    write_source_manifest(tmp_path)
    identity = frozen_run_identity(**kwargs)
    expected = hashlib.sha256()
    for name in ("uv.lock", "pyproject.toml"):
        expected.update(name.encode("utf-8"))
        expected.update((tmp_path / name).read_bytes())
    assert identity.dependency_lock_hash == expected.hexdigest()
    assert identity.dependency_lock_hash != identity.source_manifest_hash
