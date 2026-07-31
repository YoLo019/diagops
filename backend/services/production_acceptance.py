from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

ScenarioName = Literal[
    "deployment_regression",
    "dependency_timeout",
    "traffic_spike",
    "healthy_control",
]
ScenarioNameV2 = Literal[
    "deployment_regression",
    "dependency_timeout",
    "traffic_spike",
    "healthy_control",
    "memory_pressure",
    "network_corruption",
    "process_failure",
]

_EXPECTED_CAUSES = {
    "deployment_regression": "deployment_regression",
    "dependency_timeout": "downstream_dependency_failure",
    "traffic_spike": "traffic_spike",
}
# 新三场景要求 Top-1 cause/component/reason 全部命中且 onset 来自真实异常段。
_EXPECTED_ATTRIBUTIONS = {
    "memory_pressure": (
        "resource_saturation",
        "checkout-service",
        "container memory load",
    ),
    "network_corruption": (
        "network_fault",
        "checkout-service",
        "network packet corruption",
    ),
    "process_failure": (
        "process_or_container_failure",
        "checkout-service",
        "container process failure",
    ),
}
# onset fallback 检查只针对各场景的 canonical signal。
_ONSET_SIGNALS = {
    "memory_pressure": "memory",
    "network_corruption": "network_corruption",
    "process_failure": "process",
}
_ONSET_TOLERANCE_SECONDS = 60.0

# answer-leak 扫描：scenario 标识与控制面 token 不得出现在持久化输入中。
_SCENARIO_TOKENS = frozenset({*_EXPECTED_CAUSES, *_EXPECTED_ATTRIBUTIONS, "healthy_control"})
_CONTROL_PLANE_TOKENS = ("/fault/", "activated_at")


class ProductionScenarioResult(BaseModel):
    """schema V1 历史场景结果；保留以读取四场景历史 artifact。"""

    model_config = ConfigDict(extra="forbid")

    scenario: ScenarioName
    passed: bool
    alert_source: str = Field(min_length=1, max_length=32)
    investigation_id: str = Field(pattern=r"^inv-[a-zA-Z0-9-]+$")
    top_cause_type: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1)
    evidence_reference_validity: float = Field(ge=0, le=1)
    read_only_violations: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)


class ProductionScenarioResultV2(BaseModel):
    """schema V2 场景结果，新增 allowlisted attribution 与 onset 字段。"""

    model_config = ConfigDict(extra="forbid")

    scenario: ScenarioNameV2
    passed: bool
    alert_source: str = Field(min_length=1, max_length=32)
    investigation_id: str = Field(pattern=r"^inv-[a-zA-Z0-9-]+$")
    top_cause_type: str = Field(min_length=1, max_length=80)
    confidence: float = Field(ge=0, le=1)
    evidence_reference_validity: float = Field(ge=0, le=1)
    read_only_violations: int = Field(ge=0)
    duration_seconds: float = Field(ge=0)
    component: str | None = Field(default=None, min_length=1, max_length=128)
    reason: str | None = Field(default=None, min_length=1, max_length=128)
    occurred_at: datetime | None = None
    onset_error_seconds: float | None = Field(default=None, ge=0)
    onset_available: bool = True
    privacy_scan_passed: bool


class ProductionAcceptanceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    scenarios: list[ProductionScenarioResult]
    total_duration_seconds: float = Field(ge=0)
    privacy_scan_passed: bool


class ProductionAcceptanceArtifactV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    scenarios: list[ProductionScenarioResultV2]
    total_duration_seconds: float = Field(ge=0)
    privacy_scan_passed: bool


def validate_acceptance_artifact(value: object) -> list[str]:
    """验证 production Gate 的 allowlisted 汇总工件；V1/V2 均可读。"""
    if not isinstance(value, dict):
        return ["production acceptance artifact schema is invalid"]
    if value.get("schema_version") == 1:
        return _validate_v1(value)
    if value.get("schema_version") == 2:
        return _validate_v2(value)
    return ["production acceptance artifact schema is invalid"]


def _validate_v1(value: object) -> list[str]:
    try:
        artifact = ProductionAcceptanceArtifact.model_validate(value)
    except ValidationError:
        return ["production acceptance artifact schema is invalid"]

    failures: list[str] = []
    by_name = {item.scenario: item for item in artifact.scenarios}
    expected_names = {*_EXPECTED_CAUSES, "healthy_control"}
    if len(artifact.scenarios) != 4 or set(by_name) != expected_names:
        failures.append("production acceptance requires exactly four scenarios")
    failures.extend(_validate_top_causes(by_name, _EXPECTED_CAUSES))
    _validate_healthy_control(by_name.get("healthy_control"), failures)
    _validate_common(
        failures,
        artifact.scenarios,
        artifact.total_duration_seconds,
        artifact.privacy_scan_passed,
    )
    return failures


