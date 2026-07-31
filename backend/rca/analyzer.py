import math
from collections.abc import Callable

from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.rca.scoring import confidence_from_score, contains_any, parse_percentage

TRAFFIC_SPIKE_QPS_CHANGE = 50.0
NORMAL_QPS_MAX_ABS_CHANGE = 10.0


class RcaAnalyzer:
    def analyze(
        self, event: IncidentEvent, evidence: list[EvidenceItem]
    ) -> list[Hypothesis]:
        evidence = [
            item
            for item in evidence
            if item.status in {EvidenceStatus.SUCCESS, EvidenceStatus.PARTIAL}
            and item.kind != EvidenceKind.PROVIDER_ERROR
        ]
        candidates = [
            self._deployment_regression(event, evidence),
            self._traffic_spike(event, evidence),
            self._dependency_failure(event, evidence),
            self._database_slowdown(event, evidence),
            self._single_instance_issue(event, evidence),
            self._resource_saturation(evidence),
            self._network_fault(evidence),
            self._process_failure(evidence),
        ]
        hypotheses = [candidate for candidate in candidates if candidate is not None]

        if not hypotheses:
            return [
                Hypothesis(
                    cause_type=CauseType.UNKNOWN,
                    summary="No strong root-cause pattern matched the available evidence.",
                    confidence=0.2,
                    next_actions=[
                        "Collect logs, metrics, deploy history, and dependency health data."
                    ],
                )
            ]

        return sorted(
            hypotheses,
            key=lambda item: (-item.confidence, item.cause_type.value),
        )

    def _deployment_regression(
        self, event: IncidentEvent, evidence: list[EvidenceItem]
    ) -> Hypothesis | None:
        deploys = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.DEPLOY
            and item.kind == EvidenceKind.DEPLOYMENT,
        )
        null_pointers = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.LOG
            and item.payload.get("exception") == "NullPointerException",
        )
        deployment_errors = [
            item
            for item in evidence
            if item.provider == EvidenceProvider.LOG
            and item.payload.get("signal_type") in {"error", "timeout"}
            and any(
                item.timestamp >= deploy.timestamp
                and _same_component(deploy, item)
                for deploy in deploys
            )
        ]
        normal_qps_evidence = _normal_qps_evidence(evidence)
        normal_qps = event.signals.get("qps") == "normal" or bool(normal_qps_evidence)
        error_evidence = _unique_evidence([*null_pointers, *deployment_errors])
        has_strong_match = bool(deploys and error_evidence)

        score = 0
        score += 4 if deploys else 0
        score += 3 if error_evidence else 0
        score += 2 if normal_qps else 0
        score += 1 if "deployment" in _event_text(event) else 0

        if not has_strong_match:
            return None

        supporting = [*deploys, *error_evidence]
        supporting.extend(normal_qps_evidence)
        supporting.extend(contains_any(evidence, ["normal range", "qps stayed"]))
        return Hypothesis(
            cause_type=CauseType.DEPLOYMENT_REGRESSION,
            summary="A recent deployment is the strongest match for the new error pattern.",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=_ids(supporting),
            next_actions=[
                "Compare the deployed version with the previous stable version.",
                "Rollback or disable the changed module if error rate remains elevated.",
                "Inspect the exception path and add a regression test for the endpoint.",
            ],
        )

    def _traffic_spike(
        self, event: IncidentEvent, evidence: list[EvidenceItem]
    ) -> Hypothesis | None:
        qps_spikes = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.METRIC
            and item.kind == EvidenceKind.METRIC_TREND
            and (
                _is_large_positive_qps_change(item.payload.get("qps_change"))
                or (
                    item.payload.get("signal_type") == "traffic"
                    and _number(item.payload.get("change_percent"))
                    >= TRAFFIC_SPIKE_QPS_CHANGE
                )
            ),
        )
        summary_fallback = contains_any(evidence, ["qps increased sharply"])

        score = 0
        score += 6 if qps_spikes else 0
        score += 2 if event.signals.get("qps") == "high" else 0
        score += 1 if summary_fallback else 0
        score += 1 if "traffic" in _event_text(event) else 0

        if not qps_spikes:
            return None

        return Hypothesis(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="Traffic growth appears to have preceded the latency and error increase.",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=_ids(qps_spikes),
            next_actions=[
                "Check autoscaling capacity and recent traffic sources.",
                "Apply rate limits or shed non-critical traffic if saturation continues.",
                "Review latency by endpoint to identify the hottest path under load.",
            ],
        )

    def _dependency_failure(
        self, event: IncidentEvent, evidence: list[EvidenceItem]
    ) -> Hypothesis | None:
        dependency_health = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.DEPENDENCY
            and item.kind == EvidenceKind.DEPENDENCY_HEALTH
            and (
                item.payload.get("dependency") is not None
                or item.payload.get("latency_p95") is not None
                or item.payload.get("error_rate") is not None
            ),
        )
        timeout_logs = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.LOG
            and (
                item.payload.get("dependency") is not None
                or item.payload.get("exception") == "TimeoutException"
                or item.payload.get("signal_type") == "timeout"
            ),
        )
        summary_fallback = contains_any(evidence, ["timeout"])
        has_strong_match = bool(dependency_health or timeout_logs)

        score = 0
        score += 4 if dependency_health else 0
        score += 6 if timeout_logs else 0
        score += 2 if event.signals.get("dependency") else 0
        score += 1 if summary_fallback else 0
        score += 1 if "dependency" in _event_text(event) else 0

        if not has_strong_match:
            return None

        return Hypothesis(
            cause_type=CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            summary="A downstream dependency slowdown matches the timeout pattern.",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=_ids([*dependency_health, *timeout_logs]),
            next_actions=[
                "Inspect the downstream service health and recent changes.",
                "Increase timeout budget only if it is safe for callers and capacity.",
                "Enable fallback, circuit breaking, or queueing for the affected dependency path.",
            ],
        )

    def _resource_saturation(
        self, evidence: list[EvidenceItem]
    ) -> Hypothesis | None:
        strong = _filter(
            evidence,
            lambda item: item.payload.get("signal_type")
            in {"cpu", "memory", "disk_io"}
            and _is_strong(item)
            and (
                item.payload.get("change_percent") is None
                or _number(item.payload.get("change_percent")) > 0
            ),
        )
        if not strong:
            return None
        return Hypothesis(
            cause_type=CauseType.RESOURCE_SATURATION,
            summary="Resource saturation is the strongest match for the observed anomaly.",
            confidence=confidence_from_score(6 + min(3, len(strong) - 1)),
            supporting_evidence_ids=_ids(strong),
            next_actions=["Inspect capacity and the workload driving resource pressure."],
        )

    def _network_fault(self, evidence: list[EvidenceItem]) -> Hypothesis | None:
        strong = _filter(
            evidence,
            lambda item: item.payload.get("signal_type")
            in {"network_latency", "network_corruption"}
            and _is_strong(item),
        )
        if not strong:
            return None
        return Hypothesis(
            cause_type=CauseType.NETWORK_FAULT,
            summary="Network degradation matches the observed latency or packet errors.",
            confidence=confidence_from_score(6 + min(3, len(strong) - 1)),
            supporting_evidence_ids=_ids(strong),
            next_actions=["Inspect the affected node and network path."],
        )

    def _process_failure(self, evidence: list[EvidenceItem]) -> Hypothesis | None:
        strong = _filter(
            evidence,
            lambda item: item.payload.get("signal_type") == "process"
            and _is_strong(item),
        )
        if not strong:
            return None
        return Hypothesis(
            cause_type=CauseType.PROCESS_OR_CONTAINER_FAILURE,
            summary="A process or container failure matches the availability loss.",
            confidence=confidence_from_score(6 + min(3, len(strong) - 1)),
            supporting_evidence_ids=_ids(strong),
            next_actions=["Inspect restarts and preserve process diagnostics."],
        )

    def _database_slowdown(
        self, event: IncidentEvent, evidence: list[EvidenceItem]
    ) -> Hypothesis | None:
        database_metrics = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.METRIC
            and item.kind == EvidenceKind.METRIC_TREND
            and item.payload.get("db_p95") is not None,
        )
        summary_fallback = contains_any(evidence, ["database query latency"])
        has_strong_match = bool(database_metrics or event.signals.get("database") == "slow")

        score = 0
        score += 5 if database_metrics else 0
        score += 3 if event.signals.get("database") == "slow" else 0
        score += 1 if summary_fallback else 0
        score += 1 if "database" in _event_text(event) else 0

        if not has_strong_match:
            return None

        return Hypothesis(
            cause_type=CauseType.DATABASE_SLOWDOWN,
            summary="Database query latency appears to be driving service latency.",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=_ids(database_metrics),
            next_actions=[
                "Find the slowest queries and compare execution plans with the baseline.",
                "Check database saturation, lock waits, and connection pool exhaustion.",
                "Mitigate with query optimization, caching, or reduced report load.",
            ],
        )

    def _single_instance_issue(
        self, event: IncidentEvent, evidence: list[EvidenceItem]
    ) -> Hypothesis | None:
        instance_metrics = _filter(
            evidence,
            lambda item: item.provider == EvidenceProvider.METRIC
            and item.kind == EvidenceKind.METRIC_TREND
            and item.payload.get("instance") is not None
            and (
                item.payload.get("cpu") is not None
                or item.payload.get("error_rate") is not None
                or (
                    item.payload.get("signal_type") in {"cpu", "error"}
                    and _is_strong(item)
                )
            ),
        )
        summary_fallback = contains_any(evidence, ["one instance"])
        canonical_instance = any(
            item.payload.get("signal_type") in {"cpu", "error"}
            and _is_strong(item)
            for item in instance_metrics
        )

        score = 0
        score += 5 if instance_metrics else 0
        score += 2 if canonical_instance else 0
        score += 2 if event.signals.get("instance") == "abnormal" else 0
        score += 1 if event.signals.get("cpu") == "high" else 0
        score += 1 if summary_fallback else 0
        score += 1 if "one instance" in _event_text(event) else 0

        if not instance_metrics or len(
            {item.payload["instance"] for item in instance_metrics}
        ) != 1:
            return None

        return Hypothesis(
            cause_type=CauseType.SINGLE_INSTANCE_ISSUE,
            summary="The impact is concentrated on one unhealthy instance.",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=_ids(instance_metrics),
            next_actions=[
                "Drain and restart the unhealthy instance after preserving diagnostic data.",
                "Compare instance-local CPU, memory, disk, and error logs against healthy peers.",
                "Check whether the load balancer is still routing traffic to the bad instance.",
            ],
        )


