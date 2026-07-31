from __future__ import annotations

import csv
import math
from collections.abc import Iterator
from datetime import UTC, datetime, tzinfo
from pathlib import Path

_TIMESTAMP_KEYS = ("timestamp", "startTime")
_COMPONENT_KEYS = ("cmdb_id", "service", "serviceName", "tc")
_METRIC_NAME_KEYS = ("kpi_name", "name", "metric")
_METRIC_VALUE_KEYS = ("value", "metric_value")
_RESERVED_COLUMNS = frozenset(
    {
        *_TIMESTAMP_KEYS,
        *_COMPONENT_KEYS,
        *_METRIC_NAME_KEYS,
        *_METRIC_VALUE_KEYS,
        "instance",
        "level",
        "severity",
        "message",
        "log",
        "content",
        "trace_id",
        "traceId",
        "span_id",
        "id",
        "parent_id",
        "parent_span",
        "pid",
        "duration",
        "elapsedTime",
        "success",
        "status_code",
    }
)


def resolve_case_directory(dataset_root: Path, telemetry_dir: str) -> Path:
    root = dataset_root.resolve()
    relative = Path(telemetry_dir)
    directory = (root / relative).resolve()
    if relative.is_absolute() or not directory.is_relative_to(root):
        raise ValueError("telemetry path is outside dataset root")
    return directory


def rows_for(directory: Path, *filename_terms: str) -> Iterator[dict[str, str]]:
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*.csv")):
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("telemetry file is outside safe case directory")
        name = path.relative_to(directory).as_posix().casefold()
        if not any(term in name for term in filename_terms):
            continue
        with path.open(encoding="utf-8-sig", newline="") as file:
            yield from (dict(row) for row in csv.DictReader(file))


def row_timestamp(row: dict[str, str], default_timezone: tzinfo) -> datetime | None:
    value = _first(row, _TIMESTAMP_KEYS)
    if not value:
        return None
    try:
        numeric = float(value)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.replace(tzinfo=parsed.tzinfo or default_timezone)
    if not math.isfinite(numeric):
        return None
    if abs(numeric) >= 100_000_000_000:
        numeric /= 1000
    return datetime.fromtimestamp(numeric, UTC).astimezone(default_timezone)


def row_component(row: dict[str, str]) -> str | None:
    return _first(row, _COMPONENT_KEYS) or None


def row_metrics(row: dict[str, str]) -> Iterator[tuple[str, float]]:
    metric_name = _first(row, _METRIC_NAME_KEYS)
    metric_value = _first(row, _METRIC_VALUE_KEYS)
    if metric_name and (value := _finite_float(metric_value)) is not None:
        yield metric_name, value
        return
    for name, raw in row.items():
        if name in _RESERVED_COLUMNS:
            continue
        value = _finite_float(raw)
        if value is not None:
            yield name, value


def row_trace_id(row: dict[str, str]) -> str | None:
    return _first(row, ("trace_id", "traceId")) or None


def row_span_id(row: dict[str, str]) -> str | None:
    return _first(row, ("span_id", "id")) or None


def row_parent_span_id(row: dict[str, str]) -> str | None:
    return _first(row, ("parent_id", "parent_span", "pid")) or None


def row_duration(row: dict[str, str]) -> float | None:
    return _finite_float(_first(row, ("duration", "elapsedTime")))


def row_success(row: dict[str, str]) -> bool:
    success = row.get("success", "").strip().casefold()
    if success:
        return success in {"1", "true", "yes", "ok", "success"}
    status = _finite_float(row.get("status_code", ""))
    return status is None or status < 400


def _first(row: dict[str, str], keys: tuple[str, ...]) -> str:
    return next((row[key].strip() for key in keys if row.get(key, "").strip()), "")


def _finite_float(value: str) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
