from __future__ import annotations

import csv
import hashlib
import json
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
            raise ValueError(
                f"partition {partition.value} has fewer than {per_partition} cases"
            )
        selected.extend(_stratified_sample(candidates, per_partition, seed))

    manifest_cases: list[OpenRcaManifestCase] = []
    runtime_cases: list[OpenRcaRuntimeCase] = []
    for candidate in selected:
        telemetry_dir = _safe_telemetry_dir(
            dataset_root, candidate.partition, candidate.row
        )
        case_id = f"{candidate.partition.value}:{candidate.task_index}"
        start_time, end_time = _parse_window(candidate.row.get("instruction", ""))
        manifest_cases.append(
            OpenRcaManifestCase(
                case_id=case_id,
                partition=candidate.partition,
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
                instruction=candidate.row.get("instruction", ""),
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


def _load_candidates(
    dataset_root: Path, partition: OpenRcaPartition
) -> list[_Candidate]:
    directory = dataset_root / Path(partition.value)
    query_rows = _read_csv(directory / "query.csv")
    record_rows = _read_csv(directory / "record.csv")
    records_by_task: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in record_rows:
        records_by_task[_task_index(row)].append(row)

    candidates = []
    for row in query_rows:
        task_index = _task_index(row)
        records = records_by_task.get(task_index, [])
        components = tuple(
            sorted(
                {
                    item.get("root_cause_component", "").strip()
                    for item in records
                    if item.get("root_cause_component", "").strip()
                }
            )
        )
        reasons = tuple(
            sorted(
                {
                    item.get("root_cause_reason", "").strip()
                    for item in records
                    if item.get("root_cause_reason", "").strip()
                }
            )
        )
        candidates.append(
            _Candidate(
                partition=partition,
                row=row,
                task_index=task_index,
                difficulty=_difficulty(task_index),
                failure_mode=(
                    OpenRcaFailureMode.MULTI
                    if len(records) > 1
                    else OpenRcaFailureMode.SINGLE
                ),
                component_stratum=components,
                reason_stratum=reasons,
            )
        )
    return candidates


def _stratified_sample(
    candidates: list[_Candidate], count: int, seed: int
) -> list[_Candidate]:
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


def _difficulty(task_index: str) -> OpenRcaDifficulty:
    try:
        index = int(task_index)
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
        value = f"{partition.value}/telemetry/{start_time.date().isoformat()}"
    relative = Path(value)
    resolved = (dataset_root / relative).resolve()
    if relative.is_absolute() or not resolved.is_relative_to(dataset_root):
        raise ValueError("telemetry path is outside dataset root")
    return relative.as_posix()


def _parse_window(instruction: str) -> tuple[datetime, datetime]:
    matches = _TIME_PATTERN.findall(instruction)
    if len(matches) < 2:
        raise ValueError("OpenRCA instruction is missing an explicit time window")
    start_time = datetime.strptime(matches[0], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_TIMEZONE
    )
    end_time = datetime.strptime(matches[1], "%Y-%m-%d %H:%M:%S").replace(
        tzinfo=_TIMEZONE
    )
    if end_time <= start_time:
        raise ValueError("OpenRCA time window must be increasing")
    return start_time, end_time


def _manifest_hash(manifest: OpenRcaManifest) -> str:
    payload = manifest.model_dump(mode="json", exclude={"manifest_hash"})
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