def _validate_v2(value: object) -> list[str]:
    try:
        artifact = ProductionAcceptanceArtifactV2.model_validate(value)
    except ValidationError:
        return ["production acceptance artifact schema is invalid"]

    failures: list[str] = []
    by_name = {item.scenario: item for item in artifact.scenarios}
    expected_names = {*_EXPECTED_CAUSES, *_EXPECTED_ATTRIBUTIONS, "healthy_control"}
    if len(artifact.scenarios) != 7 or set(by_name) != expected_names:
        failures.append("production acceptance requires exactly seven scenarios")
    failures.extend(
        _validate_top_causes(
            by_name,
            {
                scenario: attribution[0]
                for scenario, attribution in _EXPECTED_ATTRIBUTIONS.items()
            },
        )
    )
    failures.extend(_validate_top_causes(by_name, _EXPECTED_CAUSES))
    for scenario, (_, component, reason) in _EXPECTED_ATTRIBUTIONS.items():
        result = by_name.get(scenario)
        if result is None:
            continue
        if result.component != component:
            failures.append(f"{scenario} component must be {component}")
        if result.reason != reason:
            failures.append(f"{scenario} reason must be {reason}")
        if not result.onset_available:
            failures.append(f"{scenario} onset must be segment-derived, fallback is forbidden")
        if (
            result.onset_error_seconds is None
            or result.onset_error_seconds > _ONSET_TOLERANCE_SECONDS
        ):
            failures.append(f"{scenario} onset error must be within 60 seconds")
    _validate_healthy_control(by_name.get("healthy_control"), failures)
    _validate_common(
        failures,
        artifact.scenarios,
        artifact.total_duration_seconds,
        artifact.privacy_scan_passed,
    )
    return failures


def _validate_top_causes(
    by_name: dict[str, object],
    expected_causes: dict[str, str],
) -> list[str]:
    failures = []
    for scenario, expected_cause in expected_causes.items():
        result = by_name.get(scenario)
        if result is None:
            continue
        if result.top_cause_type != expected_cause:
            failures.append(f"{scenario} Top-1 must be {expected_cause}")
        if result.alert_source != "alertmanager":
            failures.append(f"{scenario} must originate from Alertmanager")
    return failures


def _validate_healthy_control(healthy: object | None, failures: list[str]) -> None:
    if healthy is not None and healthy.top_cause_type != "unknown" and healthy.confidence >= 0.5:
        failures.append("healthy_control must be UNKNOWN or confidence below 0.5")


def _validate_common(
    failures: list[str],
    scenarios: list[object],
    total_duration_seconds: float,
    privacy_scan_passed: bool,
) -> None:
    if any(not item.passed for item in scenarios):
        failures.append("at least one production scenario failed")
    if any(item.evidence_reference_validity != 1 for item in scenarios):
        failures.append("Evidence reference validity must be 100%")
    if any(item.read_only_violations for item in scenarios):
        failures.append("read-only violations must be zero")
    if total_duration_seconds >= 900:
        failures.append("production acceptance must finish below 900 seconds")
    if not privacy_scan_passed:
        failures.append("production acceptance privacy scan must pass")


