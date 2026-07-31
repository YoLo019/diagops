import csv
from collections import Counter
from pathlib import Path

import pytest

from backend.benchmarks.openrca.models import OpenRcaFailureMode
from backend.benchmarks.openrca.prepare import (
    _expected_root_cause_count,
    prepare_cases,
)

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
                        "task_index": f"task_{(index - 1) % 7 + 1}",
                        "instruction": (
                            "A failure occurred on July 14, 2026, within the "
                            "time range of 12:00 to 12:10. Diagnose it."
                        ),
                        "telemetry_dir": "",
                        "scoring_points": "root cause component is secret",
                    }
                )
        with (directory / "record.csv").open("w", encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(
                file,
                fieldnames=("timestamp", "datetime", "component", "reason"),
            )
            writer.writeheader()
            for index in range(1, 13):
                writer.writerow(
                    {
                        "timestamp": str(1784001600 + index * 60),
                        "datetime": f"2026-07-14 12:{index:02d}:00",
                        "component": f"service-{index}",
                        "reason": "fabricated failure",
                    }
                )
    return root


def test_prepare_selects_ten_per_partition_with_seed_42(full_fixture_root: Path, tmp_path: Path):
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
    assert len({case.case_id for case in first.manifest.cases}) == 40
    assert all("/telemetry/2026_07_14" in case.telemetry_dir for case in first.manifest.cases)
    assert all(case.failure_mode == OpenRcaFailureMode.MULTI for case in first.manifest.cases)
    assert all(case.expected_root_cause_count == 1 for case in first.runtime_cases)


@pytest.mark.parametrize(
    ("instruction", "expected"),
    [
        ("Two system failures were reported.", 2),
        ("2 failures were reported.", 2),
        ("A single failure was reported.", 1),
        ("One failure was reported.", 1),
        ("A failure was reported.", 1),
        ("There was 1 failure.", 1),
    ],
)
def test_expected_root_cause_count_uses_only_instruction(instruction, expected):
    assert _expected_root_cause_count(instruction) == expected


@pytest.mark.parametrize(
    "instruction",
    [
        "Investigate the incident.",
        "One failure and two failures were reported.",
        "Three failures were reported.",
    ],
)
def test_expected_root_cause_count_rejects_missing_or_ambiguous_instruction(
    instruction,
):
    with pytest.raises(ValueError, match="root cause count"):
        _expected_root_cause_count(instruction)


def test_committed_fixture_uses_official_query_and_record_headers(fixture_root: Path):
    for partition in PARTITIONS:
        with (fixture_root / partition / "query.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            query = next(csv.DictReader(file))
        with (fixture_root / partition / "record.csv").open(
            encoding="utf-8-sig", newline=""
        ) as file:
            record = next(csv.DictReader(file))

        assert query["task_index"].startswith("task_")
        assert set(query) == {"task_index", "instruction", "scoring_points"}
        assert set(record) == {"timestamp", "datetime", "component", "reason"}


def test_runtime_index_contains_no_ground_truth(fixture_root: Path, tmp_path: Path):
    result = prepare_cases(fixture_root, tmp_path, per_partition=1, seed=42)
    serialized = result.runtime_index_path.read_text(encoding="utf-8")

    assert "scoring_points" not in serialized
    assert "root cause component" not in serialized.lower()
    assert "record.csv" not in serialized
    assert all(case.expected_root_cause_count == 1 for case in result.runtime_cases)
    assert all(case.timezone == "Asia/Shanghai" for case in result.runtime_cases)
    assert all(
        case.start_time.utcoffset().total_seconds() == 8 * 3600 for case in result.runtime_cases
    )


def test_prepare_rejects_telemetry_path_escape(fixture_root: Path, tmp_path: Path):
    copied = tmp_path / "dataset"
    for partition in PARTITIONS:
        source = fixture_root / partition
        target = copied / partition
        target.mkdir(parents=True)
        if partition == "Bank":
            with (source / "query.csv").open(encoding="utf-8-sig", newline="") as file:
                row = next(csv.DictReader(file))
            row["telemetry_dir"] = "../../record.csv"
            with (target / "query.csv").open("w", encoding="utf-8", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=tuple(row))
                writer.writeheader()
                writer.writerow(row)
        else:
            (target / "query.csv").write_text(
                (source / "query.csv").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        (target / "record.csv").write_text(
            (source / "record.csv").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    with pytest.raises(ValueError, match="outside dataset root"):
        prepare_cases(copied, tmp_path / "output", per_partition=1, seed=42)
