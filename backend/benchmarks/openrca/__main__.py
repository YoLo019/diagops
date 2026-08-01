from __future__ import annotations

import argparse
from pathlib import Path

from agents import set_tracing_disabled

from backend.benchmarks.openrca.evaluator import (
    evaluate_run,
    gate_run,
    paired_gate_run,
    targeted_gate_run,
    write_official_query_inputs,
)
from backend.benchmarks.openrca.prepare import load_excluded_case_ids, prepare_cases
from backend.benchmarks.openrca.runner import (
    OpenRcaDiagnosisRunner,
    run_benchmark_pair,
)
from backend.domain.multi_agent import InvestigationStrategy, ModelProvider
from backend.services.container import get_container


def main() -> None:
    # Runtime 事件才是基准审计源；SDK tracing 会把自定义端点凭据发往官方 exporter。
    set_tracing_disabled(True)
    parser = argparse.ArgumentParser(prog="python -m backend.benchmarks.openrca")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--dataset-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--per-partition", type=int, default=10)
    prepare.add_argument("--seed", type=int, default=42)
    prepare.add_argument("--exclude-safe-index", type=Path, action="append", default=None)

    run = commands.add_parser("run")
    run.add_argument("--dataset-root", type=Path, required=True)
    run.add_argument("--safe-index", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--model")
    run.add_argument("--mode", choices=("deterministic", "agent-shadow"))
    run.add_argument(
        "--provider",
        choices=tuple(item.value for item in ModelProvider),
        default=ModelProvider.OPENAI.value,
    )
    run.add_argument("--strategy", choices=("fixed", "adaptive", "both"), default="both")
    run.add_argument("--prompt-version", default="v8.2")
    run.add_argument("--input-cost-per-million", type=float, default=0)
    run.add_argument("--output-cost-per-million", type=float, default=0)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--query-root", type=Path, required=True)
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--official-query-output", type=Path)

    gate = commands.add_parser("gate")
    gate.add_argument("--run-dir", type=Path, required=True)

    targeted_gate = commands.add_parser("targeted-gate")
    targeted_gate.add_argument("--run-dir", type=Path, required=True)

    paired_gate = commands.add_parser("paired-gate")
    paired_gate.add_argument("--safe-index", type=Path, required=True)
    paired_gate.add_argument("--baseline-run-dir", type=Path, required=True)
    paired_gate.add_argument("--baseline-evaluation-dir", type=Path, required=True)
    paired_gate.add_argument("--candidate-run-dir", type=Path, required=True)
    paired_gate.add_argument("--candidate-evaluation-dir", type=Path, required=True)
    paired_gate.add_argument("--official-evaluator-root", type=Path, required=True)
    paired_gate.add_argument("--output", type=Path, required=True)

    arguments = parser.parse_args()
    if arguments.command == "prepare":
        excluded = frozenset()
        if arguments.exclude_safe_index:
            try:
                excluded = load_excluded_case_ids(arguments.exclude_safe_index)
            except (OSError, ValueError) as exc:
                parser.error(f"invalid exclusion safe-index: {exc}")
        result = prepare_cases(
            arguments.dataset_root,
            arguments.output,
            per_partition=arguments.per_partition,
            seed=arguments.seed,
            excluded_case_ids=excluded,
        )
        print(result.manifest.manifest_hash)
        return
    if arguments.command == "evaluate":
        report_path = evaluate_run(arguments.query_root, arguments.run_dir)
        if arguments.official_query_output is not None:
            write_official_query_inputs(
                arguments.query_root,
                arguments.run_dir,
                arguments.official_query_output,
            )
        print(report_path)
        return
    if arguments.command == "gate":
        failures = gate_run(arguments.run_dir)
        if failures:
            parser.error("; ".join(failures))
        print("release gate passed")
        return
    if arguments.command == "targeted-gate":
        failures = targeted_gate_run(arguments.run_dir)
        if failures:
            parser.error("; ".join(failures))
        print("targeted gate passed")
        return
    if arguments.command == "paired-gate":
        failures = paired_gate_run(
            safe_index=arguments.safe_index,
            baseline_run_dir=arguments.baseline_run_dir,
            baseline_evaluation_dir=arguments.baseline_evaluation_dir,
            candidate_run_dir=arguments.candidate_run_dir,
            candidate_evaluation_dir=arguments.candidate_evaluation_dir,
            official_evaluator_root=arguments.official_evaluator_root,
            output_path=arguments.output,
        )
        if failures:
            parser.error("; ".join(failures))
        print("paired gate passed")
        return

    if arguments.mode == "deterministic":
        if arguments.model is not None:
            parser.error("deterministic mode does not accept --model")
        if arguments.strategy != "fixed":
            parser.error("deterministic mode requires --strategy fixed")
        model = "deterministic"
        prompt_version = "v10-shared-core"
    else:
        if arguments.model is None:
            parser.error("--model is required outside deterministic mode")
        model = arguments.model
        prompt_version = arguments.prompt_version

    strategies = (
        (InvestigationStrategy.FIXED, InvestigationStrategy.ADAPTIVE)
        if arguments.strategy == "both"
        else (InvestigationStrategy(arguments.strategy),)
    )
    container = get_container()
    result = run_benchmark_pair(
        OpenRcaDiagnosisRunner(
            arguments.dataset_root,
            None if arguments.mode == "deterministic" else model,
            repository=container.repository,
            runtime_store=container.runtime_store,
            provider=ModelProvider(arguments.provider),
            prompt_version=prompt_version,
            deterministic=arguments.mode == "deterministic",
        ),
        arguments.safe_index,
        arguments.output,
        model=model,
        provider=ModelProvider(arguments.provider),
        prompt_version=prompt_version,
        timeout_seconds=container.settings.agents.timeout_seconds,
        input_cost_per_million=arguments.input_cost_per_million,
        output_cost_per_million=arguments.output_cost_per_million,
        strategies=strategies,
        mode=arguments.mode or "agent",
    )
    print(result.run_id)


if __name__ == "__main__":
    main()
