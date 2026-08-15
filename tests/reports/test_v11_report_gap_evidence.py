"""V11 报告层对 GAP finding 证据引用的契约（spec §7.2 + §8.2）。

GAP 的语义是"证据缺失"：引用同 run 已提交的 failed/skipped 证据正是其正确
出处（与准入层 `_finding_from_draft`、终态校验 `validate_v11_result` 同一
契约）。报告层只豁免 GAP；非 GAP finding、candidate、assessment 的引用仍
要求 usable（success/partial）。
"""

from datetime import UTC, datetime

import pytest

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    FindingActor,
)
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    DiagnosticStatus,
)
from backend.reports.generator import ReportGenerator
from tests.reports.test_v11_product_integration import (
    RUN_ID,
    _candidate,
    _event,
    _evidence,
    _review,
    _run_summary,
)


def _skipped_evidence(
    evidence_id: str = "ev-deploy-skipped", *, runtime_run_id: str = RUN_ID
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime(2026, 8, 5, 0, 2, tzinfo=UTC),
        summary="deploy provider skipped",
        status=EvidenceStatus.SKIPPED,
        error_message="read_deployments provider not configured",
        runtime_run_id=runtime_run_id,
    )


def _gap_finding(evidence_id: str) -> AgentFinding:
    return AgentFinding(
        id="finding-gap-deploy",
        investigation_id="inv-v11-product",
        agent_name=FindingActor.INVESTIGATOR,
        agent_instance_id="inst-1",
        task_id="task-1",
        runtime_run_id=RUN_ID,
        finding_type=AgentFindingType.GAP,
        summary="deployment change evidence is unavailable",
        confidence=0.2,
        evidence_ids=[evidence_id],
        gaps=["deployment history unavailable"],
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        analysis_round=1,
    )


def _generate(evidence: list[EvidenceItem], findings: list[AgentFinding]):
    return ReportGenerator().generate(
        "inv-v11-product",
        _event(),
        evidence,
        [],
        coordination_review=_review(_candidate()),
        multi_agent_run=_run_summary(DiagnosticStatus.COMPLETE),
        agent_findings=findings,
    )


def test_v11_report_accepts_gap_finding_citing_skipped_evidence():
    skipped = _skipped_evidence()
    report = _generate([_evidence(), skipped], [_gap_finding(skipped.id)])
    assert "finding-gap-deploy" in report.markdown


def test_v11_report_accepts_gap_finding_citing_failed_evidence():
    failed = _skipped_evidence().model_copy(
        update={"status": EvidenceStatus.FAILED}
    )
    report = _generate([_evidence(), failed], [_gap_finding(failed.id)])
    assert "finding-gap-deploy" in report.markdown


def test_v11_report_rejects_non_gap_finding_citing_skipped_evidence():
    skipped = _skipped_evidence()
    finding = _gap_finding(skipped.id).model_copy(
        update={"finding_type": AgentFindingType.SIGNAL, "gaps": []}
    )
    with pytest.raises(ValueError, match="usable evidence"):
        _generate([_evidence(), skipped], [finding])


def test_v11_report_rejects_gap_finding_citing_uncommitted_evidence():
    with pytest.raises(ValueError, match="missing evidence id: ev-missing"):
        _generate([_evidence()], [_gap_finding("ev-missing")])


def test_v11_report_rejects_gap_finding_citing_evidence_from_another_run():
    foreign = _skipped_evidence(runtime_run_id="run-other")
    with pytest.raises(ValueError, match="owner mismatch"):
        _generate([_evidence(), foreign], [_gap_finding(foreign.id)])


def test_legacy_report_rejects_gap_finding_citing_missing_evidence():
    # legacy（非 AGENT authority）分支只校验存在性，GAP 引用同样不能悬空。
    review = _review(_candidate()).model_copy(
        update={"authority_mode": AuthorityMode.LEGACY_DETERMINISTIC}
    )
    with pytest.raises(ValueError, match="missing evidence id: ev-missing"):
        ReportGenerator().generate(
            "inv-v11-product",
            _event(),
            [_evidence()],
            [],
            coordination_review=review,
            multi_agent_run=_run_summary(DiagnosticStatus.COMPLETE),
            agent_findings=[_gap_finding("ev-missing")],
        )