def _filter(
    evidence: list[EvidenceItem], predicate: Callable[[EvidenceItem], bool]
) -> list[EvidenceItem]:
    return [item for item in evidence if predicate(item)]


def _ids(evidence: list[EvidenceItem]) -> list[str]:
    return sorted({item.id for item in evidence})


def _unique_evidence(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    return list({item.id: item for item in evidence}.values())


def _event_text(event: IncidentEvent) -> str:
    return f"{event.title} {event.description}".lower()


def _normal_qps_evidence(evidence: list[EvidenceItem]) -> list[EvidenceItem]:
    return _filter(
        evidence,
        lambda item: item.provider == EvidenceProvider.METRIC
        and item.kind == EvidenceKind.METRIC_TREND
        and (
            _is_small_qps_change(item.payload.get("qps_change"))
            or (
                item.payload.get("signal_type") == "traffic"
                and abs(_number(item.payload.get("change_percent"))) <= NORMAL_QPS_MAX_ABS_CHANGE
            )
        ),
    )


def _is_large_positive_qps_change(value: object) -> bool:
    percentage = parse_percentage(value)
    return percentage is not None and percentage >= TRAFFIC_SPIKE_QPS_CHANGE


def _is_small_qps_change(value: object) -> bool:
    percentage = parse_percentage(value)
    return percentage is not None and abs(percentage) <= NORMAL_QPS_MAX_ABS_CHANGE


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) else 0.0


def _is_strong(item: EvidenceItem) -> bool:
    return _number(item.payload.get("deviation_score")) >= 1


def _same_component(left: EvidenceItem, right: EvidenceItem) -> bool:
    left_component = left.payload.get("component") or left.payload.get("service")
    right_component = right.payload.get("component") or right.payload.get("service")
    return not left_component or not right_component or left_component == right_component
