"""Tempo Docker-local parity gate（spec 7.7/R23）。

职责链：有界 preflight（daemon/pinned image/端口/磁盘）-> 唯一 Compose project
启动 -> OTLP 恒定偏移 replay（canonical->backend 可逆映射持久化）-> File 与
Tempo 结果归一化对比 -> 仅按 project 名做 scoped cleanup。
Docker 不可用或 preflight 不通过时 gate 记录 blocked 及确切原因；offline
验收独立运行，永不被本 gate 标记失败。退出码：0=passed，1=failed，2=blocked。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from backend.domain.events import IncidentEvent
from backend.domain.evidence import TraceSpanPayload
from backend.domain.tool_queries import TraceDirection, TraceQuery
from backend.providers.local_package import (
    FileTraceProvider,
    LocalIncidentPackage,
    load_incident_event,
)
from backend.providers.results import ProviderResult
from backend.providers.tempo import ReplayMapping, TempoTraceProvider

GATE_VERSION = "tempo-gate-v1"
TEMPO_GATE_IMAGE = "grafana/otel-lgtm:0.11.0"
TEMPO_HTTP_PORT = 3200
OTLP_HTTP_PORT = 4318
READY_TIMEOUT_SECONDS = 60.0
INGEST_TIMEOUT_SECONDS = 60.0
REPLAY_OFFSET_MS = 7 * 24 * 3600 * 1000  # 恒定偏移：canonical 事件窗口 +7 天
MIN_FREE_DISK_BYTES = 2 * 1024 * 1024 * 1024
DURATION_TOLERANCE_MS = 2.0


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    image_digest: str | None = None


@dataclass(frozen=True)
class GateReport:
    status: str  # passed | failed | blocked
    rows: list[dict[str, Any]] = field(default_factory=list)
    preflight: tuple[str, ...] = ()
    image: str = TEMPO_GATE_IMAGE
    image_digest: str | None = None
    project: str | None = None
    replay_manifest: dict[str, Any] | None = None


def _run_command(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _port_free(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def pinned_image_digest() -> str | None:
    """不可变 image 身份只来自显式配置，绝不从可变 tag 推断。"""
    configured = os.environ.get("DIAGOPS_TEMPO_GATE_IMAGE_DIGEST", "").strip()
    return configured or None


def preflight(
    *,
    runner=_run_command,
    port_free=_port_free,
    disk_free_bytes: int | None = None,
    image_digest: str | None = None,
) -> PreflightResult:
    reasons: list[str] = []
    try:
        probe = runner(["docker", "info", "--format", "{{.ServerVersion}}"])
        daemon_ok = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        daemon_ok = False
    if not daemon_ok:
        reasons.append("docker daemon unavailable")

    digest = image_digest if image_digest is not None else pinned_image_digest()
    if daemon_ok:
        if digest is None:
            reasons.append("pinned image digest not configured")
        else:
            try:
                inspect = runner(
                    [
                        "docker",
                        "image",
                        "inspect",
                        TEMPO_GATE_IMAGE,
                        "--format",
                        "{{json .RepoDigests}}",
                    ]
                )
                repo_digests = (
                    json.loads(inspect.stdout) if inspect.returncode == 0 else []
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                repo_digests = []
            if not any(str(item).endswith(f"@{digest}") for item in repo_digests):
                reasons.append("pinned image digest not present locally")

    for port in (TEMPO_HTTP_PORT, OTLP_HTTP_PORT):
        if not port_free(port):
            reasons.append(f"port {port} already in use")

    free = (
        disk_free_bytes
        if disk_free_bytes is not None
        else shutil.disk_usage(Path.cwd()).free
    )
    if free < MIN_FREE_DISK_BYTES:
        reasons.append("insufficient free disk space")

    return PreflightResult(ok=not reasons, reasons=tuple(reasons), image_digest=digest)


def build_replay_mapping(spans: list[TraceSpanPayload]) -> ReplayMapping:
    """为 replay 生成恒定时间偏移与可逆 ID 映射；canonical 事件范围不改写。"""
    return ReplayMapping(
        offset_ms=REPLAY_OFFSET_MS,
        trace_id_map={
            span.trace_id: hashlib.sha256(
                f"replay-trace:{span.trace_id}".encode()
            ).hexdigest()[:32]
            for span in spans
        },
        span_id_map={
            span.span_id: hashlib.sha256(
                f"replay-span:{span.span_id}".encode()
            ).hexdigest()[:16]
            for span in spans
        },
    )


def build_otlp_export(
    spans: list[TraceSpanPayload], mapping: ReplayMapping
) -> dict[str, Any]:
    """把 canonical span 转为 OTLP/HTTP JSON（backend 时间/ID 已偏移）。"""
    by_service: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        backend_start = mapping.to_backend_time(span.started_at)
        start_nano = int(backend_start.timestamp() * 1_000_000_000)
        end_nano = start_nano + int(span.duration_ms * 1_000_000)
        status_code = {"ok": 1, "error": 2, "unset": 0}[span.status.value]
        by_service.setdefault(span.service, []).append(
            {
                "traceId": mapping.to_backend_trace_id(span.trace_id),
                "spanId": mapping.span_id_map.get(span.span_id, span.span_id),
                "parentSpanId": (
                    mapping.span_id_map.get(span.parent_span_id, span.parent_span_id)
                    if span.parent_span_id
                    else ""
                ),
                "name": span.operation,
                "startTimeUnixNano": str(start_nano),
                "endTimeUnixNano": str(end_nano),
                "status": {"code": status_code},
                "attributes": [
                    {"key": key, "value": {"stringValue": value}}
                    for key, value in span.attributes.items()
                ],
            }
        )
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}}
                    ]
                },
                "scopeSpans": [{"spans": service_spans}],
            }
            for service, service_spans in sorted(by_service.items())
        ]
    }


def normalize_trace_evidence(result: ProviderResult) -> list[dict[str, Any]]:
    """parity 只比较 canonical 字段；evidence ID 属 invocation 私有，不参与。"""
    normalized = []
    for item in result.evidence_items:
        payload = item.payload
        normalized.append(
            {
                "trace_id": payload.get("trace_id"),
                "span_id": payload.get("span_id"),
                "parent_span_id": payload.get("parent_span_id"),
                "service": payload.get("service"),
                "operation": payload.get("operation"),
                "status": payload.get("status"),
                "started_at": payload.get("started_at"),
                "duration_ms": payload.get("duration_ms"),
            }
        )
    return normalized


def compare_trace_results(
    file_result: ProviderResult,
    tempo_result: ProviderResult,
    *,
    duration_tolerance_ms: float = DURATION_TOLERANCE_MS,
) -> list[str]:
    """按 path、parent 关系、service/operation、顺序、status、时长容差对比。"""
    mismatches: list[str] = []
    if file_result.status != tempo_result.status:
        mismatches.append(
            f"status differs: file={file_result.status.value} tempo={tempo_result.status.value}"
        )
    left = normalize_trace_evidence(file_result)
    right = normalize_trace_evidence(tempo_result)
    if len(left) != len(right):
        mismatches.append(f"span count differs: file={len(left)} tempo={len(right)}")
        return mismatches
    for index, (lhs, rhs) in enumerate(zip(left, right, strict=True)):
        for field_name in (
            "trace_id",
            "span_id",
            "parent_span_id",
            "service",
            "operation",
            "status",
            "started_at",
        ):
            if lhs[field_name] != rhs[field_name]:
                mismatches.append(
                    f"span {index} {field_name} differs: "
                    f"file={lhs[field_name]} tempo={rhs[field_name]}"
                )
        if (
            lhs["duration_ms"] is not None
            and rhs["duration_ms"] is not None
            and abs(lhs["duration_ms"] - rhs["duration_ms"]) > duration_tolerance_ms
        ):
            mismatches.append(
                f"span {index} duration differs beyond tolerance: "
                f"file={lhs['duration_ms']} tempo={rhs['duration_ms']}"
            )
    return mismatches


def run_gate(
    output_dir: Path,
    *,
    package: LocalIncidentPackage | None = None,
    compose_file: Path = Path("compose.tempo-gate.yaml"),
    runner=_run_command,
    port_free=_port_free,
    image_digest: str | None = None,
    http_client_factory=None,
    sleep=time.sleep,
) -> GateReport:
    preflight_result = preflight(
        runner=runner, port_free=port_free, image_digest=image_digest
    )
    if not preflight_result.ok:
        report = GateReport(
            status="blocked",
            preflight=preflight_result.reasons,
            image_digest=preflight_result.image_digest,
        )
        _write_report(output_dir, report)
        return report

    package = package or _default_package(output_dir)
    event = load_incident_event(package)
    spans = _load_canonical_spans(package)
    mapping = build_replay_mapping(spans)
    project = f"tempo-gate-{uuid4().hex[:8]}"
    rows: list[dict[str, Any]] = []
    status = "passed"
    try:
        _compose(runner, compose_file, project, "up", "-d")
        _wait_ready(sleep=sleep)
        _replay(spans, mapping)
        _wait_ingested(spans, mapping, sleep=sleep)
        tempo = TempoTraceProvider(
            f"http://127.0.0.1:{TEMPO_HTTP_PORT}", mapping=mapping
        )
        file_provider = FileTraceProvider(package)
        for label, query in _parity_queries(event):
            file_result = file_provider.collect(event, query)
            tempo_result = tempo.collect(event, query)
            mismatches = compare_trace_results(file_result, tempo_result)
            rows.append(
                {
                    "query": label,
                    "passed": not mismatches,
                    "mismatches": mismatches,
                }
            )
            if mismatches:
                status = "failed"
    except (OSError, subprocess.SubprocessError, httpx.HTTPError, TimeoutError) as exc:
        status = "failed"
        rows.append({"query": "gate", "passed": False, "mismatches": [type(exc).__name__]})
    finally:
        # cleanup 只作用于本 project 创建的资源。
        _compose(runner, compose_file, project, "down", "-v", "--remove-orphans", check=False)

    report = GateReport(
        status=status,
        rows=rows,
        project=project,
        image_digest=preflight_result.image_digest,
        replay_manifest={
            "offset_ms": mapping.offset_ms,
            "trace_id_map": mapping.trace_id_map,
            "span_id_map": mapping.span_id_map,
            "compose_file": compose_file.as_posix(),
        },
    )
    _write_report(output_dir, report)
    return report


def _parity_queries(event: IncidentEvent) -> list[tuple[str, TraceQuery]]:
    start = event.started_at - timedelta(minutes=event.time_window_minutes)
    end = event.started_at + timedelta(minutes=event.time_window_minutes)
    window = {"window_start": start, "window_end": end}
    return [
        ("error_only", TraceQuery(**window, error_only=True)),
        (
            "service_upstream",
            TraceQuery(
                **window,
                service="payment-service",
                direction=TraceDirection.UPSTREAM,
            ),
        ),
        ("full_window", TraceQuery(**window)),
    ]


def _default_package(output_dir: Path) -> LocalIncidentPackage:
    from backend.services.offline_tool_acceptance import build_synthetic_package

    return build_synthetic_package(output_dir / "package")


def _load_canonical_spans(package: LocalIncidentPackage) -> list[TraceSpanPayload]:
    source = package.sources.get("spans")
    if source is None or not source.present or not source.file:
        raise ValueError("tempo gate requires a spans source")
    raw = json.loads((package.root / source.file).read_text(encoding="utf-8"))
    return [TraceSpanPayload.model_validate(row) for row in raw]


def _compose(runner, compose_file: Path, project: str, *args: str, check: bool = True):
    result = runner(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            compose_file.as_posix(),
            *args,
        ],
        timeout=120.0,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker compose {args[0]} failed for project {project}")
    return result


def _wait_ready(*, sleep) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{TEMPO_HTTP_PORT}/ready", timeout=2.0
            )
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        sleep(1.0)
    raise TimeoutError("tempo readiness polling exceeded deadline")


def _replay(spans: list[TraceSpanPayload], mapping: ReplayMapping) -> None:
    payload = build_otlp_export(spans, mapping)
    response = httpx.post(
        f"http://127.0.0.1:{OTLP_HTTP_PORT}/v1/traces",
        json=payload,
        timeout=10.0,
    )
    response.raise_for_status()


def _wait_ingested(
    spans: list[TraceSpanPayload], mapping: ReplayMapping, *, sleep
) -> None:
    if not spans:
        return
    backend_id = mapping.to_backend_trace_id(spans[0].trace_id)
    deadline = time.monotonic() + INGEST_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            response = httpx.get(
                f"http://127.0.0.1:{TEMPO_HTTP_PORT}/api/traces/{backend_id}",
                timeout=2.0,
            )
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        sleep(1.0)
    raise TimeoutError("tempo ingestion polling exceeded deadline")


def _write_report(output_dir: Path, report: GateReport) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "gate_version": GATE_VERSION,
        "status": report.status,
        "image": report.image,
        "image_digest": report.image_digest,
        "project": report.project,
        "preflight_reasons": list(report.preflight),
        "rows": report.rows,
        "replay_manifest": report.replay_manifest,
        "generated_at": datetime.now(UTC).isoformat(),
    }
    (output_dir / "tempo-gate-report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DiagOps Tempo Docker-local parity gate")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/tempo_acceptance"),
    )
    parser.add_argument("--package-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    package = (
        LocalIncidentPackage.load(args.package_dir) if args.package_dir else None
    )
    report = run_gate(args.output_dir, package=package)
    print(f"tempo gate: status={report.status} project={report.project}")
    for reason in report.preflight:
        print(f"blocked reason: {reason}")
    for row in report.rows:
        if not row.get("passed", True):
            print(f"FAILED {row['query']}: {row['mismatches']}")
    if report.status == "passed":
        return 0
    return 2 if report.status == "blocked" else 1


if __name__ == "__main__":
    raise SystemExit(main())
