from __future__ import annotations

from dataclasses import dataclass

from backend.domain.agent_findings import RootCauseAttribution, RootCauseCandidate
from backend.domain.evidence import EvidenceItem, validate_usable_evidence

RULE_VERSION = "v2"
_TASK_FIELDS = {
    "task_1": ("time",),
    "task_2": ("reason",),
    "task_3": ("component",),
    "task_4": ("time", "reason"),
    "task_5": ("time", "component"),
    "task_6": ("component", "reason"),
    "task_7": ("time", "component", "reason"),
}


@dataclass(frozen=True)
class ProjectionAudit:
    """记录 OpenRCA 投影过程，不保存 Ground Truth 或原始遥测。"""

    rule_version: str
    scored_fields: tuple[str, ...]
    selected_evidence_ids: tuple[tuple[str, ...], ...]
    projection_fallback: bool = False
    fallback_reason: str | None = None
    projection_error: bool = False

    def metadata(self) -> dict[str, object]:
        """转换为 prediction CSV 可审计的安全 metadata。"""
        return {
            "rule_version": self.rule_version,
            "scored_fields": list(self.scored_fields),
            "selected_evidence_ids": [list(item) for item in self.selected_evidence_ids],
            "projection_fallback": self.projection_fallback,
            "fallback_reason": self.fallback_reason,
            "projection_error": self.projection_error,
        }

    @classmethod
    def failed(cls, task_index: str) -> ProjectionAudit:
        """生成不暴露异常详情的失败审计记录。"""
        return cls(
            rule_version=RULE_VERSION,
            scored_fields=_TASK_FIELDS.get(task_index, ()),
            selected_evidence_ids=(),
            projection_error=True,
        )


@dataclass(frozen=True)
class ProjectionResult:
    """OpenRCA 专用根因投影及其审计信息。"""

    causes: tuple[RootCauseAttribution, ...]
    audit: ProjectionAudit


def scored_fields(task_index: str) -> tuple[str, ...]:
    try:
        return _TASK_FIELDS[task_index]
    except KeyError as exc:
        raise ValueError(f"invalid OpenRCA task_index: {task_index}") from exc


def project_root_causes(
    *,
    task_index: str,
    expected_count: int,
    evidence: list[EvidenceItem],
    root_causes: list[RootCauseAttribution],
) -> ProjectionResult:
    """只做字段投影：校验引用、按被评分字段去重、截取 authoritative Top-N。

    不重建归因、不生成候选、不修改时间、不做二次排序；authoritative root cause
    不足时显式 fallback，由 targeted Gate 判失败。
    """
    fields = scored_fields(task_index)
    evidence_ids = {item.id for item in evidence}
    seen: set[tuple[str, ...]] = set()
    selected: list[RootCauseAttribution] = []
    for cause in root_causes:
        if not cause.supporting_evidence_ids or any(
            item_id not in evidence_ids for item_id in cause.supporting_evidence_ids
        ):
            continue
        key = _projection_key(cause, fields)
        if key in seen:
            continue
        seen.add(key)
        selected.append(cause)
        if len(selected) == expected_count:
            break
    fallback = len(selected) < expected_count
    return ProjectionResult(
        causes=tuple(selected),
        audit=ProjectionAudit(
            rule_version=RULE_VERSION,
            scored_fields=fields,
            selected_evidence_ids=tuple(
                tuple(sorted(cause.supporting_evidence_ids)) for cause in selected
            ),
            projection_fallback=fallback,
            fallback_reason="insufficient_distinct_candidates" if fallback else None,
        ),
    )


def project_v11_candidates(
    *,
    task_index: str,
    expected_count: int,
    evidence: list[EvidenceItem],
    candidates: list[RootCauseCandidate],
    fallback_timestamp,
    runtime_run_id: str,
) -> ProjectionResult:
    """把已接受的 V11 候选投影为 OpenRCA 兼容字段，不重跑 RCA 规则。"""
    fields = scored_fields(task_index)
    projected: list[RootCauseAttribution] = []
    for candidate in sorted(candidates, key=lambda item: item.rank):
        validate_usable_evidence(
            evidence,
            candidate.supporting_evidence_ids,
            runtime_run_id=runtime_run_id,
        )
        supporting = list(candidate.supporting_evidence_ids)
        projected.append(
            RootCauseAttribution(
                root_cause_occurred_at=(
                    candidate.onset_window_start or fallback_timestamp
                ),
                root_cause_component=candidate.affected_entity or "unknown",
                root_cause_reason=candidate.failure_mechanism or candidate.summary,
                supporting_evidence_ids=supporting,
            )
        )
        if len(projected) >= expected_count:
            break
    fallback = len(projected) < expected_count
    return ProjectionResult(
        causes=tuple(projected),
        audit=ProjectionAudit(
            rule_version="v11-agent-generic",
            scored_fields=fields,
            selected_evidence_ids=tuple(
                tuple(sorted(cause.supporting_evidence_ids)) for cause in projected
            ),
            projection_fallback=fallback,
            fallback_reason="insufficient_accepted_candidates" if fallback else None,
        ),
    )


def _projection_key(
    cause: RootCauseAttribution,
    fields: tuple[str, ...],
) -> tuple[str, ...]:
    values = {
        "time": cause.root_cause_occurred_at.isoformat(),
        "component": cause.root_cause_component.casefold(),
        "reason": cause.root_cause_reason.casefold(),
    }
    return tuple(values[field] for field in fields)