def aggregate_scenario_results(
    paths: list[Path],
    output: Path,
) -> ProductionAcceptanceArtifactV2:
    """汇总七个独立 Compose project 的安全场景结果为 schema V2 artifact。"""
    scenarios = [
        ProductionScenarioResultV2.model_validate_json(path.read_text(encoding="utf-8"))
        for path in paths
    ]
    artifact = ProductionAcceptanceArtifactV2(
        scenarios=scenarios,
        total_duration_seconds=sum(item.duration_seconds for item in scenarios),
        privacy_scan_passed=all(item.privacy_scan_passed for item in scenarios),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(artifact.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    failures = validate_acceptance_artifact(artifact.model_dump(mode="json"))
    if failures:
        raise ValueError("; ".join(failures))
    return artifact


def run_scenario(
    scenario: ScenarioNameV2,
    *,
    app_url: str,
    dependency_url: str,
    diagops_url: str,
    alertmanager_url: str,
    requester: Callable[..., object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> ProductionScenarioResultV2:
    """运行一个隔离的真实栈场景并只返回 allowlisted 验收字段。"""
    requester = requester or _request_json
    started = time.perf_counter()
    for url in (
        f"{app_url}/health",
        f"{dependency_url}/health",
        f"{diagops_url}/health",
        f"{alertmanager_url}/-/ready",
    ):
        _wait_ready(url, requester, sleeper)

    before = {
        str(item["id"])
        for item in requester("GET", f"{diagops_url}/investigations")
        if isinstance(item, dict) and "id" in item
    }
    requester("POST", f"{app_url}/fault/reset")
    requester("POST", f"{dependency_url}/fault/reset")
    _drive_requests(app_url, 12, 0.5, requester, sleeper)

    activated_at: datetime | None = None
    if scenario == "deployment_regression":
        requester("POST", f"{app_url}/fault/deployment")
        _drive_requests(app_url, 100, 0.1, requester, sleeper)
    elif scenario == "dependency_timeout":
        requester("POST", f"{dependency_url}/fault/timeout")
        _drive_requests(app_url, 24, 0.1, requester, sleeper)
    elif scenario == "traffic_spike":
        requester("POST", f"{app_url}/fault/traffic")
        _drive_requests(app_url, 300, 0.01, requester, sleeper)
    elif scenario in _EXPECTED_ATTRIBUTIONS:
        fault = requester("POST", f"{app_url}/fault/{scenario}")
        activated_at = _parse_timestamp(
            fault.get("activated_at") if isinstance(fault, dict) else None
        )
        _drive_requests(app_url, 24, 0.1, requester, sleeper)
    else:
        requester(
            "POST",
            f"{alertmanager_url}/api/v2/alerts",
            [
                {
                    "labels": {
                        "service": "checkout-service",
                        "environment": "prod",
                        "severity": "warning",
                    },
                    "annotations": {
                        "summary": "service degradation detected",
                        "description": "service degradation detected",
                    },
                    "startsAt": datetime.now(UTC).isoformat(),
                }
            ],
        )

    investigation_id = _wait_new_investigation(
        diagops_url,
        before,
        requester,
        sleeper,
    )
    detail = requester(
        "GET",
        f"{diagops_url}/investigations/{investigation_id}",
    )
    tool_calls = requester(
        "GET",
        f"{diagops_url}/investigations/{investigation_id}/tool-calls",
    )
    review = requester(
        "GET",
        f"{diagops_url}/investigations/{investigation_id}/coordination-review",
    )
    if not isinstance(detail, dict) or not isinstance(tool_calls, list):
        raise ValueError("production investigation response is invalid")

    evidence = detail.get("evidence", [])
    evidence_ids = {str(item["id"]) for item in evidence if isinstance(item, dict) and "id" in item}
    hypotheses = detail.get("hypotheses", [])
    references = [
        str(evidence_id)
        for hypothesis in hypotheses
        if isinstance(hypothesis, dict)
        for evidence_id in hypothesis.get("supporting_evidence_ids", [])
    ]
    references.extend(
        str(evidence_id)
        for call in tool_calls
        if isinstance(call, dict)
        for evidence_id in call.get("output_evidence_ids", [])
    )
    invalid_references = sum(item not in evidence_ids for item in references)
    validity = (len(references) - invalid_references) / len(references) if references else 1
    read_only_tools = {
        "read_logs",
        "query_metrics",
        "read_deployments",
        "read_service_catalog",
        "query_prometheus",
        "query_dependencies",
        "lookup_memory",
    }
    read_only_violations = sum(
        not isinstance(call, dict) or call.get("tool_name") not in read_only_tools
        for call in tool_calls
    )
    top = hypotheses[0] if hypotheses and isinstance(hypotheses[0], dict) else {}
    top_cause = str(top.get("cause_type", "unknown"))
    confidence = float(top.get("confidence", 0))
    event = detail.get("event")
    alert_source = (
        "alertmanager"
        if isinstance(event, dict) and event.get("source") == "webhook"
        else "invalid"
    )

    # authoritative attribution 来自只读 coordination-review；activated_at
    # 仅在本 runner 内计算 onset error，不写入 DiagOps。
    root_causes = review.get("root_causes", []) if isinstance(review, dict) else []
    top_root = root_causes[0] if root_causes and isinstance(root_causes[0], dict) else {}
    component = (
        str(top_root["root_cause_component"])
        if isinstance(top_root.get("root_cause_component"), str)
        else None
    )
    reason = (
        str(top_root["root_cause_reason"])
        if isinstance(top_root.get("root_cause_reason"), str)
        else None
    )
    occurred_at = _parse_timestamp(top_root.get("root_cause_occurred_at"))
    onset_error_seconds = None
    if occurred_at is not None and activated_at is not None:
        onset_error_seconds = abs((occurred_at - activated_at).total_seconds())
    onset_available = _onset_available(scenario, evidence)
    privacy_scan_passed = _privacy_scan(event, evidence, review)

    expected = _EXPECTED_CAUSES.get(scenario)
    expected_attribution = _EXPECTED_ATTRIBUTIONS.get(scenario)
    if expected_attribution is not None:
        expected_cause, expected_component, expected_reason = expected_attribution
        diagnosis_passed = (
            top_cause == expected_cause
            and component == expected_component
            and reason == expected_reason
            and onset_available
            and onset_error_seconds is not None
            and onset_error_seconds <= _ONSET_TOLERANCE_SECONDS
        )
    elif expected is not None:
        diagnosis_passed = top_cause == expected
    else:
        diagnosis_passed = top_cause == "unknown" or confidence < 0.5
    return ProductionScenarioResultV2(
        scenario=scenario,
        passed=(
            detail.get("status") == "completed"
            and alert_source == "alertmanager"
            and diagnosis_passed
            and validity == 1
            and read_only_violations == 0
            and privacy_scan_passed
        ),
        alert_source=alert_source,
        investigation_id=investigation_id,
        top_cause_type=top_cause,
        confidence=confidence,
        evidence_reference_validity=validity,
        read_only_violations=read_only_violations,
        duration_seconds=time.perf_counter() - started,
        component=component,
        reason=reason,
        occurred_at=occurred_at,
        onset_error_seconds=onset_error_seconds,
        onset_available=onset_available,
        privacy_scan_passed=privacy_scan_passed,
    )


def _onset_available(scenario: str, evidence: list[object]) -> bool:
    signal = _ONSET_SIGNALS.get(scenario)
    if signal is None:
        return True
    relevant = [
        item
        for item in evidence
        if isinstance(item, dict)
        and isinstance(item.get("payload"), dict)
        and item["payload"].get("signal_type") == signal
    ]
    return bool(relevant) and not any(
        item["payload"].get("onset_unavailable") for item in relevant
    )


def _privacy_scan(event: object, evidence: object, review: object) -> bool:
    """持久化 Event/Evidence 不得含 scenario 标识或控制面 token；Review 不得含控制面 token。"""
    input_text = json.dumps(
        {"event": event, "evidence": evidence},
        ensure_ascii=False,
        default=str,
    ).casefold()
    if any(
        token in input_text
        for token in (*_SCENARIO_TOKENS, *_CONTROL_PLANE_TOKENS)
    ):
        return False
    review_text = json.dumps(review or {}, ensure_ascii=False, default=str).casefold()
    return not any(token in review_text for token in _CONTROL_PLANE_TOKENS)


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _wait_ready(
    url: str,
    requester: Callable[..., object],
    sleeper: Callable[[float], None],
) -> None:
    for _ in range(60):
        try:
            requester("GET", url, timeout=3)
            return
        except OSError:
            sleeper(1)
    raise TimeoutError("production service did not become ready")


def _drive_requests(
    app_url: str,
    count: int,
    pause: float,
    requester: Callable[..., object],
    sleeper: Callable[[float], None],
) -> None:
    for _ in range(count):
        requester("GET", f"{app_url}/work")
        sleeper(pause)


def _wait_new_investigation(
    diagops_url: str,
    before: set[str],
    requester: Callable[..., object],
    sleeper: Callable[[float], None],
) -> str:
    for _ in range(90):
        investigations = requester("GET", f"{diagops_url}/investigations")
        if isinstance(investigations, list):
            for item in investigations:
                if (
                    isinstance(item, dict)
                    and (investigation_id := str(item.get("id", "")))
                    and investigation_id not in before
                ):
                    return investigation_id
        sleeper(2)
    raise TimeoutError("production investigation was not created")


def _request_json(
    method: str,
    url: str,
    payload: object | None = None,
    timeout: float = 10,
) -> object:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        body = response.read().decode("utf-8")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def _run_from_environment() -> ProductionScenarioResultV2:
    scenario = os.environ["DIAGOPS_PRODUCTION_SCENARIO"]
    result = run_scenario(
        scenario,
        app_url=os.environ["DIAGOPS_LAB_APP_URL"].rstrip("/"),
        dependency_url=os.environ["DIAGOPS_LAB_DEPENDENCY_URL"].rstrip("/"),
        diagops_url=os.environ["DIAGOPS_URL"].rstrip("/"),
        alertmanager_url=os.environ["DIAGOPS_ALERTMANAGER_URL"].rstrip("/"),
    )
    output = Path(os.environ["DIAGOPS_PRODUCTION_RESULT"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("scenario_results", nargs="+", type=Path)
    aggregate.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()

    if arguments.command == "run":
        result = _run_from_environment()
        print(result.model_dump_json())
        if not result.passed:
            raise SystemExit(1)
        return
    artifact = aggregate_scenario_results(
        arguments.scenario_results,
        arguments.output,
    )
    print(artifact.model_dump_json())


if __name__ == "__main__":
    main()
