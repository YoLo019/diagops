from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from tests.runtime.golden_contract import (  # noqa: E402
    canonical_runtime_contract,
    contract_checksum,
)

CASES = (
    "deployment_regression",
    "traffic_spike",
    "dependency_timeout",
    "database_slowdown",
    "single_bad_instance",
)
STRATEGIES = ("fixed", "adaptive")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--debug-raw", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))

    from backend.db.repositories import InMemoryInvestigationRepository
    from backend.diagnosis.coordinator import DiagnosisCoordinator
    from backend.diagnosis.orchestrator import DiagnosisOrchestrator
    from backend.domain.multi_agent import InvestigationStrategy
    from backend.providers.registry import build_mock_provider_registry
    from backend.rca.analyzer import RcaAnalyzer
    from backend.reports.generator import ReportGenerator
    from backend.services.incident_cases import load_incident_case

    args.output.mkdir(parents=True, exist_ok=True)
    checksums = {}
    for case_id in CASES:
        for strategy_value in STRATEGIES:
            providers = build_mock_provider_registry()
            repository = InMemoryInvestigationRepository()
            orchestrator = DiagnosisOrchestrator(
                repository=repository,
                providers=providers,
                analyzer=RcaAnalyzer(),
                report_generator=ReportGenerator(),
                coordinator=DiagnosisCoordinator(providers),
            )
            strategy = InvestigationStrategy(strategy_value)
            record = orchestrator.run(load_incident_case(case_id), strategy=strategy)
            review = repository.get_coordination_review(record.id)
            raw = {
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
            contract = canonical_runtime_contract(raw)
            checksum = contract_checksum(contract)
            name = f"{case_id}__{strategy_value}.json"
            fixture = {
                "baseline_commit": args.commit,
                "case_id": case_id,
                "strategy": strategy_value,
                "checksum": checksum,
                "contract": contract,
            }
            if args.debug_raw:
                fixture["raw_markdown"] = raw["record"]["report"]["markdown"]
            (args.output / name).write_text(
                json.dumps(fixture, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            checksums[name] = checksum
    (args.output / "checksums.json").write_text(
        json.dumps(
            {"baseline_commit": args.commit, "fixtures": checksums},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
