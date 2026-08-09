"""local incident package 加载器与 Provider 状态矩阵。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.domain.evidence import EvidenceKind, EvidenceSourceClass, SpanStatus
from backend.domain.tool_queries import (
    DependencyDirection,
    DependencyQuery,
    LogQuery,
    RuntimeStateQuery,
    RuntimeStateValue,
    TraceDirection,
    TraceQuery,
)
from backend.providers.local_package import (
    FileTraceProvider,
    LocalIncidentPackage,
    PackageDependencyProvider,
    PackageLogProvider,
    PackageRuntimeStateProvider,
    PackageSourceError,
    build_package_providers,
    derive_span_edges,
    load_incident_event,
)
from backend.providers.results import ProviderStatus
from backend.services.offline_tool_acceptance import build_synthetic_package

_STARTED = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


@pytest.fixture
def package(tmp_path):
    return build_synthetic_package(tmp_path / "package")


@pytest.fixture
def event(package):
    return load_incident_event(package)


def _window(**overrides):
    return {
        "start_time": _STARTED - timedelta(minutes=30),
        "end_time": _STARTED + timedelta(minutes=30),
        "reason": "验证 package provider",
        **overrides,
    }


def test_manifest_and_envelope_load(package, event):
    assert package.package_id == "synthetic-offline-gate"
    assert package.source_class == EvidenceSourceClass.SYNTHETIC_FIXTURE
    assert set(package.sources) == {
        "logs",
        "metrics",
        "spans",
        "catalog",
        "deployments",
        "runtime_state",
        "related_alerts",
    }
    assert event.service == "checkout-service"


def test_missing_manifest_fails_visibly(tmp_path):
    with pytest.raises(PackageSourceError):
        LocalIncidentPackage.load(tmp_path / "no-such-package")


def test_log_provider_filters_and_redacts(package, event):
    provider = PackageLogProvider(package)
    result = provider.collect(event, LogQuery(**_window(), keywords=["timeout"]))

    assert result.status == ProviderStatus.SUCCESS
    assert len(result.evidence_items) == 1
    item = result.evidence_items[0]
    assert "sk-testsecretvalue1234567890" not in json.dumps(item.payload)
    assert item.provenance is not None
    assert item.provenance.source_artifact_hash is not None


def test_absent_source_is_explicit_skip(package, event):
    sources = dict(package.sources)
    source = sources["logs"]
    sources["logs"] = type(source)(file=source.file, present=False)
    absent = LocalIncidentPackage(
        root=package.root,
        package_id=package.package_id,
        source_class=package.source_class,
        sources=sources,
        artifact_hashes=package.artifact_hashes,
    )

    result = PackageLogProvider(absent).collect(event)

    assert result.status == ProviderStatus.SKIPPED
    assert "absent" in result.error_message


def test_missing_file_is_failed_not_mock(package, event):
    (package.root / "logs.json").unlink()

    result = PackageLogProvider(package).collect(event)

    assert result.status == ProviderStatus.FAILED


def test_hash_mismatch_is_failed(package, event):
    (package.root / "logs.json").write_text("[]", encoding="utf-8")

    result = PackageLogProvider(package).collect(event)

    assert result.status == ProviderStatus.FAILED


def test_malformed_rows_yield_partial_with_valid_items(package, event):
    rows = json.loads((package.root / "logs.json").read_text(encoding="utf-8"))
    rows.insert(0, {"__invalid__": True})
    text = json.dumps(rows, ensure_ascii=False, indent=2)
    (package.root / "logs.json").write_text(text, encoding="utf-8")
    import hashlib

    package.artifact_hashes["logs"] = hashlib.sha256(
        (package.root / "logs.json").read_bytes()
    ).hexdigest()

    result = PackageLogProvider(package).collect(event)

    assert result.status == ProviderStatus.PARTIAL
    assert result.evidence_items
    assert "malformed" in result.error_message


def test_all_rows_invalid_is_failed(package, event):
    text = json.dumps([{"__invalid__": True}], ensure_ascii=False)
    (package.root / "logs.json").write_text(text, encoding="utf-8")
    import hashlib

    package.artifact_hashes["logs"] = hashlib.sha256(
        (package.root / "logs.json").read_bytes()
    ).hexdigest()

    result = PackageLogProvider(package).collect(event)

    assert result.status == ProviderStatus.FAILED


def test_trace_provider_direction_and_filters(package, event):
    provider = FileTraceProvider(package)
    query = TraceQuery(
        service="payment-service",
        direction=TraceDirection.UPSTREAM,
        window_start=_STARTED - timedelta(minutes=30),
        window_end=_STARTED + timedelta(minutes=30),
    )

    result = provider.collect(event, query)

    assert result.status == ProviderStatus.SUCCESS
    services = [item.payload["service"] for item in result.evidence_items]
    # upstream 扩展只保留 payment 与其祖先 checkout，不含下游 ledger。
    assert "payment-service" in services
    assert "checkout-service" in services
    assert "ledger-service" not in services


def test_trace_provider_error_only_and_min_duration(package, event):
    provider = FileTraceProvider(package)
    result = provider.collect(
        event,
        TraceQuery(
            error_only=True,
            min_duration_ms=800.0,
            window_start=_STARTED - timedelta(minutes=30),
            window_end=_STARTED + timedelta(minutes=30),
        ),
    )

    payloads = [item.payload for item in result.evidence_items]
    assert payloads and all(
        payload["status"] == SpanStatus.ERROR.value and payload["duration_ms"] >= 800.0
        for payload in payloads
    )
    assert {item.kind for item in result.evidence_items} == {EvidenceKind.TRACE_ERROR}


def test_runtime_state_excludes_healthy_by_default(package, event):
    provider = PackageRuntimeStateProvider(package)

    result = provider.collect(event)

    states = [item.payload["state"] for item in result.evidence_items]
    assert states == [RuntimeStateValue.RESTARTING.value]

    result = provider.collect(event, RuntimeStateQuery(include_healthy=True))
    states = sorted(item.payload["state"] for item in result.evidence_items)
    assert states == [RuntimeStateValue.HEALTHY.value, RuntimeStateValue.RESTARTING.value]


def test_dependency_edges_derive_only_from_span_parenting(package, event):
    provider = PackageDependencyProvider(package)
    result = provider.collect(
        event,
        DependencyQuery(
            **_window(),
            direction=DependencyDirection.DOWNSTREAM,
            target="checkout-service",
        ),
    )

    assert result.status == ProviderStatus.SUCCESS
    edges = result.evidence_items[0].payload["edges"]
    dynamic = {tuple(edge[key] for key in ("parent", "child")) for edge in edges}
    assert ("checkout-service", "payment-service") in dynamic

    upstream = provider.collect(
        event,
        DependencyQuery(
            **_window(),
            direction=DependencyDirection.UPSTREAM,
            target="ledger-service",
        ),
    )
    upstream_edges = upstream.evidence_items[0].payload["edges"]
    assert any(edge["parent"] == "payment-service" for edge in upstream_edges)


def test_derive_span_edges_ignores_same_service_and_missing_parent():
    from backend.domain.evidence import TraceSpanPayload

    spans = [
        TraceSpanPayload(
            trace_id="a" * 32,
            span_id="1" * 16,
            parent_span_id=None,
            service="svc-a",
            operation="root",
            started_at=_STARTED,
            duration_ms=1.0,
        ),
        TraceSpanPayload(
            trace_id="a" * 32,
            span_id="2" * 16,
            parent_span_id="1" * 16,
            service="svc-a",
            operation="inner",
            started_at=_STARTED,
            duration_ms=1.0,
        ),
        TraceSpanPayload(
            trace_id="a" * 32,
            span_id="3" * 16,
            parent_span_id="9" * 16,
            service="svc-b",
            operation="orphan",
            started_at=_STARTED,
            duration_ms=1.0,
        ),
    ]

    assert derive_span_edges(spans) == set()


def test_all_package_providers_have_unique_supported_tools(package):
    providers = build_package_providers(package)
    tools = [tool for provider in providers for tool in provider.supported_tools]
    assert len(tools) == len(set(tools)) == 8
