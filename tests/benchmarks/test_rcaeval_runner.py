import hashlib
import json
import logging
from pathlib import Path

import pytest
from agents import AgentOutputSchema

from backend.benchmarks.rcaeval.models import (
    EndpointCapabilityIdentity,
    EvaluationBudget,
    RcaEvalConfiguration,
    RcaEvalPartition,
    RuntimeCaseEntry,
    RuntimeManifest,
    materialize_ss15_configurations,
)
from backend.benchmarks.rcaeval.providers import incident_event_for_case
from backend.benchmarks.rcaeval.runner import (
    RcaEvalCaseRunner,
    SingleInvestigatorAgent,
    _candidate_lifecycle_audit,
    _safe_exception_diagnostic,
    _unresolved_case_failure_category,
    validate_configuration_set,
)
from backend.db.session import create_db_engine, initialize_database
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.diagnosis.v11_runtime import V11SingleControlOutput
from backend.domain.agent_findings import CoordinationReview, RootCauseCandidate
from backend.domain.agent_plan import AgentExecution, AgentExecutionStatus
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    DiagnosticStatus,
    ExecutionStepKind,
    FailureCategory,
    LeadAction,
)
from backend.domain.runtime import (
    RuntimeEventType,
    RuntimeFailureCategory,
    RuntimeRunStatus,
)
from backend.runtime.sqlite_store import SQLiteRuntimeStore


def test_candidate_lifecycle_audit_ignores_resolved_retry_failures():
    candidate = RootCauseCandidate(
        id="candidate-audit",
        affected_entity="checkout-service",
        failure_mechanism="bounded observed symptom",
        summary="bounded observed symptom",
        rank=1,
        confidence=0.5,
        supporting_evidence_ids=["evidence-1"],
    )
    failed = AgentExecution(
        id="exec-lead-attempt-1",
        task_id="lead-planning-run-audit",
        agent_name="LeadAgent",
        runtime_run_id="run-audit",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.LEAD_PLANNING,
        analysis_round=1,
        attempt=1,
        failure_category=FailureCategory.INVALID_OUTPUT,
    )
    resolved = failed.model_copy(
        update={
            "id": "exec-lead-attempt-2",
            "status": AgentExecutionStatus.COMPLETED,
            "attempt": 2,
            "failure_category": FailureCategory.NONE,
        }
    )
    unresolved = failed.model_copy(
        update={
            "id": "exec-critic-attempt-1",
            "task_id": "critic-review-run-audit",
            "agent_name": "CriticAgent",
            "step_kind": ExecutionStepKind.CRITIC_REVIEW,
        }
    )
    audit = _candidate_lifecycle_audit(
        CoordinationReview(investigation_id="inv-audit", candidates=[candidate]),
        [failed, resolved, unresolved],
        [candidate],
        single=True,
    )

    assert audit.failure_categories == [FailureCategory.INVALID_OUTPUT.value]


def test_unresolved_case_failure_category_uses_only_live_execution_failures():
    failed_transport = AgentExecution(
        id="exec-transport-attempt-1",
        task_id="investigator-task-audit",
        agent_name="InvestigatorAgent",
        runtime_run_id="run-audit",
        status=AgentExecutionStatus.FAILED,
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        step_kind=ExecutionStepKind.INVESTIGATOR_ANALYSIS,
        analysis_round=1,
        attempt=1,
        failure_category=FailureCategory.TRANSPORT,
        error_message="model attempt failed",
    )

    assert _unresolved_case_failure_category([failed_transport]) == "transport"

    other_investigator_completed = failed_transport.model_copy(
        update={
            "id": "exec-other-investigator-completed",
            "task_id": "other-investigator-task-audit",
            "status": AgentExecutionStatus.COMPLETED,
            "failure_category": FailureCategory.NONE,
        }
    )
    assert _unresolved_case_failure_category(
        [failed_transport, other_investigator_completed]
    ) is None

    recovered = failed_transport.model_copy(
        update={
            "id": "exec-transport-attempt-2",
            "agent_name": "single-investigator",
            "status": AgentExecutionStatus.COMPLETED,
            "attempt": 2,
            "failure_category": FailureCategory.NONE,
        }
    )
    assert _unresolved_case_failure_category([failed_transport, recovered]) is None

    rejected_candidate = failed_transport.model_copy(
        update={
            "id": "exec-candidate-rejection",
            "failure_category": FailureCategory.INVALID_REFERENCE,
            "error_message": "candidate draft rejected: candidate_evidence_reference",
        }
    )
    assert _unresolved_case_failure_category([rejected_candidate]) is None


