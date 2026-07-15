import csv
from collections import Counter
from pathlib import Path

import pytest

from backend.benchmarks.openrca.prepare import prepare_cases

PARTITIONS = (
    "Bank",
    "Telecom",
    "Market/cloudbed-1",
    "Market/cloudbed-2",
)


@pytest.fixture
def fixture_root() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "openrca"


@pytest.fixture
def full_fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    for partition in PARTITIONS:
        directory = root / partition
        directory.mkdir(parents=True)
        with (directory / "query.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=(
                    "task_index",
                    "instruction",
                    "telemetry_dir",
                    "scoring_points",
                ),
            )
            writer.writeheader()
            for index in range(1, 13):
                writer.writerow(
                    {
                        "task_index": index,
                        "instruction": (
                            "Diagnose the incident from 2026-07-14 12:00:00 "
                            "to 2026-07-14 12:10:00."
                        ),
                        "telemetry_dir": f"{partition}/telemetry/2026-07-14",
                        "scoring_points": "root cause component is secret",
                    }
                )
        with (directory / "record.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=("task_index", "root_cause_component", "root_cause_reason"),
            )
            writer.writeheader()
            for index in range(1, 13):
                writer.writerow(
                    {
                        "task_index": index,
                        "root_cause_component": f"service-{index}",
                        "root_cause_reason": "fabricated failure",
                    }
                )
    return root


def test_prepare_selects_ten_per_partition_with_seed_42(
    full_fixture_root: Path, tmp_path: Path
):
    first = prepare_cases(full_fixture_root, tmp_path / "first", per_partition=10, seed=42)
    second = prepare_cases(full_fixture_root, tmp_path / "second", per_partition=10, seed=42)

    assert Counter(case.partition.value for case in first.manifest.cases) == {
        "Bank": 10,
        "Telecom": 10,
        "Market/cloudbed-1": 10,
        "Market/cloudbed-2": 10,
    }
    assert [case.case_id for case in first.manifest.cases] == [
        case.case_id for case in second.manifest.cases
    ]
    assert first.manifest.manifest_hash == second.manifest.manifest_hash


def test_runtime_index_contains_no_ground_truth(fixture_root: Path, tmp_path: Path):
    result = prepare_cases(fixture_root, tmp_path, per_partition=1, seed=42)
    serialized = result.runtime_index_path.read_text(encoding="utf-8")

    assert "scoring_points" not in serialized
    assert "root cause component" not in serialized.lower()
    assert "record.csv" not in serialized
    assert all(case.timezone == "Asia/Shanghai" for case in result.runtime_cases)
    assert all(
        case.start_time.utcoffset().total_seconds() == 8 * 3600
        for case in result.runtime_cases
    )


def test_prepare_rejects_telemetry_path_escape(fixture_root: Path, tmp_path: Path):
    query_path = fixture_root / "Bank" / "query.csv"
    original = query_path.read_text(encoding="utf-8")
    escaped = original.replace("Bank/telemetry/2026-07-14", "../../record.csv")
    copied = tmp_path / "dataset"
    for partition in PARTITIONS:
        source = fixture_root / partition
        target = copied / partition
        target.mkdir(parents=True)
        (target / "query.csv").write_text(
            escaped if partition == "Bank" else (source / "query.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (target / "record.csv").write_text(
            (source / "record.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="outside dataset root"):
        prepare_cases(copied, tmp_path / "output", per_partition=1, seed=42)
