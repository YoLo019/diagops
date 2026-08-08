"""Tempo gate preflight、blocked 语义与 scoped cleanup（全部 fake，不需要 Docker）。"""

import json
import subprocess
from datetime import UTC, datetime

from backend.providers.results import ProviderResult, ProviderStatus
from backend.services import tempo_acceptance
from backend.services.offline_tool_acceptance import build_synthetic_package
from backend.services.tempo_acceptance import (
    GateReport,
    build_otlp_export,
    build_replay_mapping,
    compare_trace_results,
    main,
    preflight,
    run_gate,
)

_DIGEST = "sha256:" + "ab" * 32


def _completed(returncode=0, stdout=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_preflight_blocked_when_daemon_unavailable():
    result = preflight(
        runner=lambda args, timeout=30.0: _completed(returncode=1),
        image_digest=_DIGEST,
        disk_free_bytes=10 * 1024**3,
    )

    assert not result.ok
    assert "docker daemon unavailable" in result.reasons


def test_preflight_blocked_when_digest_not_configured():
    def runner(args, timeout=30.0):
        return _completed(returncode=0, stdout="29.0.0")

    result = preflight(
        runner=runner,
        image_digest=None,
        disk_free_bytes=10 * 1024**3,
    )

    assert not result.ok
    assert "pinned image digest not configured" in result.reasons


def test_preflight_blocked_on_digest_mismatch_and_port_conflict():
    def runner(args, timeout=30.0):
        if "inspect" in args:
            return _completed(stdout=json.dumps(["grafana/otel-lgtm@sha256:" + "ff" * 32]))
        return _completed(stdout="29.0.0")

    result = preflight(
        runner=runner,
        port_free=lambda port: False,
        image_digest=_DIGEST,
        disk_free_bytes=10 * 1024**3,
    )

    assert not result.ok
    assert "pinned image digest not present locally" in result.reasons
    assert "port 3200 already in use" in result.reasons
    assert "port 4318 already in use" in result.reasons


def test_preflight_ok_with_matching_digest():
    def runner(args, timeout=30.0):
        if "inspect" in args:
            return _completed(stdout=json.dumps([f"grafana/otel-lgtm@{_DIGEST}"]))
        return _completed(stdout="29.0.0")

    result = preflight(
        runner=runner,
        image_digest=_DIGEST,
        disk_free_bytes=10 * 1024**3,
    )

    assert result.ok
    assert result.image_digest == _DIGEST


def test_blocked_gate_writes_report_with_reasons(tmp_path):
    report = run_gate(
        tmp_path / "gate",
        runner=lambda args, timeout=120.0: _completed(returncode=1),
        image_digest=_DIGEST,
    )

    assert report.status == "blocked"
    persisted = json.loads((tmp_path / "gate" / "tempo-gate-report.json").read_text())
    assert persisted["status"] == "blocked"
    assert "docker daemon unavailable" in persisted["preflight_reasons"]
    assert persisted["rows"] == []


def test_blocked_gate_exit_code_is_two(tmp_path, monkeypatch):
    monkeypatch.setattr(
        tempo_acceptance,
        "run_gate",
        lambda output_dir, package=None: GateReport(
            status="blocked", preflight=("docker daemon unavailable",)
        ),
    )

    assert main(["--output-dir", str(tmp_path / "gate")]) == 2


def test_otlp_export_roundtrip_preserves_canonical_identity():
    from backend.domain.evidence import TraceSpanPayload
    from backend.providers.tempo import _project_otlp_trace

    started = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    spans = [
        TraceSpanPayload(
            trace_id="a" * 32,
            span_id="1" * 16,
            parent_span_id=None,
            service="checkout-service",
            operation="POST:/checkout",
            started_at=started,
            duration_ms=100.0,
            status="error",
        ),
        TraceSpanPayload(
            trace_id="a" * 32,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            service="payment-service",
            operation="POST:/pay",
            started_at=started,
            duration_ms=90.0,
            status="ok",
        ),
    ]
    mapping = build_replay_mapping(spans)
    export = build_otlp_export(spans, mapping)
    # OTLP JSON 只含一条 resourceSpans 列表；逐 batch 投影回 canonical。
    projected = [
        span
        for batch in export["resourceSpans"]
        for span in _project_otlp_trace({"batches": [batch]}, mapping)
    ]

    assert sorted(spans, key=lambda span: span.span_id) == sorted(
        projected, key=lambda span: span.span_id
    )
    # 导出确实使用了偏移后的 backend 时间。
    backend_start = int(
        export["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["startTimeUnixNano"]
    )
    assert backend_start != int(spans[0].started_at.timestamp() * 1_000_000_000)


def test_compare_detects_field_count_and_duration_mismatch():
    from backend.domain.evidence import EvidenceItem

    def result(duration_ms: float, span_id: str) -> ProviderResult:
        return ProviderResult(
            provider="trace",
            status=ProviderStatus.SUCCESS,
            evidence_items=[
                EvidenceItem(
                    provider="trace",
                    kind="trace_path",
                    timestamp=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
                    summary="s",
                    payload={
                        "trace_id": "a" * 32,
                        "span_id": span_id,
                        "parent_span_id": None,
                        "service": "svc",
                        "operation": "op",
                        "status": "ok",
                        "started_at": "2026-07-20T09:30:00+00:00",
                        "duration_ms": duration_ms,
                    },
                )
            ],
        )

    assert compare_trace_results(result(100.0, "1" * 16), result(101.0, "1" * 16)) == []
    mismatches = compare_trace_results(result(100.0, "1" * 16), result(200.0, "1" * 16))
    assert any("duration" in item for item in mismatches)
    mismatches = compare_trace_results(result(100.0, "1" * 16), result(100.0, "2" * 16))
    assert any("span_id" in item for item in mismatches)


def test_cleanup_is_scoped_to_unique_project(tmp_path, monkeypatch):
    commands: list[list[str]] = []

    def runner(args, timeout=120.0):
        commands.append(list(args))
        if "inspect" in args:
            return _completed(stdout=json.dumps([f"grafana/otel-lgtm@{_DIGEST}"]))
        return _completed(stdout="29.0.0")

    monkeypatch.setattr(tempo_acceptance, "_wait_ready", lambda sleep: None)
    monkeypatch.setattr(tempo_acceptance, "_replay", lambda spans, mapping: None)
    monkeypatch.setattr(tempo_acceptance, "_wait_ingested", lambda spans, mapping, sleep: None)

    from backend.providers.local_package import FileTraceProvider

    package = build_synthetic_package(tmp_path / "package")
    file_provider = FileTraceProvider(package)

    class _FakeTempo:
        def __init__(self, *args, **kwargs):
            pass

        def collect(self, event, query=None):
            return file_provider.collect(event, query)

    monkeypatch.setattr(tempo_acceptance, "TempoTraceProvider", _FakeTempo)

    report = run_gate(
        tmp_path / "gate",
        package=package,
        runner=runner,
        port_free=lambda port: True,
        image_digest=_DIGEST,
    )

    assert report.status == "passed"
    assert report.project is not None and report.project.startswith("tempo-gate-")
    down_commands = [cmd for cmd in commands if "down" in cmd]
    assert len(down_commands) == 1
    down = down_commands[0]
    assert report.project in down
    assert "-v" in down and "--remove-orphans" in down
    assert report.replay_manifest["offset_ms"] == tempo_acceptance.REPLAY_OFFSET_MS
