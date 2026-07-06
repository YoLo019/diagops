from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.llm_analysis import LLMAnalysis


class ReadOnlyLlmAnalyst:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def analyze(
        self,
        *,
        investigation_id: str,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
    ) -> LLMAnalysis | None:
        if not self.enabled:
            return None

        evidence_ids = {item.id for item in evidence}
        referenced_ids = self._referenced_evidence_ids(evidence, hypotheses)
        missing_evidence = self._missing_evidence(evidence)
        risk_notes = self._risk_notes(hypotheses)
        suggested_questions = self._suggested_questions(missing_evidence, hypotheses)

        return LLMAnalysis.create(
            investigation_id=investigation_id,
            existing_evidence_ids=evidence_ids,
            summary=self._summary(evidence, hypotheses),
            missing_evidence=missing_evidence,
            risk_notes=risk_notes,
            suggested_questions=suggested_questions,
            referenced_evidence_ids=referenced_ids,
        )

    def _referenced_evidence_ids(
        self,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
    ) -> list[str]:
        evidence_ids = {item.id for item in evidence}
        referenced: list[str] = []
        for hypothesis in hypotheses:
            referenced.extend(hypothesis.supporting_evidence_ids)
            referenced.extend(hypothesis.contradicting_evidence_ids)
        if not referenced:
            referenced.extend(item.id for item in evidence[:3])
        return sorted({evidence_id for evidence_id in referenced if evidence_id in evidence_ids})

    def _missing_evidence(self, evidence: list[EvidenceItem]) -> list[str]:
        present_providers = {item.provider for item in evidence}
        suggestions: list[str] = []
        provider_suggestions = [
            (
                EvidenceProvider.DEPLOY,
                "Add recent deployment/change evidence for the affected service.",
            ),
            (
                EvidenceProvider.LOG,
                "Add error log patterns from the incident window.",
            ),
            (
                EvidenceProvider.METRIC,
                "Add service metrics for traffic, latency, and error rate trends.",
            ),
            (
                EvidenceProvider.DEPENDENCY,
                "Add downstream dependency health evidence.",
            ),
            (
                EvidenceProvider.SERVICE_CATALOG,
                "Add service ownership and dependency metadata.",
            ),
        ]
        for provider, suggestion in provider_suggestions:
            if provider not in present_providers:
                suggestions.append(suggestion)

        failed_kinds = {item.kind for item in evidence if item.kind == EvidenceKind.PROVIDER_ERROR}
        if failed_kinds:
            suggestions.append("Review failed evidence providers and collect replacement data.")
        return suggestions

    def _risk_notes(self, hypotheses: list[Hypothesis]) -> list[str]:
        if not hypotheses:
            return ["No root-cause hypothesis was available for LLM review."]
        top = max(hypotheses, key=lambda item: item.confidence)
        if top.confidence < 0.7:
            return ["Top hypothesis confidence is below 0.70; avoid irreversible actions."]
        return [f"Top hypothesis is {top.cause_type.value} with grounded evidence links."]

    def _suggested_questions(
        self,
        missing_evidence: list[str],
        hypotheses: list[Hypothesis],
    ) -> list[str]:
        questions = [
            "Which missing evidence source would most quickly confirm or rule out "
            "the top hypothesis?"
        ]
        if hypotheses:
            top = max(hypotheses, key=lambda item: item.confidence)
            if top.cause_type == CauseType.DEPLOYMENT_REGRESSION:
                questions.append("Was the latest deployment correlated with the first bad signal?")
            elif top.cause_type == CauseType.DEPENDENCY_FAILURE:
                questions.append("Which downstream dependency changed state during the incident?")
        if missing_evidence:
            questions.append("Can the on-call collect the highest-priority missing evidence now?")
        return questions

    def _summary(
        self,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
    ) -> str:
        if not evidence:
            return "No evidence was available for read-only LLM analysis."
        if not hypotheses:
            return f"Reviewed {len(evidence)} evidence item(s); no hypothesis was available."
        top = max(hypotheses, key=lambda item: item.confidence)
        return (
            f"Reviewed {len(evidence)} evidence item(s); top hypothesis is "
            f"{top.cause_type.value}."
        )
