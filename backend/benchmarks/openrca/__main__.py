from __future__ import annotations

import argparse
from pathlib import Path

from backend.benchmarks.openrca.evaluator import (
    evaluate_run,
    write_official_query_inputs,
)
from backend.benchmarks.openrca.prepare import prepare_cases
from backend.benchmarks.openrca.runner import (
    OpenRcaDiagnosisRunner,
    run_benchmark_pair,
)
from backend.domain.multi_agent import InvestigationStrategy


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.benchmarks.openrca")
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--dataset-root", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--per-partition", type=int, default=10)
    prepare.add_argument("--seed", type=int, default=42)

    run = commands.add_parser("run")
    run.add_argument("--dataset-root", type=Path, required=True)
    run.add_argument("--safe-index", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--model", required=True)
    run.add_argument(
        "--strategy", choices=("fixed", "adaptive", "both"), default="both"
    )
    run.add_argument("--prompt-version", default="v8.2")
    run.add_argument("--input-cost-per-million", type=float, default=0)
    run.add_argument("--output-cost-per-million", type=float, default=0)

    evaluate = commands.add_parser("evaluate")
    evaluate.add_argument("--query-root", type=Path, required=True)
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--official-query-output", type=Path)

    arguments = parser.parse_args()
    if arguments.command == "prepare":
        result = prepare_cases(
            arguments.dataset_root,
            arguments.output,
            per_partition=arguments.per_partition,
            seed=arguments.seed,
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

    strategies = (
        (InvestigationStrategy.FIXED, InvestigationStrategy.ADAPTIVE)
        if arguments.strategy == "both"
        else (InvestigationStrategy(arguments.strategy),)
    )
    result = run_benchmark_pair(
        OpenRcaDiagnosisRunner(arguments.dataset_root, arguments.model),
        arguments.safe_index,
        arguments.output,
        model=arguments.model,
        prompt_version=arguments.prompt_version,
        input_cost_per_million=arguments.input_cost_per_million,
        output_cost_per_million=arguments.output_cost_per_million,
        strategies=strategies,
    )
    print(result.run_id)


if __name__ == "__main__":
    main()
