from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.benchmarks.openrca.models import (
    OpenRcaDifficulty,
    OpenRcaFailureMode,
    OpenRcaManifest,
    OpenRcaManifestCase,
    OpenRcaPartition,
    OpenRcaRuntimeCase,
    OpenRcaRuntimeIndex,
)

# OpenRCA 现代数据固定使用 UTC+8；固定 offset 避免 Windows 缺少 IANA tzdata 时隐式失败。
_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_TIME_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_DATE_PATTERN = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|"
    r"November|December)\s+\d{1,2},\s+\d{4}",
    re.IGNORECASE,
)
_CLOCK_PATTERN = re.compile(
    r"(\d{1,2}:\d{2})(?::\d{2})?\s+(?:to|and)\s+"
    r"(\d{1,2}:\d{2})(?::\d{2})?",
    re.IGNORECASE,
)
_ROOT_CAUSE_COUNT_PATTERNS = {
    1: re.compile(
        r"\b(?:a|one|single|1)\b[^.!?\n]{0,40}\bfailure\b",
        re.IGNORECASE,
    ),
    2: re.compile(
        r"\b(?:two|2)\b[^.!?\n]{0,40}\bfailures\b",
        re.IGNORECASE,
    ),
}


@dataclass(frozen=True)
class PrepareResult:
    manifest: OpenRcaManifest
    runtime_cases: list[OpenRcaRuntimeCase]
    manifest_path: Path
    runtime_index_path: Path


@dataclass(frozen=True)
class _Candidate:
    partition: OpenRcaPartition
    row: dict[str, str]
    row_id: str
    task_index: str
    difficulty: OpenRcaDifficulty
    failure_mode: OpenRcaFailureMode
    component_stratum: tuple[str, ...]
    reason_stratum: tuple[str, ...]


