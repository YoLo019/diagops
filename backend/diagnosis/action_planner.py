from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.agent_findings import CoordinationReview
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import (
    AuthorityMode,
    DiagnosticStatus,
    MultiAgentRunSummary,
)
from backend.domain.v11_contracts import validate_v11_final_status
from backend.safety.redaction import redact_model


class ActionPlanner:
    def plan(
        self,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
    ) -> tuple[list[RecommendedAction], list[VerificationSuggestion]]:
        if not hypotheses:
            return self._manual_follow_up("No hypotheses were generated.")
        if not evidence:
            return self._manual_follow_up("No evidence was available.")

        top = hypotheses[0]
        supporting_ids = top.supporting_evidence_ids or [item.id for item in evidence[:1]]
        if not supporting_ids:
            return self._manual_follow_up("No evidence was available.")

        if top.cause_type == CauseType.DEPLOYMENT_REGRESSION:
            return [
                RecommendedAction(
                    action_type=ActionType.ROLLBACK_SUGGESTION,
                    title=f"Evaluate rollback for {event.service}",
                    description=(
                        "Recent deployment evidence and new errors point to a possible "
                        "regression. V2 does not execute rollback."
                    ),
                    risk_level=ActionRiskLevel.HIGH,
                    requires_approval=True,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications(
                "5xx rate below 1%",
                "new exception group stops appearing",
            )

        if top.cause_type == CauseType.TRAFFIC_SPIKE:
            return [
                RecommendedAction(
                    action_type=ActionType.SCALE_SUGGESTION,
                    title=f"Evaluate capacity response for {event.service}",
                    description=(
                        "Traffic and latency evidence suggest overload. V2 only records "
                        "this as a recommendation."
                    ),
                    risk_level=ActionRiskLevel.MEDIUM,
                    requires_approval=True,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications(
                "QPS returns to expected range",
                "P95 latency recovers",
            )

        if top.cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE:
            return [
                RecommendedAction(
                    action_type=ActionType.DEPENDENCY_CHECK,
                    title="Contact dependency owner",
                    description=(
                        "Dependency health evidence points to a downstream issue."
                    ),
                    risk_level=ActionRiskLevel.LOW,
                    requires_approval=False,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications(
                "dependency latency recovers",
                "local timeout errors decrease",
            )

        if top.cause_type == CauseType.DATABASE_SLOWDOWN:
            return [
                RecommendedAction(
                    action_type=ActionType.CONFIG_CHECK,
                    title="Review database performance signals",
                    description=(
                        "Database latency evidence suggests a database slowdown."
                    ),
                    risk_level=ActionRiskLevel.LOW,
                    requires_approval=False,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications(
                "database P95 latency recovers",
                "application latency recovers",
            )

        if top.cause_type == CauseType.SINGLE_INSTANCE_ISSUE:
            return [
                RecommendedAction(
                    action_type=ActionType.CHECK,
                    title="Inspect abnormal instance",
                    description="Instance-level evidence suggests one bad instance.",
                    risk_level=ActionRiskLevel.READ_ONLY,
                    requires_approval=False,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications(
                "bad instance error rate recovers",
                "load balancer stops routing errors",
            )

        return self._manual_follow_up(top.summary, supporting_ids)

    def plan_v11(
        self,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        review: CoordinationReview,
        run: MultiAgentRunSummary,
    ) -> tuple[list[RecommendedAction], list[VerificationSuggestion]]:
        """从已接受候选生成同一 run 绑定的只读建议。"""
        if (
            review.authority_mode != AuthorityMode.AGENT
            or run.authority_mode != AuthorityMode.AGENT
            or review.runtime_run_id is None
            or run.runtime_run_id != review.runtime_run_id
        ):
            raise ValueError("V11 actions require matching Agent review and run")
        validate_v11_final_status(review, run)
        if review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE:
            return [], []

        evidence_by_id = {
            item.id: item
            for item in evidence
            if item.status.value in {"success", "partial"}
        }
        candidates_by_id = {candidate.id: candidate for candidate in review.candidates}
        actions: list[RecommendedAction] = []
        verifications: list[VerificationSuggestion] = []
        for candidate_id in review.authoritative_candidate_ids:
            candidate = candidates_by_id.get(candidate_id)
            if candidate is None:
                raise ValueError("V11 review references an unknown candidate")
            supporting_ids = [
                evidence_id
                for evidence_id in candidate.supporting_evidence_ids
                if evidence_id in evidence_by_id
            ]
            if not supporting_ids:
                continue
            entity = candidate.affected_entity or event.service
            mechanism = candidate.failure_mechanism or candidate.summary
            action = RecommendedAction(
                action_type=ActionType.CHECK,
                title=f"Inspect evidence for {entity}",
                description=(
                    f"Review the read-only signals for {entity} related to {mechanism}. "
                    "This recommendation does not execute a production change."
                ),
                risk_level=ActionRiskLevel.READ_ONLY,
                requires_approval=False,
                supporting_evidence_ids=supporting_ids,
                related_candidate_ids=[candidate.id],
                runtime_run_id=review.runtime_run_id,
            )
            actions.append(action)
            verifications.append(
                VerificationSuggestion(
                    title=f"Verify {entity} signals",
                    description=(
                        f"Confirm the read-only signals for {entity} match the candidate "
                        "before any human-approved action."
                    ),
                    expected_signal="candidate evidence remains consistent",
                    related_action_ids=[action.id],
                    related_candidate_ids=[candidate.id],
                    runtime_run_id=review.runtime_run_id,
                )
            )
        # 动态文字拼接后再次脱敏，避免安全片段组合成新的敏感赋值形态。
        return [redact_model(item) for item in actions], [
            redact_model(item) for item in verifications
        ]

    def _manual_follow_up(
        self,
        reason: str,
        supporting_ids: list[str] | None = None,
    ) -> tuple[list[RecommendedAction], list[VerificationSuggestion]]:
        return [
            RecommendedAction(
                action_type=ActionType.MANUAL_FOLLOW_UP,
                title="Collect more evidence",
                description=reason,
                risk_level=ActionRiskLevel.LOW,
                requires_approval=False,
                supporting_evidence_ids=supporting_ids or ["ev-missing-evidence"],
            )
        ], [
            VerificationSuggestion(
                title="Collect additional evidence",
                description=(
                    "Gather logs, metrics, deploy records, and dependency status before "
                    "acting."
                ),
                expected_signal="new evidence is collected",
            )
        ]

    def _default_verifications(self, *signals: str) -> list[VerificationSuggestion]:
        return [
            VerificationSuggestion(
                title=f"Verify {signal}",
                description=f"Confirm that {signal}.",
                expected_signal=signal,
            )
            for signal in signals
        ]
