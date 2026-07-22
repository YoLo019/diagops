import asyncio
import json
from pathlib import Path

import pytest

from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.multi_agent import InvestigationStrategy
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.services.incident_cases import load_incident_case
from tests.runtime.golden_contract import (
    _markdown_section_hashes,
    canonical_runtime_contract,
    contract_checksum,
)

BASELINE_COMMIT = "3f06d9ae87729185b3429c839f11cc366de75000"
FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "v8_2_3f06d9a"
GOLDEN_CASES = (
    "deployment_regression",
    "traffic_spike",
    "dependency_timeout",
    "database_slowdown",
    "single_bad_instance",
)


def _orchestrator() -> DiagnosisOrchestrator:
    providers = build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=InMemoryInvestigationRepository(),
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
    )


def _raw_contract(orchestrator, record):
    repository = orchestrator.repository
    review = repository.get_coordination_review(record.id)
    return {
        "record": record.model_dump(mode="json"),
        "findings": [
            item.model_dump(mode="json")
            for item in repository.list_agent_findings(record.id)
        ],
        "review": review.model_dump(mode="json") if review is not None else None,
        "tasks": [
            item.model_dump(mode="json")
            for item in repository.list_tasks(record.id)
        ],
        "executions": [
            item.model_dump(mode="json")
            for item in repository.list_executions(record.id)
        ],
        "tool_calls": [
            item.model_dump(mode="json")
            for item in repository.list_tool_calls(record.id)
        ],
    }


def test_markdown_contract_detects_semantic_line_reordering() -> None:
    original = "# 根因\n先验证数据库延迟\n再检查服务超时\n"
    reordered = "# 根因\n再检查服务超时\n先验证数据库延迟\n"

    assert _markdown_section_hashes(original) != _markdown_section_hashes(reordered)


def test_markdown_contract_preserves_stable_id_order_within_a_line() -> None:
    forward = "# 根因\n证据链：EVIDENCE_1 → EVIDENCE_2\n"
    reversed_ids = "# 根因\n证据链：EVIDENCE_2 → EVIDENCE_1\n"

    assert _markdown_section_hashes(forward) != _markdown_section_hashes(reversed_ids)


@pytest.mark.parametrize("case_id", GOLDEN_CASES)
@pytest.mark.parametrize(
    "strategy", [InvestigationStrategy.FIXED, InvestigationStrategy.ADAPTIVE]
)
def test_durable_phases_match_real_3f06d9a_fixtures(
    case_id: str, strategy: InvestigationStrategy
) -> None:
    fixture_name = f"{case_id}__{strategy.value}.json"
    fixture = json.loads((FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8"))
    manifest = json.loads(
        (FIXTURE_ROOT / "checksums.json").read_text(encoding="utf-8")
    )
    assert fixture["baseline_commit"] == BASELINE_COMMIT
    assert manifest["baseline_commit"] == BASELINE_COMMIT
    assert contract_checksum(fixture["contract"]) == fixture["checksum"]
    assert manifest["fixtures"][fixture_name] == fixture["checksum"]

    orchestrator = _orchestrator()
    record = asyncio.run(
        DiagnosisPhaseExecutor(orchestrator).execute(
            load_incident_case(case_id), strategy=strategy
        )
    ).record
    actual = canonical_runtime_contract(_raw_contract(orchestrator, record))

    assert actual == fixture["contract"]
    assert contract_checksum(actual) == fixture["checksum"]