def test_safe_exception_diagnostic_redacts_message_and_reports_relative_location():
    try:
        raise ValueError(
            "api_key=sk-abcdefghijklmnopqrstuvwxyz "
            "base_url=https://user:pass@example.test/v1 "
            "path=C:\\private\\case.json "
            + "x" * 600
            + " TAIL-MARKER"
        )
    except ValueError as exc:
        diagnostic = _safe_exception_diagnostic(exc, Path.cwd())

    assert diagnostic["exception_type"] == "ValueError"
    assert "sk-" not in diagnostic["message"]
    assert "user:pass" not in diagnostic["message"]
    assert "C:\\private" not in diagnostic["message"]
    assert len(diagnostic["message"]) <= 256
    assert diagnostic["message"].endswith("TAIL-MARKER")  # 长消息保留尾部，便于定位截断点
    assert diagnostic["location"].startswith("tests/benchmarks/test_rcaeval_runner.py:")


def test_safe_exception_diagnostic_extracts_pydantic_detail_from_long_message():
    detail = (
        "1 validation error for V11SingleControlOutput "
        "investigator.candidates.0 Value error, "
        "onset window start must not be later than end "
        "[type=value_error, input_value={...}, input_type=dict] "
        "For further information visit https://errors.pydantic.dev/2.13/v/value_error"
    )
    try:
        raise ValueError(f"Invalid JSON when parsing {'x' * 600} for adapter; {detail}")
    except ValueError as exc:
        diagnostic = _safe_exception_diagnostic(exc, Path.cwd())

    assert len(diagnostic["message"]) <= 256
    assert "onset window start must not be later than end" in diagnostic["message"]


def test_single_control_output_is_a_valid_strict_json_schema():
    schema = AgentOutputSchema(
        V11SingleControlOutput, strict_json_schema=True
    ).json_schema()

    def assert_strict(value):
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_strict(child)
        elif isinstance(value, list):
            for child in value:
                assert_strict(child)

    assert_strict(schema)