def prepare_cases(
    dataset_root: Path,
    output_dir: Path,
    *,
    per_partition: int = 10,
    seed: int = 42,
) -> PrepareResult:
    dataset_root = dataset_root.resolve()
    selected: list[_Candidate] = []
    for partition in OpenRcaPartition:
        candidates = _load_candidates(dataset_root, partition)
        if len(candidates) < per_partition:
            raise ValueError(f"partition {partition.value} has fewer than {per_partition} cases")
        selected.extend(_stratified_sample(candidates, per_partition, seed))

    manifest_cases: list[OpenRcaManifestCase] = []
    runtime_cases: list[OpenRcaRuntimeCase] = []
    for candidate in selected:
        telemetry_dir = _safe_telemetry_dir(dataset_root, candidate.partition, candidate.row)
        case_id = f"{candidate.partition.value}:{candidate.row_id}"
        start_time, end_time = _parse_window(candidate.row.get("instruction", ""))
        manifest_cases.append(
            OpenRcaManifestCase(
                case_id=case_id,
                partition=candidate.partition,
                row_id=candidate.row_id,
                task_index=candidate.task_index,
                difficulty=candidate.difficulty,
                failure_mode=candidate.failure_mode,
                telemetry_dir=telemetry_dir,
            )
        )
        runtime_cases.append(
            OpenRcaRuntimeCase(
                case_id=case_id,
                partition=candidate.partition,
                row_id=candidate.row_id,
                task_index=candidate.task_index,
                system=candidate.row.get("system", "").strip() or candidate.partition.value,
                date=candidate.row.get("date", "").strip() or start_time.date().isoformat(),
                service=candidate.row.get("service", "").strip()
                or candidate.row.get("service_scope", "").strip()
                or candidate.partition.value,
                instruction=candidate.row.get("instruction", ""),
                expected_root_cause_count=_expected_root_cause_count(
                    candidate.row.get("instruction", "")
                ),
                start_time=start_time,
                end_time=end_time,
                telemetry_dir=telemetry_dir,
            )
        )

    manifest = OpenRcaManifest(
        seed=seed,
        per_partition=per_partition,
        cases=manifest_cases,
    )
    manifest.manifest_hash = _manifest_hash(manifest)
    runtime_index = OpenRcaRuntimeIndex(
        case_manifest_hash=manifest.manifest_hash,
        cases=runtime_cases,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "case-manifest.json"
    runtime_index_path = output_dir / "runtime-cases.json"
    _write_json(manifest_path, manifest.model_dump(mode="json"))
    _write_json(runtime_index_path, runtime_index.model_dump(mode="json"))
    return PrepareResult(
        manifest=manifest,
        runtime_cases=runtime_cases,
        manifest_path=manifest_path,
        runtime_index_path=runtime_index_path,
    )


def _load_candidates(dataset_root: Path, partition: OpenRcaPartition) -> list[_Candidate]:
    directory = dataset_root / Path(partition.value)
    query_rows = _read_csv(directory / "query.csv")
    record_rows = _read_csv(directory / "record.csv")
    records_by_window: dict[int, list[dict[str, str]]] = defaultdict(list)
    for row in record_rows:
        if (window := _record_window(row)) is not None:
            records_by_window[window].append(row)

    candidates = []
    for row_number, row in enumerate(query_rows):
        task_index = _task_index(row)
        own_record = record_rows[row_number] if row_number < len(record_rows) else {}
        window = _record_window(own_record)
        records = records_by_window.get(window, []) if window is not None else [own_record]
        components = tuple(
            sorted(
                {
                    (item.get("component", "") or item.get("root_cause_component", "")).strip()
                    for item in records
                    if (item.get("component", "") or item.get("root_cause_component", "")).strip()
                }
            )
        )
        reasons = tuple(
            sorted(
                {
                    (item.get("reason", "") or item.get("root_cause_reason", "")).strip()
                    for item in records
                    if (item.get("reason", "") or item.get("root_cause_reason", "")).strip()
                }
            )
        )
        candidates.append(
            _Candidate(
                partition=partition,
                row=row,
                row_id=str(row_number),
                task_index=task_index,
                difficulty=_difficulty(task_index),
                failure_mode=(
                    OpenRcaFailureMode.MULTI if len(records) > 1 else OpenRcaFailureMode.SINGLE
                ),
                component_stratum=components,
                reason_stratum=reasons,
            )
        )
    return candidates


def _stratified_sample(candidates: list[_Candidate], count: int, seed: int) -> list[_Candidate]:
    groups: dict[tuple[object, ...], list[_Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[
            (
                candidate.difficulty,
                candidate.failure_mode,
                candidate.component_stratum,
                candidate.reason_stratum,
            )
        ].append(candidate)
    rng = random.Random(seed)
    for group in groups.values():
        rng.shuffle(group)

    selected: list[_Candidate] = []
    keys = sorted(groups, key=str)
    while len(selected) < count:
        progressed = False
        for key in keys:
            if groups[key] and len(selected) < count:
                selected.append(groups[key].pop())
                progressed = True
        if not progressed:
            break
    return selected


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as file:
        return [dict(row) for row in csv.DictReader(file)]


def _task_index(row: dict[str, str]) -> str:
    value = row.get("task_index", "").strip()
    if not value:
        raise ValueError("OpenRCA row is missing task_index")
    return value


def _record_window(row: dict[str, str]) -> int | None:
    try:
        timestamp = float(row.get("timestamp", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp):
        return None
    # OpenRCA generate.py 将同一半小时桶内的故障合并为一个多根因问题。
    return int(timestamp // 1800)


def _difficulty(task_index: str) -> OpenRcaDifficulty:
    try:
        index = int(task_index.rsplit("_", 1)[-1])
    except ValueError as exc:
        raise ValueError(f"invalid task_index: {task_index}") from exc
    if index <= 3:
        return OpenRcaDifficulty.EASY
    if index <= 6:
        return OpenRcaDifficulty.MIDDLE
    return OpenRcaDifficulty.HARD


def _safe_telemetry_dir(
    dataset_root: Path,
    partition: OpenRcaPartition,
    row: dict[str, str],
) -> str:
    value = row.get("telemetry_dir", "").strip()
    if not value:
        start_time, _ = _parse_window(row.get("instruction", ""))
        value = f"{partition.value}/telemetry/{start_time.date().isoformat().replace('-', '_')}"
    relative = Path(value)
    resolved = (dataset_root / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(dataset_root):
        raise ValueError("telemetry path is outside dataset root")
    return relative.as_posix()


def _parse_window(instruction: str) -> tuple[datetime, datetime]:
    matches = _TIME_PATTERN.findall(instruction)
    if len(matches) >= 2:
        start_time = datetime.strptime(matches[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TIMEZONE)
        end_time = datetime.strptime(matches[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=_TIMEZONE)
    else:
        date_match = _DATE_PATTERN.search(instruction)
        clock_match = _CLOCK_PATTERN.search(instruction)
        if date_match is None or clock_match is None:
            raise ValueError("OpenRCA instruction is missing an explicit time window")
        date = datetime.strptime(date_match.group(0).title(), "%B %d, %Y").date()
        start_clock = datetime.strptime(clock_match.group(1), "%H:%M").time()
        end_clock = datetime.strptime(clock_match.group(2), "%H:%M").time()
        start_time = datetime.combine(date, start_clock, _TIMEZONE)
        end_time = datetime.combine(date, end_clock, _TIMEZONE)
    if end_time <= start_time:
        end_time += timedelta(days=1)
    return start_time, end_time


def _expected_root_cause_count(instruction: str) -> int:
    matches = {
        count
        for count, pattern in _ROOT_CAUSE_COUNT_PATTERNS.items()
        if pattern.search(instruction)
    }
    if len(matches) != 1:
        raise ValueError("OpenRCA instruction root cause count must be exactly one or two")
    return matches.pop()


def _manifest_hash(manifest: OpenRcaManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
