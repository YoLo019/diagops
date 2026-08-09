"""九工具离线验收：通过正常 ToolRegistry 调用所有 Agent 工具并输出哈希矩阵。

边界（spec 7.7/R22）：全程离线、无凭证、无 Docker、无外部服务；每个工具
必须对 prepared package 产出至少一条非空 success，empty/malformed/missing/
timeout/redaction/provenance/scope/idempotency 是独立行，不能顶替 success。
synthetic fixture 仅证明工具契约，不构成生产准确性证据。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.agent_findings import CoordinationReview, RootCauseCandidate
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceSourceClass,
    EvidenceStatus,
)
from backend.domain.memory import (
    MemoryItem,
    MemoryType,
    MemoryVerificationStatus,
)
from backend.domain.tool_calls import ToolCallStatus
from backend.providers.local_package import (
    LocalIncidentPackage,
    PackageRows,
    PackageSourceError,
    PackageTimeoutError,
    build_package_providers,
    load_incident_event,
)
from backend.providers.registry import ProviderRegistry
from backend.providers.results import ProviderStatus
from backend.safety.redaction import assert_safe_value
from backend.tools.provider_tools import VerifiedMemoryLookup, build_provider_tool_registry

ACCEPTANCE_VERSION = "offline-tool-acceptance-v1"
PACKAGE_ID = "synthetic-offline-gate"

# spec 7.5 冻结的九个 Agent 可见只读工具；与 registry manifest 对账。
EXPECTED_AGENT_TOOLS = (
    "lookup_memory",
    "query_dependencies",
    "query_metrics",
    "query_related_alerts",
    "query_traces",
    "read_deployments",
    "read_logs",
    "read_runtime_state",
    "read_service_catalog",
)

_FILE_BACKED_TOOLS = {
    "read_logs": "logs",
    "query_metrics": "metrics",
    "query_traces": "spans",
    "read_service_catalog": "catalog",
    "read_deployments": "deployments",
    "read_runtime_state": "runtime_state",
    "query_related_alerts": "related_alerts",
    "query_dependencies": "spans",
}


def build_synthetic_package(root: Path) -> LocalIncidentPackage:
    """生成覆盖全部 source 的 synthetic package，并回填真实 artifact 哈希。"""
    root.mkdir(parents=True, exist_ok=True)
    base = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)
    files: dict[str, object] = {
        "incident.json": {
            "service": "checkout-service",
            "environment": "prod",
            "severity": "critical",
            "title": "checkout 5xx spike",
            "description": "synthetic offline gate incident",
            "started_at": base.isoformat(),
            "time_window_minutes": 30,
        },
        "logs.json": [
            {
                "timestamp": (base + timedelta(minutes=1)).isoformat(),
                "level": "ERROR",
                "message": "timeout calling payment-service api_key=sk-testsecretvalue1234567890",
                "service": "checkout-service",
                "entity_id": "checkout-1",
            },
            {
                "timestamp": (base + timedelta(minutes=2)).isoformat(),
                "level": "WARN",
                "message": "retrying payment-service request",
                "service": "checkout-service",
                "entity_id": "checkout-1",
            },
        ],
        "metrics.json": [
            {
                "name": "error_rate",
                "entity_id": "checkout-1",
                "unit": "ratio",
                "points": [
                    {"timestamp": (base - timedelta(minutes=5)).isoformat(), "value": 0.01},
                    {"timestamp": base.isoformat(), "value": 0.42},
                    {"timestamp": (base + timedelta(minutes=5)).isoformat(), "value": 0.55},
                ],
            }
        ],
        "spans.json": [
            {
                "trace_id": "a" * 32,
                "span_id": "1" * 16,
                "parent_span_id": None,
                "service": "checkout-service",
                "operation": "POST /checkout",
                "started_at": base.isoformat(),
                "duration_ms": 820.0,
                "status": "error",
                "attributes": {"http.status_code": "500"},
            },
            {
                "trace_id": "a" * 32,
                "span_id": "2" * 16,
                "parent_span_id": "1" * 16,
                "service": "payment-service",
                "operation": "POST /pay",
                "started_at": (base + timedelta(milliseconds=10)).isoformat(),
                "duration_ms": 790.0,
                "status": "error",
                "attributes": {"http.status_code": "504"},
            },
            {
                "trace_id": "a" * 32,
                "span_id": "3" * 16,
                "parent_span_id": "2" * 16,
                "service": "ledger-service",
                "operation": "GET /balance",
                "started_at": (base + timedelta(milliseconds=20)).isoformat(),
                "duration_ms": 12.0,
                "status": "ok",
                "attributes": {},
            },
        ],
        "catalog.json": {
            "services": [
                {
                    "name": "checkout-service",
                    "owner": "team-checkout",
                    "dependencies": ["payment-service"],
                },
                {
                    "name": "payment-service",
                    "owner": "team-pay",
                    "dependencies": ["ledger-service"],
                },
                {"name": "ledger-service", "owner": "team-ledger", "dependencies": []},
            ]
        },
        "deployments.json": [
            {
                "service": "payment-service",
                "version": "v2.4.1",
                "deployed_at": (base - timedelta(minutes=10)).isoformat(),
                "environment": "prod",
            }
        ],
        "runtime_state.json": [
            {
                "entity_id": "payment-2",
                "runtime_kind": "container",
                "state": "restarting",
                "reason": "OOMKilled after deploy",
                "observed_at": (base - timedelta(minutes=3)).isoformat(),
                "restart_count": 4,
                "ready": False,
            },
            {
                "entity_id": "checkout-1",
                "runtime_kind": "container",
                "state": "healthy",
                "reason": "ready",
                "observed_at": base.isoformat(),
                "restart_count": 0,
                "ready": True,
            },
        ],
        "related_alerts.json": [
            {
                "fingerprint": "fp-payment-5xx",
                "name": "PaymentHigh5xx",
                "entity_id": "payment-service",
                "severity": "critical",
                "status": "firing",
                "starts_at": (base - timedelta(minutes=2)).isoformat(),
                "ends_at": None,
                "labels": {"team": "team-pay"},
            }
        ],
    }
    sources: dict[str, dict[str, object]] = {}
    hashes: dict[str, str] = {}
    for name, payload in files.items():
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        (root / name).write_text(text, encoding="utf-8")
        if name == "incident.json":
            continue
        source = name.removesuffix(".json")
        sources[source] = {"file": name, "present": True}
        # 以落盘字节为准计算哈希，避免平台换行转换造成不一致。
        hashes[source] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    manifest = {
        "package_id": PACKAGE_ID,
        "source_class": EvidenceSourceClass.SYNTHETIC_FIXTURE.value,
        "sources": sources,
        "artifact_hashes": hashes,
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return LocalIncidentPackage.load(root)


def seed_verified_memory(
    repository: InMemoryInvestigationRepository,
    event: IncidentEvent,
) -> None:
    """为 lookup_memory 的 success 行准备一条带完整 provenance 的 verified 记录。"""
    source_event = event.model_copy(update={"title": "historical checkout incident"})
    source_record = repository.save(
        _investigation_record(source_event, investigation_id="inv-source-verified")
    )
    candidate = RootCauseCandidate(
        id="candidate-payment-timeout",
        affected_entity="payment-service",
        failure_mechanism="payment-service timeout after deploy",
        summary="payment-service deploy caused checkout timeouts",
        rank=1,
        confidence=0.9,
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=source_record.id,
            candidates=[candidate],
            summary="verified historical root cause",
        )
    )
    repository.save_memory_items(
        [
            MemoryItem(
                id="mem-verified-payment",
                service=event.service,
                environment=event.environment,
                memory_type=MemoryType.INVESTIGATION_SUMMARY,
                summary="checkout 5xx traced to payment-service deploy v2.4.0",
                source_investigation_id=source_record.id,
                verification_status=MemoryVerificationStatus.VERIFIED,
                verified_at=event.started_at - timedelta(days=7),
                verified_by="operator-review",
                root_candidate_id=candidate.id,
                created_at=event.started_at - timedelta(days=7),
            )
        ]
    )


def _investigation_record(event: IncidentEvent, *, investigation_id: str):
    return InvestigationRecord(id=investigation_id, event=event)


def _query_input(tool_name: str, event: IncidentEvent, *, empty: bool = False) -> dict:
    start = event.started_at - timedelta(minutes=30)
    end = event.started_at + timedelta(minutes=30)
    if tool_name == "lookup_memory":
        return (
            {"affected_entity": "no-such-entity", "limit": 5}
            if empty
            else {"affected_entity": "payment-service", "limit": 5}
        )
    if tool_name in {
        "query_traces",
        "read_runtime_state",
        "query_related_alerts",
    }:
        default_entity = {
            "query_traces": "payment-service",
            "read_runtime_state": "payment-2",
            "query_related_alerts": "payment-service",
        }[tool_name]
        scoped = {
            "entity_ids": ["no-such-entity"] if empty else [default_entity],
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "limit": 20,
        }
        if tool_name == "query_traces" and not empty:
            scoped["service"] = "payment-service"
        return scoped
    windowed = {
        "start_time": start.isoformat(),
        "end_time": end.isoformat(),
        "reason": "offline acceptance gate",
        "limit": 20,
    }
    if empty:
        if tool_name == "read_logs":
            windowed["keywords"] = ["zzz-no-such-keyword"]
        elif tool_name == "query_metrics":
            windowed["metric_names"] = ["no_such_metric"]
        elif tool_name == "read_deployments":
            windowed["version"] = "v0.0.0-nonexistent"
        elif tool_name == "read_service_catalog":
            windowed["name"] = "no-such-service"
        elif tool_name == "query_dependencies":
            windowed["target"] = "no-such-service"
    if tool_name == "query_dependencies" and not empty:
        windowed["target"] = "checkout-service"
    if tool_name == "query_metrics" and not empty:
        windowed["metric_names"] = ["error_rate"]
    return windowed


def run_acceptance(output_dir: Path) -> dict:
    package = build_synthetic_package(output_dir / "package")
    event = load_incident_event(package)
    repository = InMemoryInvestigationRepository()
    seed_verified_memory(repository, event)
    registry = build_provider_tool_registry(
        ProviderRegistry(build_package_providers(package)),
        VerifiedMemoryLookup(repository),
    )

    rows: list[dict] = []
    manifest = registry.agent_manifest()
    rows.append(
        _row(
            "manifest",
            "nine_tool_manifest",
            passed=manifest == EXPECTED_AGENT_TOOLS,
            detail=f"manifest={list(manifest)}",
        )
    )

    results_by_tool: dict[str, dict] = {}
    for tool_name in EXPECTED_AGENT_TOOLS:
        result = registry.invoke_detailed(
            tool_name,
            event=event,
            task_id=f"acceptance-{tool_name}",
            agent_name="OfflineAcceptance",
            input=_query_input(tool_name, event),
        )
        success_evidence = [
            item
            for item in result.evidence
            if item.status == EvidenceStatus.SUCCESS
        ]
        passed = (
            result.call.status == ToolCallStatus.SUCCESS
            and len(success_evidence) >= 1
        )
        results_by_tool[tool_name] = {
            "result": result,
            "success_evidence": success_evidence,
        }
        rows.append(
            _row(
                tool_name,
                "success",
                passed=passed,
                detail=(
                    f"status={result.call.status.value} "
                    f"success_evidence={len(success_evidence)}"
                ),
            )
        )
        rows.extend(_empty_row(registry, tool_name, event))
        rows.extend(
            _provenance_scope_rows(
                tool_name,
                success_evidence,
                expected_class=(
                    EvidenceSourceClass.RECORDED_LOCAL
                    if tool_name == "lookup_memory"
                    else EvidenceSourceClass.SYNTHETIC_FIXTURE
                ),
            )
        )
        rows.extend(_idempotency_row(registry, tool_name, event))

    for tool_name, source_name in sorted(_FILE_BACKED_TOOLS.items()):
        rows.extend(_source_matrix_rows(tool_name, source_name, package, event))
    rows.extend(_redaction_rows(results_by_tool))

    failed = [row for row in rows if not row["passed"]]
    report = {
        "acceptance_version": ACCEPTANCE_VERSION,
        "package_id": package.package_id,
        "source_class": package.source_class.value,
        "agent_manifest": list(manifest),
        "generated_at": datetime.now(UTC).isoformat(),
        "rows": rows,
        "summary": {
            "total_rows": len(rows),
            "failed_rows": len(failed),
            "passed": not failed,
        },
    }
    report["artifact_hash"] = _canonical_hash(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "matrix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def _row(tool: str, condition: str, *, passed: bool, detail: str) -> dict:
    return {
        "tool": tool,
        "condition": condition,
        "passed": bool(passed),
        "detail": detail,
    }


def _empty_row(registry, tool_name: str, event: IncidentEvent) -> list[dict]:
    result = registry.invoke_detailed(
        tool_name,
        event=event,
        task_id=f"acceptance-empty-{tool_name}",
        agent_name="OfflineAcceptance",
        input=_query_input(tool_name, event, empty=True),
    )
    success_evidence = [
        item for item in result.evidence if item.status == EvidenceStatus.SUCCESS
    ]
    return [
        _row(
            tool_name,
            "empty",
            passed=result.call.status == ToolCallStatus.SUCCESS and not success_evidence,
            detail=f"status={result.call.status.value} success_evidence={len(success_evidence)}",
        )
    ]


def _provenance_scope_rows(
    tool_name: str,
    evidence: list[EvidenceItem],
    *,
    expected_class: EvidenceSourceClass,
) -> list[dict]:
    provenance_ok = bool(evidence) and all(
        item.provenance is not None
        and item.provenance.source_class == expected_class
        for item in evidence
    )
    scope_ok = bool(evidence) and all(
        item.scope is not None and len(item.scope.entity_ids) <= 20 for item in evidence
    )
    return [
        _row(
            tool_name,
            "provenance",
            passed=provenance_ok,
            detail=f"all evidence carries {expected_class.value} provenance",
        ),
        _row(
            tool_name,
            "scope",
            passed=scope_ok,
            detail="all evidence carries bounded scope",
        ),
    ]


def _idempotency_row(registry, tool_name: str, event: IncidentEvent) -> list[dict]:
    outputs = []
    for attempt in (1, 2):
        result = registry.invoke_detailed(
            tool_name,
            event=event,
            task_id=f"acceptance-idem-{tool_name}-{attempt}",
            agent_name="OfflineAcceptance",
            input=_query_input(tool_name, event),
        )
        outputs.append(
            (
                result.call.status,
                [item.summary for item in result.evidence],
                [item.payload for item in result.evidence],
            )
        )
    return [
        _row(
            tool_name,
            "idempotency",
            passed=outputs[0] == outputs[1],
            detail="repeated identical call yields identical status/summaries/payloads",
        )
    ]


def _source_matrix_rows(
    tool_name: str,
    source_name: str,
    package: LocalIncidentPackage,
    event: IncidentEvent,
) -> list[dict]:
    rows: list[dict] = []

    # malformed：注入一行无效 + 一行有效 => partial 且保留有效证据。
    malformed_registry = _registry_with_loader(
        package,
        source_name,
        lambda _source: _malformed_rows(package, source_name),
    )
    result = malformed_registry.invoke_detailed(
        tool_name,
        event=event,
        task_id=f"acceptance-malformed-{tool_name}",
        agent_name="OfflineAcceptance",
        input=_query_input(tool_name, event),
    )
    partial = any(
        item.status == ProviderStatus.PARTIAL for item in result.provider_results
    )
    rows.append(
        _row(
            tool_name,
            "malformed",
            passed=result.call.status == ToolCallStatus.SUCCESS and partial,
            detail=f"partial={partial} status={result.call.status.value}",
        )
    )

    # missing file：manifest 声明存在但文件被移除 => failed，绝不回退 mock。
    missing_registry = _registry_with_loader(
        package,
        source_name,
        _raise_missing,
    )
    result = missing_registry.invoke_detailed(
        tool_name,
        event=event,
        task_id=f"acceptance-missing-{tool_name}",
        agent_name="OfflineAcceptance",
        input=_query_input(tool_name, event),
    )
    rows.append(
        _row(
            tool_name,
            "missing",
            passed=result.call.status == ToolCallStatus.FAILED,
            detail=f"status={result.call.status.value}",
        )
    )

    # absent source：manifest 显式缺席 => skipped。
    absent_package = _package_with_absent_source(package, source_name)
    absent_registry = build_provider_tool_registry(
        ProviderRegistry(build_package_providers(absent_package)),
        VerifiedMemoryLookup(InMemoryInvestigationRepository()),
    )
    result = absent_registry.invoke_detailed(
        tool_name,
        event=event,
        task_id=f"acceptance-absent-{tool_name}",
        agent_name="OfflineAcceptance",
        input=_query_input(tool_name, event),
    )
    rows.append(
        _row(
            tool_name,
            "absent_source",
            passed=result.call.status == ToolCallStatus.SKIPPED,
            detail=f"status={result.call.status.value}",
        )
    )

    # timeout：注入超界读取 => failed/timeout 分类。
    timeout_registry = _registry_with_loader(
        package,
        source_name,
        _raise_timeout,
    )
    result = timeout_registry.invoke_detailed(
        tool_name,
        event=event,
        task_id=f"acceptance-timeout-{tool_name}",
        agent_name="OfflineAcceptance",
        input=_query_input(tool_name, event),
    )
    timeout_recorded = any(
        item.status == ProviderStatus.FAILED and item.error_message == "provider timeout"
        for item in result.provider_results
    )
    rows.append(
        _row(
            tool_name,
            "timeout",
            passed=result.call.status == ToolCallStatus.FAILED and timeout_recorded,
            detail=f"timeout_recorded={timeout_recorded}",
        )
    )
    return rows


def _redaction_rows(results_by_tool: dict[str, dict]) -> list[dict]:
    rows: list[dict] = []
    for tool_name in ("read_logs", "query_related_alerts"):
        evidence = results_by_tool[tool_name]["result"].evidence
        try:
            for item in evidence:
                assert_safe_value(item.model_dump(mode="json"))
            leaked = False
        except Exception:
            leaked = True
        rows.append(
            _row(
                tool_name,
                "redaction",
                passed=not leaked,
                detail="evidence payload survives redaction scan",
            )
        )
    return rows


def _registry_with_loader(package, source_name, loader):
    providers = [
        type(provider)(package, row_loader=loader)
        if provider.source_name == source_name
        else provider
        for provider in build_package_providers(package)
    ]
    return build_provider_tool_registry(
        ProviderRegistry(providers),
        VerifiedMemoryLookup(InMemoryInvestigationRepository()),
    )


def _malformed_rows(package: LocalIncidentPackage, source_name: str):
    source = package.sources[source_name]
    raw = json.loads((package.root / source.file).read_text(encoding="utf-8"))
    rows = raw.get("services", raw) if isinstance(raw, dict) else raw
    valid = list(rows)[:1]
    return PackageRows(rows=[{"__invalid__": True}, *valid], malformed=0)


def _raise_missing(_source):
    raise PackageSourceError("source file missing or corrupt")


def _raise_timeout(_source):
    raise PackageTimeoutError("read deadline exceeded")


def _package_with_absent_source(
    package: LocalIncidentPackage, source_name: str
) -> LocalIncidentPackage:
    sources = dict(package.sources)
    source = sources[source_name]
    sources[source_name] = type(source)(file=source.file, present=False)
    return LocalIncidentPackage(
        root=package.root,
        package_id=package.package_id,
        source_class=package.source_class,
        sources=sources,
        artifact_hashes=package.artifact_hashes,
    )


def _canonical_hash(report: dict) -> str:
    payload = {key: value for key, value in report.items() if key != "artifact_hash"}
    canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DiagOps offline nine-tool acceptance")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/offline_tool_acceptance"),
    )
    args = parser.parse_args(argv)
    report = run_acceptance(args.output_dir)
    summary = report["summary"]
    print(
        f"offline tool acceptance: rows={summary['total_rows']} "
        f"failed={summary['failed_rows']} artifact={args.output_dir / 'matrix.json'}"
    )
    for row in report["rows"]:
        if not row["passed"]:
            print(f"FAILED {row['tool']}:{row['condition']} {row['detail']}")
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
