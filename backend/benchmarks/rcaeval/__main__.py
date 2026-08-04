"""RCAEval 准备工具 CLI：inspect 只读预览，prepare 产出隔离的 runtime/标签包。

数据集与标签永远位于仓库之外；本命令只接受显式 --source 与 --pin。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.benchmarks.rcaeval.prepare import (
    inspect_source,
    load_pin,
    prepare_partitions,
)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.benchmarks.rcaeval")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser("inspect", help="校验源与 pin 并预览分区选择，不写文件")
    inspect.add_argument("--source", type=Path, required=True)
    inspect.add_argument("--pin", type=Path, required=True)

    prepare = commands.add_parser("prepare", help="产出隔离的 runtime 包与 evaluator-only 标签包")
    prepare.add_argument("--source", type=Path, required=True)
    prepare.add_argument("--pin", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)

    arguments = parser.parse_args()
    pin = load_pin(arguments.pin)
    if arguments.command == "inspect":
        report = inspect_source(arguments.source, pin)
        summary = {
            "source_case_count": report.source_case_count,
            "partition_counts": {
                partition.value: count for partition, count in report.partition_counts.items()
            },
            "selected_source_ids": {
                partition.value: ids for partition, ids in report.selected_source_ids.items()
            },
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return
    result = prepare_partitions(arguments.source, arguments.pin, arguments.output)
    print(result.runtime_manifest.manifest_hash)


if __name__ == "__main__":
    main()