def test_ss15_materializes_four_fair_configurations():
    configurations = materialize_ss15_configurations(
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
    configurations = materialize_ss15_configurations(
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

    configurations = materialize_ss15_configurations(
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
            RcaEvalPartition.SS15: 0,
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
    if output_type == "V11SingleControlOutput":
        return {"candidates": []}
    raise AssertionError(f"unexpected single output type: {output_type}")


def _failing_single_turn(**kwargs):
    raise ValueError(
        "api_key=sk-abcdefghijklmnopqrstuvwxyz "
        "base_url=https://user:pass@example.test/v1"
    )


def _invalid_multi_turn(**kwargs):
    assert kwargs["output_type"].__name__ == "LeadPlanningOutput"
    return {
        "decision": {
            "action": "investigate",
            "summary": "Inspect bounded offline evidence.",
            "task_ids": ["wrong-task-id"],
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


def _inconclusive_single_turn(**kwargs):
    output_type = kwargs["output_type"].__name__
    if output_type == "V11SingleControlOutput":
        return {"candidates": []}
    raise AssertionError(f"unexpected single output type: {output_type}")


def _single_turn_with_investigator(investigator):
    def _turn(**kwargs):
        output_type = kwargs["output_type"].__name__
        if output_type == "V11SingleControlOutput":
            return {"candidates": investigator.get("candidates", [])}
        raise AssertionError(f"unexpected single output type: {output_type}")

    return _turn


def _run_single_case(tmp_path: Path, turn):
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
        turn=turn,
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
    return prediction, repository, runtime_store


def test_single_control_empty_candidates_are_inconclusive(tmp_path: Path):
    turn = _single_turn_with_investigator(
        {
            "candidates": [],
        }
    )
    prediction, repository, runtime_store = _run_single_case(tmp_path, turn)

    assert prediction.completed is True
    assert prediction.candidates == []
    assert prediction.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert persisted.status == RuntimeRunStatus.COMPLETED
    assert repository.get_coordination_review(persisted.investigation_id).candidates == []


def test_single_control_drops_candidate_citing_unknown_references(tmp_path: Path):
    turn = _single_turn_with_investigator(
        {
            "summary": "Candidate references model-invented IDs.",
            "findings": [],
                "candidates": [
                    {
                        "affected_entity": "carts",
                        "failure_class": "thread pool exhaustion",
                        "failure_mechanism": "thread pool exhaustion",
                        "supporting_evidence_ids": ["ev-bogus"],
                        "contradicting_evidence_ids": [],
                    }
                ],
        }
    )
    prediction, repository, runtime_store = _run_single_case(tmp_path, turn)

    # finding ID 由服务端生成，模型填的引用几乎必然无效：准入层丢弃候选并
    # 审计（绝不改写），run 收敛 inconclusive 而不是终态校验杀 run。
    assert prediction.completed is True
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert persisted.status == RuntimeRunStatus.COMPLETED
    review = repository.get_coordination_review(persisted.investigation_id)
    assert review is not None
    assert review.candidates == []
    assert review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    audits = [
        item
        for item in repository.list_executions(persisted.investigation_id)
        if item.status == AgentExecutionStatus.FAILED
    ]
    assert len(audits) == 1
    assert audits[0].failure_category == FailureCategory.INVALID_REFERENCE
    assert audits[0].error_message == (
        "candidate draft rejected: candidate_evidence_reference"
    )


def test_single_control_rejects_incomplete_candidate_output(tmp_path: Path):
    turn = _single_turn_with_investigator(
        {
            "summary": "Candidate lacks entity, mechanism, and evidence.",
            "findings": [],
            "candidates": [
                {
                    "summary": "incomplete candidate",
                    "rank": 1,
                    "confidence": 0.5,
                }
            ],
        }
    )
    prediction, repository, runtime_store = _run_single_case(tmp_path, turn)

    # Candidate draft 的结构化输出契约拒绝不完整对象；不让它进入 review 或
    # predictions.json。共享准入层仍覆盖历史/内部构造的 domain 对象。
    assert prediction.completed is False
    assert prediction.failure_category == RuntimeFailureCategory.OUTPUT_VALIDATION
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert persisted.status == RuntimeRunStatus.FAILED
    review = repository.get_coordination_review(persisted.investigation_id)
    assert review is None or review.candidates == []
    audits = [
        item
        for item in repository.list_executions(persisted.investigation_id)
        if item.status == AgentExecutionStatus.FAILED
    ]
    assert audits


def test_single_control_candidate_rejection_is_audited_without_terminal_failure(
    tmp_path: Path,
):
    turn = _single_turn_with_investigator(
        {
            "candidates": [
                {
                    "affected_entity": "carts",
                    "failure_class": "unsupported",
                    "failure_mechanism": "unsupported mechanism",
                    "supporting_evidence_ids": ["ev-uncommitted"],
                }
            ],
        }
    )
    prediction, repository, runtime_store = _run_single_case(tmp_path, turn)

    assert prediction.completed is True
    assert prediction.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert persisted.status == RuntimeRunStatus.COMPLETED
    audits = [
        item
        for item in repository.list_executions(persisted.investigation_id)
        if item.status == AgentExecutionStatus.FAILED
    ]
    messages = [item.error_message for item in audits]
    assert "candidate draft rejected: candidate_evidence_reference" in messages
    assert all(item.failure_category == FailureCategory.INVALID_REFERENCE for item in audits)


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
    assert output_type in {"InvestigatorOutput", "InvestigatorCandidateOutput"}
    if output_type == "InvestigatorCandidateOutput":
        return {"candidates": []}
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
    assert prediction.diagnostic_status == DiagnosticStatus.INCONCLUSIVE
    assert prediction.candidates == []
    assert prediction.candidate_lifecycle is not None
    assert prediction.candidate_lifecycle.raw_structured_output_count == 1
    assert prediction.candidate_lifecycle.parsed_draft_count == 0
    assert prediction.candidate_lifecycle.admitted_candidate_count == 0
    assert prediction.candidate_lifecycle.persisted_candidate_count == 0
    assert prediction.candidate_lifecycle.published_candidate_count == 0
    assert prediction.candidate_lifecycle.raw_structured_output_hashes
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
    assert plan.lead_decision.selected_skills
    assert plan.lead_decision.selected_skills[0] == "first_failure_timeline@1.0.0"
    assert runtime_store.get_benchmark_replay_locator(persisted.id) == {
        "schema_version": 1,
        "benchmark": "rcaeval-re2-v11",
        "case_id": case.case_id,
        "partition": case.partition.value,
        "configuration": budget.configuration.value,
        "runtime_manifest_hash": "c" * 64,
        "execution_contract_hash": prediction.execution_contract_hash,
    }


def test_single_control_uses_server_owned_provisional_plan(tmp_path: Path):
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
        turn=_inconclusive_single_turn,
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
    assert persisted.status == RuntimeRunStatus.COMPLETED
    plan = repository.get_plan(persisted.investigation_id)
    assert plan is not None
    assert plan.lead_decision is not None
    assert plan.lead_decision.action == LeadAction.INVESTIGATE
    assert plan.lead_decision.task_ids
    assert plan.lead_decision.candidate_ids == []
    assert plan.tasks
    assert plan.tasks[0].id in plan.lead_decision.task_ids


def test_run_summary_keeps_total_tool_limit_after_phase_budget_is_exhausted(
    tmp_path: Path,
):
    prediction, repository, runtime_store = _run_single_case(tmp_path, _single_turn)
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    runtime = SingleInvestigatorAgent(
        model="bounded-test-model",
        model_provider="deepseek",
        model_name="bounded-test-model",
        max_total_tool_calls=8,
    )
    runtime.runtime_run_id = prediction.runtime_run_id
    runtime._phase_tool_budget = 0

    runtime._update_summary(repository, persisted.investigation_id)

    summary = repository.get(persisted.investigation_id).multi_agent_run
    assert summary is not None
    assert summary.max_total_tool_calls == 8


def test_single_control_logs_safe_exception_diagnostic(tmp_path: Path, caplog):
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
        turn=_failing_single_turn,
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

    with caplog.at_level(logging.WARNING):
        prediction = runner.run_case(case, budget)

    assert prediction.completed is False
    assert "rcaeval single control failed" in caplog.text
    assert "exception_type=ValueError" in caplog.text
    assert "tests/benchmarks/test_rcaeval_runner.py:" in caplog.text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in caplog.text


def test_multi_control_logs_safe_exception_diagnostic(tmp_path: Path, caplog):
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
        turn=_invalid_multi_turn,
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

    with caplog.at_level(logging.WARNING):
        prediction = runner.run_case(case, budget)

    assert prediction.completed is False
    assert "v11 phase failed phase=lead_planning" in caplog.text
    assert "exception_type=V11RuntimeContractError" in caplog.text
    assert "backend/diagnosis/v11_runtime.py:" in caplog.text
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in caplog.text
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert any(
        item.event_type == RuntimeEventType.MODEL_COMPLETED
        for item in runtime_store.list_events(persisted.id)
    )


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
    base = materialize_ss15_configurations(
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
        materialize_ss15_configurations(
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


def test_single_control_rejects_server_owned_candidate_fields(tmp_path: Path):
    turn = _single_turn_with_investigator(
        {
            "candidates": [
                {
                    "affected_entity": "carts",
                    "failure_class": "latency",
                    "failure_mechanism": "latency increase",
                    "supporting_evidence_ids": ["ev-1"],
                    "id": "candidate-model-owned",
                }
            ],
        }
    )
    prediction, repository, runtime_store = _run_single_case(tmp_path, turn)

    assert prediction.completed is False
    assert prediction.failure_category == RuntimeFailureCategory.OUTPUT_VALIDATION
    persisted = runtime_store.get_run(prediction.runtime_run_id)
    assert persisted.status == RuntimeRunStatus.FAILED
    audits = [
        item
        for item in repository.list_executions(persisted.investigation_id)
        if item.status == AgentExecutionStatus.FAILED
    ]
    assert audits


def _stub_prediction(completed: bool, category: str | None, run_suffix: str):
    from backend.benchmarks.rcaeval.models import CasePrediction

    return CasePrediction(
        case_id="re2-0123456789abcdef",
        configuration=RcaEvalConfiguration.SINGLE_INTENDED,
        completed=completed,
        runtime_run_id=f"run-{run_suffix}",
        execution_contract_hash="a" * 64,
        failure_category=category,
    )


class _StubRunner:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    def run_case(self, case, budget):
        del case, budget
        self.calls += 1
        return self._outcomes.pop(0)


def test_case_retry_retries_operational_failure_once():
    from backend.benchmarks.rcaeval.runner import run_case_with_bounded_retry

    runner = _StubRunner(
        [
            _stub_prediction(False, "timeout", "a1"),
            _stub_prediction(True, None, "a2"),
        ]
    )

    prediction = run_case_with_bounded_retry(runner, object(), object())

    assert prediction.completed is True
    assert prediction.runtime_run_id == "run-a2"
    assert prediction.attempts == 2
    assert runner.calls == 2


@pytest.mark.parametrize("category", ["transport", "rate_limit"])
def test_case_retry_retries_provider_failure_once(category):
    from backend.benchmarks.rcaeval.runner import run_case_with_bounded_retry

    runner = _StubRunner(
        [
            _stub_prediction(False, category, "a1"),
            _stub_prediction(True, None, "a2"),
        ]
    )

    prediction = run_case_with_bounded_retry(runner, object(), object())

    assert prediction.completed is True
    assert prediction.attempts == 2
    assert runner.calls == 2


def test_case_retry_does_not_retry_contract_integrity():
    from backend.benchmarks.rcaeval.runner import run_case_with_bounded_retry

    runner = _StubRunner([_stub_prediction(False, "contract_integrity", "a1")])

    prediction = run_case_with_bounded_retry(runner, object(), object())

    # 代码缺陷信号不重试：避免烧双倍预算掩盖 bug，保持 fail-closed。
    assert runner.calls == 1
    assert prediction.attempts == 1


def test_case_retry_returns_last_attempt_when_both_fail():
    from backend.benchmarks.rcaeval.runner import run_case_with_bounded_retry

    runner = _StubRunner(
        [
            _stub_prediction(False, "output_validation", "a1"),
            _stub_prediction(False, "timeout", "a2"),
        ]
    )

    prediction = run_case_with_bounded_retry(runner, object(), object())

    assert runner.calls == 2
    assert prediction.runtime_run_id == "run-a2"
    assert prediction.attempts == 2
    assert prediction.completed is False


def test_case_retry_skips_when_first_attempt_completes():
    from backend.benchmarks.rcaeval.runner import run_case_with_bounded_retry

    runner = _StubRunner([_stub_prediction(True, None, "a1")])

    prediction = run_case_with_bounded_retry(runner, object(), object())

    assert runner.calls == 1
    assert prediction.attempts == 1


def test_case_retry_rejects_max_attempts_beyond_contract_bound():
    # 复审 M1：CasePrediction.attempts 上限为 2，model_copy 不校验；
    # 构造侧必须挡住 max_attempts>2，避免冻结成功、评测期才被拒。
    import pytest

    from backend.benchmarks.rcaeval.runner import run_case_with_bounded_retry

    runner = _StubRunner([_stub_prediction(True, None, "a1")])

    with pytest.raises(ValueError, match="max_attempts"):
        run_case_with_bounded_retry(runner, object(), object(), max_attempts=3)
    with pytest.raises(ValueError, match="max_attempts"):
        run_case_with_bounded_retry(runner, object(), object(), max_attempts=0)
    assert runner.calls == 0


def test_evaluation_budget_timeout_cap_allows_authorized_300s():
    # 运营噪声链：正式 run 的 run deadline 经用户逐次授权可放宽至 300s
    # （spec §9.1 默认 120s 不变）；超过 300 仍 fail-closed。
    from pydantic import ValidationError

    from backend.benchmarks.rcaeval.models import EvaluationBudget

    budget = EvaluationBudget(
        configuration=RcaEvalConfiguration.SINGLE_INTENDED,
        token_budget=48_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=300,
        max_investigators=1,
        max_rounds=1,
    )
    assert budget.timeout_seconds == 300

    with pytest.raises(ValidationError):
        EvaluationBudget(
            configuration=RcaEvalConfiguration.SINGLE_INTENDED,
            token_budget=48_000,
            max_turns=8,
            tool_budget=8,
            timeout_seconds=301,
            max_investigators=1,
            max_rounds=1,
        )


def test_v11_runtime_accepts_authorized_300s_deadline():
    # 运营噪声链第三道守卫：V11Runtime.__init__ 的 timeout 上限与 domain
    # 常量对齐；300 合法、301 拒绝。epoch-6 即死于此守卫的旧 120s 上限。
    from backend.diagnosis.v11_runtime import V11Runtime

    runtime = V11Runtime(model=None, timeout_seconds=300, token_budget=48_000)
    assert runtime.timeout_seconds == 300

    with pytest.raises(ValueError, match="V11 timeout"):
        V11Runtime(model=None, timeout_seconds=301, token_budget=48_000)


def test_launch_preflight_catches_construction_rejection_before_token():
    # epoch-5/6 教训：worker 构造期守卫必须在 reauthorization token 消耗前
    # 触发。preflight 用相同启动参数离线过全部构造守卫。
    from types import SimpleNamespace

    from pydantic import ValidationError

    from backend.benchmarks.rcaeval.__main__ import _preflight_launch_construction

    base = dict(
        configuration="single_intended",
        token_budget=48_000,
        max_turns=8,
        tool_budget=8,
        timeout_seconds=300.0,
    )
    _preflight_launch_construction(SimpleNamespace(**base))

    with pytest.raises((ValueError, ValidationError)):
        _preflight_launch_construction(
            SimpleNamespace(**{**base, "timeout_seconds": 301.0})
        )


def test_launch_preflight_covers_multi_configuration():
    # 复审 low-3：multi 分支（SS15 后续侧的正式配置）同参数过构造守卫。
    from types import SimpleNamespace

    from backend.benchmarks.rcaeval.__main__ import _preflight_launch_construction

    _preflight_launch_construction(
        SimpleNamespace(
            configuration="multi_intended",
            token_budget=48_000,
            max_turns=8,
            tool_budget=8,
            timeout_seconds=300.0,
        )
    )


def test_launch_preflight_capability_requires_api_key(monkeypatch):
    # 复审 low-2：capability/key 准入也在 token 消耗前；缺 key 立即失败。
    from types import SimpleNamespace

    from backend.benchmarks.rcaeval.__main__ import _preflight_capability_admission

    monkeypatch.delenv("DIAGOPS_AGENTS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="DIAGOPS_AGENTS_API_KEY"):
        _preflight_capability_admission(
            SimpleNamespace(capability_artifact="unused", base_url="unused")
        )
