from datetime import datetime

import pytest

from backend.db.models import InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.agents_runtime import AgentsRcaRuntimeResult
from backend.diagnosis.coordination_review import build_hybrid_coordination_review
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.agent_plan import AgentExecutionStatus, DiagnosisTaskStatus
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    CoordinationDecisionStatus,
    ModelProvider,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import DECISION_LABELS, ReportGenerator
from backend.services.incident_cases import load_incident_case


def test_report_generator_outputs_markdown_with_evidence():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert report.investigation_id == "inv-1"
    assert "最可能根因" in report.markdown
    assert "NullPointerException" in report.markdown
    assert "payment-service v1.8.2" in report.markdown
    assert "事实与推断" in report.markdown
    assert "观察到的事实" in report.markdown
    assert "推断结论/不确定性" in report.markdown
    assert report.action_ids == []
    assert report.verification_suggestion_ids == []


def test_report_generator_rejects_empty_hypotheses():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)

    with pytest.raises(ValueError, match="hypotheses must contain at least one item"):
        ReportGenerator().generate("inv-1", event, evidence, [])


def test_report_generator_rejects_missing_supporting_evidence_id():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(
            supporting_evidence_ids=["ev-real", "ev-missing-support"],
        )
    ]

    with pytest.raises(ValueError, match="missing evidence id: ev-missing-support"):
        ReportGenerator().generate("inv-1", event, evidence, hypotheses)


def test_report_generator_rejects_missing_contradicting_evidence_id():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(
            supporting_evidence_ids=["ev-real"],
            contradicting_evidence_ids=["ev-missing-contradiction"],
        )
    ]

    with pytest.raises(ValueError, match="missing evidence id: ev-missing-contradiction"):
        ReportGenerator().generate("inv-1", event, evidence, hypotheses)


def test_report_generator_rejects_missing_evidence_id_from_any_hypothesis():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(supporting_evidence_ids=["ev-real"]),
        _hypothesis(
            cause_type=CauseType.TRAFFIC_SPIKE,
            supporting_evidence_ids=["ev-missing-alternative"],
        ),
    ]

    with pytest.raises(ValueError, match="missing evidence id: ev-missing-alternative"):
        ReportGenerator().generate("inv-1", event, evidence, hypotheses)


def test_report_generator_rejects_missing_action_evidence_id():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [_hypothesis(supporting_evidence_ids=["ev-real"])]
    actions = [
        RecommendedAction(
            action_type=ActionType.CHECK,
            title="Inspect deployment",
            description="Check deployment metadata.",
            risk_level=ActionRiskLevel.READ_ONLY,
            requires_approval=False,
            supporting_evidence_ids=["ev-missing-action"],
        )
    ]

    with pytest.raises(ValueError, match="missing evidence id: ev-missing-action"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            actions=actions,
        )


def test_report_generator_orders_markdown_evidence_chain_by_timestamp():
    event = load_incident_case("deployment_regression")
    older = _evidence(
        "ev-older",
        "older evidence summary",
        timestamp="2026-07-03T13:55:00+08:00",
    )
    newer = _evidence(
        "ev-newer",
        "newer evidence summary",
        timestamp="2026-07-03T14:05:00+08:00",
    )
    hypotheses = [_hypothesis(supporting_evidence_ids=["ev-older"])]

    report = ReportGenerator().generate("inv-1", event, [newer, older], hypotheses)

    assert report.markdown.index("older evidence summary") < report.markdown.index(
        "newer evidence summary"
    )
    assert report.timeline == [
        {"time": older.timestamp.isoformat(), "event": older.summary},
        {"time": newer.timestamp.isoformat(), "event": newer.summary},
    ]


def test_report_generator_renders_alternative_hypotheses():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(supporting_evidence_ids=["ev-real"]),
        _hypothesis(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="Traffic also increased around the incident.",
            confidence=0.42,
        ),
    ]

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert "其他可能假设" in report.markdown
    assert "traffic_spike" in report.markdown
    assert "Traffic also increased around the incident." in report.markdown
    assert "0.42" in report.markdown


def test_report_generator_renders_stable_chinese_headings_and_raw_fields():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [
        _hypothesis(supporting_evidence_ids=["ev-real"]),
        _hypothesis(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="Traffic also increased around the incident.",
            confidence=0.42,
        ),
    ]
    actions = [
        RecommendedAction(
            action_type=ActionType.CHECK,
            title="Inspect deployment",
            description="Check deployment metadata.",
            risk_level=ActionRiskLevel.READ_ONLY,
            requires_approval=False,
            supporting_evidence_ids=["ev-real"],
        )
    ]
    verifications = [
        VerificationSuggestion(
            title="Check recovery",
            description="Confirm error rate returned to normal.",
            expected_signal="5xx rate below 1%",
        )
    ]

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        actions=actions,
        verification_suggestions=verifications,
    )

    for heading in [
        "摘要",
        "最可能根因",
        "证据链",
        "支持该结论的证据",
        "其他可能假设",
        "建议动作",
        "验证建议",
        "事实与推断",
    ]:
        assert f"## {heading}" in report.markdown

    assert "[`deploy/deployment`]" in report.markdown
    assert "`deployment_regression`" in report.markdown
    assert "`traffic_spike`" in report.markdown
    assert "`check`" in report.markdown
    assert "`read_only`" in report.markdown
    assert "`proposed`" in report.markdown
    assert "`pending`" in report.markdown


def test_report_generator_renders_real_contradicting_evidence_only():
    event = load_incident_case("deployment_regression")
    supporting = _evidence("ev-support", "real supporting evidence")
    contradicting = _evidence("ev-contradicting", "real contradicting evidence")
    hypotheses = [
        _hypothesis(
            supporting_evidence_ids=["ev-support"],
            contradicting_evidence_ids=["ev-contradicting"],
        )
    ]

    report = ReportGenerator().generate(
        "inv-1", event, [supporting, contradicting], hypotheses
    )

    assert "反向证据/不确定性" in report.markdown
    assert "real supporting evidence" in report.markdown
    assert "real contradicting evidence" in report.markdown
    assert "ev-missing" not in report.markdown


def test_report_generator_renders_v2_actions_and_verifications():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)
    actions, verifications = ActionPlanner().plan(event, evidence, hypotheses)

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        actions=actions,
        verification_suggestions=verifications,
    )

    assert report.action_ids == [action.id for action in actions]
    assert report.verification_suggestion_ids == [
        suggestion.id for suggestion in verifications
    ]
    assert "## 建议动作" in report.markdown
    assert "## 需要审批的动作" in report.markdown
    assert "## 验证建议" in report.markdown
    assert "V2 未执行该动作" in report.markdown
    assert actions[0].id in report.markdown
    assert verifications[0].id in report.markdown


def test_report_generator_accepts_explicit_none_for_v2_sections():
    event = load_incident_case("deployment_regression")
    evidence = [_evidence("ev-real", "real deployment evidence")]
    hypotheses = [_hypothesis(supporting_evidence_ids=["ev-real"])]

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        actions=None,
        verification_suggestions=None,
    )

    assert report.action_ids == []
    assert report.verification_suggestion_ids == []
    assert "## 需要审批的动作" in report.markdown
    assert "## 验证建议" in report.markdown


def test_v7_decision_labels_are_exact():
    assert DECISION_LABELS == {
        "agreement": "多 Agent 复核一致",
        "conflict": "存在冲突，需要人工确认",
        "agent_leads": "多 Agent 主要候选，尚未确认",
        "fallback": "多 Agent 复核未完成，以下为确定性 RCA 结果",
    }


def test_report_generator_default_off_does_not_add_v7_section():
    event, evidence, hypotheses = _v7_baseline()

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert "## 混合 RCA 裁决" not in report.markdown


@pytest.mark.parametrize(
    ("decision_status", "expected_label"),
    [
        (CoordinationDecisionStatus.AGREEMENT, "多 Agent 复核一致"),
        (CoordinationDecisionStatus.CONFLICT, "存在冲突，需要人工确认"),
        (CoordinationDecisionStatus.AGENT_LEADS, "多 Agent 主要候选，尚未确认"),
        (
            CoordinationDecisionStatus.FALLBACK,
            "多 Agent 复核未完成，以下为确定性 RCA 结果",
        ),
    ],
)
def test_report_generator_renders_exact_v7_decision_label(
    decision_status, expected_label
):
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review(decision_status=decision_status)

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        coordination_review=review,
        multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
        agent_findings=findings,
    )
    section = _section(report.markdown, "混合 RCA 裁决", "建议动作")

    assert expected_label in section
    if decision_status == CoordinationDecisionStatus.AGENT_LEADS:
        assert "确认根因" not in section


@pytest.mark.parametrize(
    "run_status",
    list(MultiAgentRunStatus),
)
def test_report_generator_renders_all_v7_run_statuses(run_status):
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review(
        run_status=run_status,
        decision_status=CoordinationDecisionStatus.CONFLICT,
    )
    if run_status in {MultiAgentRunStatus.FAILED, MultiAgentRunStatus.SKIPPED}:
        review = None

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        coordination_review=review,
        multi_agent_run=MultiAgentRunSummary(
            status=run_status,
            failure_reason="not configured" if review is None else None,
        ),
        agent_findings=findings,
    )
    section = _section(report.markdown, "混合 RCA 裁决", "建议动作")

    assert f"`{run_status}`" in section
    if review is None:
        assert DECISION_LABELS["fallback"] in section
        assert "未用 V7 review 替换确定性 RCA" in section


def test_report_generator_separates_v7_inference_history_and_actions():
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review()

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        coordination_review=review,
        multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
        agent_findings=findings,
    )
    section = _section(report.markdown, "混合 RCA 裁决", "建议动作")

    assert report.markdown.index("## 混合 RCA 裁决") < report.markdown.index(
        "## 建议动作"
    )
    assert "### 确定性推断" in section
    assert "`deployment_regression`" in section
    assert "0.88" in section
    assert "review 基线：`deployment_regression`" in section
    assert "### Agent 推断" in section
    assert "协调摘要" in section
    assert "`traffic_spike`" in section
    assert "LogAgent" in section
    assert "第 1 轮" in section and "第 2 轮" in section
    assert "修订自：`finding-v7-r1`" in section
    assert "`ev-agent`" in section and "`ev-review`" in section
    assert "private chain of thought" not in section
    assert "### 不确定性" in section
    assert "需要人工比对冲突证据" in section
    assert "执行状态：V2 未执行上述动作" in report.markdown


@pytest.mark.parametrize(
    ("entry", "injected_text", "dangerous"),
    [
        (
            "finding",
            'Bearer abc token="secret" user@example.com postgres://u:p@db '
            "<script>*unsafe*</script>\n## 建议动作",
            False,
        ),
        (
            "review_summary",
            "secret='value' admin@example.com\n## 建议动作\n已执行回滚 via SSH",
            True,
        ),
        (
            "review_uncertainty",
            "password=pwd mysql://root:pwd@db user@example.com\r\n"
            "## 建议动作 restart scale 配置变更",
            True,
        ),
        ("finding", "已完成回滚，服务已修复", True),
        ("review_summary", "回滚已完成，实例恢复正常", True),
        ("review_uncertainty", "执行了重启并完成扩容", True),
        ("finding", "配置已经修改，故障修复完成", True),
        ("review_summary", "the service was rebooted and repaired", True),
        ("finding", "服务已修复", True),
        ("review_uncertainty", "故障修复成功", True),
        (
            "finding",
            "I updated the production configuration successfully.",
            True,
        ),
        ("review_summary", "I rolled back the deployment.", True),
        ("review_uncertainty", "We already applied the config change.", True),
    ],
)
def test_report_generator_sanitizes_v7_agent_text(entry, injected_text, dangerous):
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review()
    if entry == "finding":
        findings[0].summary = injected_text
    elif entry == "review_summary":
        review.summary = injected_text
    else:
        review.uncertainty = injected_text

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        coordination_review=review,
        multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
        agent_findings=findings,
    )
    section = _section(report.markdown, "混合 RCA 裁决", "建议动作")

    assert report.markdown.count("## 建议动作") == 1
    assert injected_text not in section
    for unsafe in [
        "Bearer abc",
        'token="secret"',
        "secret='value'",
        "password=pwd",
        "user@example.com",
        "admin@example.com",
        "postgres://u:p@db",
        "mysql://root:pwd@db",
        "已执行回滚",
        "SSH",
    ]:
        assert unsafe not in section
    if dangerous:
        assert "[未验证操作声明已省略]" in section
    else:
        assert "REDACTED" in section
        assert "&lt;script&gt;" in section
        assert "\\#\\# 建议动作" in section


@pytest.mark.parametrize(
    "claim",
    [
        "I updated the production configuration successfully.",
        "I rolled back the deployment.",
        "We already applied the config change.",
    ],
)
def test_report_generator_keeps_v5_text_unchanged(claim):
    event, evidence, _ = _v7_baseline()

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        [_hypothesis(summary=claim, supporting_evidence_ids=["ev-baseline"])],
    )

    assert claim in report.markdown


@pytest.mark.parametrize(
    ("failure_reason", "safe_category"),
    [
        ("Bearer abc token=secret user@example.com postgres://u:p@db", "operation failed"),
        ("request timeout; Bearer abc", "operation failed"),
        ("401 unauthorized token=secret", "operation failed"),
        ("429 rate limit for user@example.com", "operation failed"),
        ("quota exceeded for postgres://u:p@db", "operation failed"),
        ("JSON validation error token=secret", "operation failed"),
    ],
)
def test_report_generator_redacts_v7_failure_reason(failure_reason, safe_category):
    event, evidence, hypotheses = _v7_baseline()

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        multi_agent_run=MultiAgentRunSummary(
            status=MultiAgentRunStatus.FAILED,
            failure_reason=failure_reason,
        ),
    )
    section = _section(report.markdown, "混合 RCA 裁决", "建议动作")

    assert f"`{safe_category}`" in section
    for secret in ["Bearer", "token=", "user@example.com", "postgres://"]:
        assert secret not in section


def test_report_generator_does_not_treat_v5_review_as_v7():
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review()
    review.execution_layer = AgentExecutionLayer.CUSTOM

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        coordination_review=review,
        multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
        agent_findings=findings,
    )
    section = _section(report.markdown, "混合 RCA 裁决", "建议动作")

    assert DECISION_LABELS["fallback"] in section
    assert "安全失败分类：`operation failed`" in section
    assert "未用 V7 review 替换确定性 RCA" in section


def test_report_generator_rejects_unknown_agent_finding_evidence():
    event, evidence, hypotheses = _v7_baseline()
    findings, _review = _v7_review()
    findings[0].evidence_ids = ["ev-unknown"]

    with pytest.raises(ValueError, match="missing evidence id: ev-unknown"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED),
            agent_findings=findings,
        )


def test_report_generator_rejects_unknown_review_candidate_evidence():
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review()
    review.candidates[0].contradicting_evidence_ids = ["ev-unknown"]

    with pytest.raises(ValueError, match="missing evidence id: ev-unknown"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            coordination_review=review,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
            agent_findings=findings,
        )


def test_report_generator_rejects_unknown_review_candidate_finding():
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review()
    review.candidates[0].contradicting_finding_ids = ["finding-unknown"]

    with pytest.raises(ValueError, match="missing finding id: finding-unknown"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            coordination_review=review,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
            agent_findings=findings,
        )


def test_report_generator_rejects_duplicate_agent_finding_id():
    event, evidence, hypotheses = _v7_baseline()
    findings, _review = _v7_review()
    findings.append(findings[0].model_copy())

    with pytest.raises(ValueError, match="duplicate finding id: finding-v7-r1"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED),
            agent_findings=findings,
        )


def test_report_generator_rejects_foreign_agent_finding():
    event, evidence, hypotheses = _v7_baseline()
    findings, _review = _v7_review()
    findings[0].investigation_id = "inv-foreign"

    with pytest.raises(ValueError, match="finding investigation mismatch"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED),
            agent_findings=findings,
        )


def test_report_generator_rejects_custom_agent_finding():
    event, evidence, hypotheses = _v7_baseline()
    findings, _review = _v7_review()
    findings[0].execution_layer = AgentExecutionLayer.CUSTOM

    with pytest.raises(ValueError, match="finding execution layer mismatch"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED),
            agent_findings=findings,
        )


def test_report_generator_rejects_foreign_coordination_review():
    event, evidence, hypotheses = _v7_baseline()
    findings, review = _v7_review()
    review.investigation_id = "inv-foreign"

    with pytest.raises(ValueError, match="review investigation mismatch"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            coordination_review=review,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.COMPLETED),
            agent_findings=findings,
        )


@pytest.mark.parametrize("revision_case", ["missing", "round2", "cross_agent"])
def test_report_generator_rejects_invalid_finding_revision(revision_case):
    event, evidence, hypotheses = _v7_baseline()
    findings, _review = _v7_review()
    if revision_case == "missing":
        findings[1].revises_finding_id = "finding-unknown"
    elif revision_case == "round2":
        findings[0].analysis_round = 2
        findings[0].revises_finding_id = findings[1].id
    else:
        findings[1].agent_name = AgentName.METRIC

    with pytest.raises(ValueError, match="invalid finding revision"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            multi_agent_run=MultiAgentRunSummary(status=MultiAgentRunStatus.FAILED),
            agent_findings=findings,
        )


def test_orchestrator_passes_persisted_v7_result_to_report():
    class TrackingRepository(InMemoryInvestigationRepository):
        saved_sdk_findings = None
        saved_sdk_review = None

        def save_multi_agent_result(
            self, investigation_id, findings, executions, review
        ):
            self.saved_sdk_findings = findings
            self.saved_sdk_review = review
            return super().save_multi_agent_result(
                investigation_id, findings, executions, review
            )

    repository = TrackingRepository()
    report_generator = _CapturingReportGenerator()
    runtime = _ReportRuntime(repository)

    record = _v7_orchestrator(repository, runtime, report_generator).run(
        load_incident_case("deployment_regression")
    )

    assert record.status == InvestigationStatus.COMPLETED
    assert report_generator.kwargs["coordination_review"].execution_layer == (
        AgentExecutionLayer.OPENAI_AGENTS_SDK
    )
    assert report_generator.kwargs["multi_agent_run"].status == (
        MultiAgentRunStatus.COMPLETED
    )
    assert report_generator.kwargs["agent_findings"]
    assert report_generator.kwargs["coordination_review"] is not (
        repository.saved_sdk_review
    )
    assert report_generator.kwargs["agent_findings"][0] is not (
        repository.saved_sdk_findings[0]
    )
    assert "## 混合 RCA 裁决" in record.report.markdown


def test_orchestrator_reports_structured_recovery_when_task_write_fails():
    class FailingV7Repository(InMemoryInvestigationRepository):
        v7_attempts = 0

        def save_tasks(self, investigation_id, tasks):
            if any(
                task.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
                for task in tasks
            ):
                self.v7_attempts += 1
                raise RuntimeError("Bearer secret@example.com postgres://u:p@db")
            return super().save_tasks(investigation_id, tasks)

    repository = FailingV7Repository()
    report_generator = _CapturingReportGenerator()

    record = _v7_orchestrator(
        repository,
        _ReportRuntime(repository),
        report_generator,
    ).run(load_incident_case("deployment_regression"))

    assert repository.v7_attempts == 1
    assert record.status == InvestigationStatus.COMPLETED
    assert report_generator.kwargs["coordination_review"] is None
    assert report_generator.kwargs["agent_findings"] == []
    assert report_generator.kwargs["multi_agent_run"].status == (
        MultiAgentRunStatus.FAILED
    )
    assert (
        report_generator.kwargs[
            "multi_agent_run"
        ].primary_stabilization_category.value
        == "review_persistence"
    )


@pytest.mark.parametrize("failed_phase", ["executions", "findings"])
def test_orchestrator_recovers_once_when_atomic_agent_write_fails(failed_phase):
    class IncompleteV7Repository(InMemoryInvestigationRepository):
        v7_attempts = 0

        def save_multi_agent_result(self, *args, **kwargs):
            self.v7_attempts += 1
            if self.v7_attempts == 1:
                raise RuntimeError(f"{failed_phase} persistence failed")
            return super().save_multi_agent_result(*args, **kwargs)

    repository = IncompleteV7Repository()
    report_generator = _CapturingReportGenerator()

    record = _v7_orchestrator(
        repository,
        _ReportRuntime(repository),
        report_generator,
    ).run(load_incident_case("deployment_regression"))

    assert repository.v7_attempts == 2
    assert record.status == InvestigationStatus.COMPLETED
    assert report_generator.kwargs["coordination_review"] is None
    assert report_generator.kwargs["agent_findings"] == []
    assert report_generator.kwargs["multi_agent_run"].status == (
        MultiAgentRunStatus.FAILED
    )


def test_orchestrator_never_exposes_completed_review_after_acknowledgement_error():
    class WriteThenRaiseRepository(InMemoryInvestigationRepository):
        v7_attempts = 0

        def save_multi_agent_result(self, *args, **kwargs):
            self.v7_attempts += 1
            saved = super().save_multi_agent_result(*args, **kwargs)
            if self.v7_attempts == 1:
                raise RuntimeError("review write acknowledgement failed")
            return saved

    repository = WriteThenRaiseRepository()
    report_generator = _CapturingReportGenerator()

    record = _v7_orchestrator(
        repository,
        _ReportRuntime(repository),
        report_generator,
    ).run(load_incident_case("deployment_regression"))

    assert repository.v7_attempts == 2
    assert record.status == InvestigationStatus.COMPLETED
    assert report_generator.kwargs["coordination_review"] is None
    assert report_generator.kwargs["multi_agent_run"].status == (
        MultiAgentRunStatus.FAILED
    )
    assert "## 混合 RCA 裁决" in record.report.markdown


@pytest.mark.parametrize("mutated_payload", ["finding", "review"])
def test_orchestrator_omits_v7_for_same_id_different_payload(mutated_payload):
    class MutatingRepository(InMemoryInvestigationRepository):
        def save_multi_agent_result(
            self, investigation_id, findings, executions, review
        ):
            if mutated_payload == "finding":
                findings = [
                    item.model_copy(update={"summary": "mutated persisted finding"})
                    for item in findings
                ]
            if mutated_payload == "review" and review is not None:
                review = review.model_copy(
                    update={"summary": "mutated persisted review"}
                )
            return super().save_multi_agent_result(
                investigation_id, findings, executions, review
            )

    repository = MutatingRepository()
    report_generator = _CapturingReportGenerator()

    record = _v7_orchestrator(
        repository,
        _ReportRuntime(repository),
        report_generator,
    ).run(load_incident_case("deployment_regression"))

    assert record.status == InvestigationStatus.COMPLETED
    assert "multi_agent_run" not in report_generator.kwargs


def test_orchestrator_recovers_when_mutated_review_write_raises():
    class MutatingWriteThenRaiseRepository(InMemoryInvestigationRepository):
        def save_multi_agent_result(
            self, investigation_id, findings, executions, review
        ):
            if review is not None:
                review = review.model_copy(
                    update={"uncertainty": "mutated uncertainty"}
                )
            saved = super().save_multi_agent_result(
                investigation_id, findings, executions, review
            )
            if review is not None:
                raise RuntimeError("review acknowledgement failed")
            return saved

    repository = MutatingWriteThenRaiseRepository()
    report_generator = _CapturingReportGenerator()

    record = _v7_orchestrator(
        repository,
        _ReportRuntime(repository),
        report_generator,
    ).run(load_incident_case("deployment_regression"))

    assert record.status == InvestigationStatus.COMPLETED
    assert report_generator.kwargs["coordination_review"] is None
    assert report_generator.kwargs["agent_findings"] == []
    assert report_generator.kwargs["multi_agent_run"].status == (
        MultiAgentRunStatus.FAILED
    )


def _evidence(
    evidence_id: str,
    summary: str,
    *,
    timestamp: str = "2026-07-03T14:00:00+08:00",
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.DEPLOY,
        kind=EvidenceKind.DEPLOYMENT,
        timestamp=datetime.fromisoformat(timestamp),
        summary=summary,
    )


def _v7_baseline():
    event = load_incident_case("deployment_regression")
    evidence = [
        _evidence("ev-baseline", "baseline evidence"),
        _evidence("ev-agent", "agent evidence"),
        _evidence("ev-review", "review evidence"),
    ]
    hypotheses = [
        _hypothesis(
            confidence=0.88,
            supporting_evidence_ids=["ev-baseline"],
        )
    ]
    return event, evidence, hypotheses


def _v7_review(
    *,
    run_status=MultiAgentRunStatus.COMPLETED,
    decision_status=CoordinationDecisionStatus.CONFLICT,
):
    round_1 = AgentFinding(
        id="finding-v7-r1",
        investigation_id="inv-1",
        agent_name=AgentName.LOG,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="Round one agent summary.",
        confidence=0.82,
        evidence_ids=["ev-agent"],
        related_cause_type=CauseType.TRAFFIC_SPIKE,
        rationale="private chain of thought",
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
    )
    round_2 = round_1.model_copy(
        update={
            "id": "finding-v7-r2",
            "summary": "Round two revised summary.",
            "evidence_ids": ["ev-review"],
            "analysis_round": 2,
            "revises_finding_id": round_1.id,
        }
    )
    selected_cause = (
        None
        if decision_status == CoordinationDecisionStatus.CONFLICT
        else CauseType.TRAFFIC_SPIKE
    )
    review = CoordinationReview(
        investigation_id="inv-1",
        candidates=[
            RootCauseCandidate(
                cause_type=CauseType.TRAFFIC_SPIKE,
                summary="Agent candidate.",
                rank=1,
                confidence=0.84,
                supporting_finding_ids=[round_2.id],
                supporting_evidence_ids=["ev-review"],
            )
        ],
        execution_layer=AgentExecutionLayer.OPENAI_AGENTS_SDK,
        run_status=run_status,
        decision_status=decision_status,
        baseline_cause_type=CauseType.DEPLOYMENT_REGRESSION,
        selected_cause_type=selected_cause,
        summary="协调摘要",
        uncertainty="需要人工比对冲突证据",
    )
    return [round_1, round_2], review


def _section(markdown: str, heading: str, next_heading: str) -> str:
    return markdown.split(f"## {heading}", 1)[1].split(f"## {next_heading}", 1)[0]


class _CapturingReportGenerator(ReportGenerator):
    def __init__(self):
        self.kwargs = {}

    def generate(self, *args, **kwargs):
        self.kwargs = kwargs
        return super().generate(*args, **kwargs)


class _ReportRuntime:
    def __init__(self, repository):
        self.repository = repository

    async def run(self, *, investigation_id, **_kwargs):
        persisted = self.repository.get(investigation_id)
        failed = AgentsRcaRuntimeResult.failed(investigation_id, "stub failure")
        task = failed.tasks[0].model_copy(
            update={
                "id": "task-v7-report",
                "status": DiagnosisTaskStatus.COMPLETED,
            }
        )
        execution = failed.executions[0].model_copy(
            update={
                "id": "execution-v7-report",
                "task_id": task.id,
                "status": AgentExecutionStatus.COMPLETED,
                "error_message": None,
            }
        )
        finding = self.repository.list_agent_findings(investigation_id)[0].model_copy(
            update={
                "id": "finding-v7-report",
                "finding_type": AgentFindingType.ROOT_CAUSE,
                "related_cause_type": persisted.hypotheses[0].cause_type,
                "confidence": 0.9,
                "execution_layer": AgentExecutionLayer.OPENAI_AGENTS_SDK,
            }
        )
        review = build_hybrid_coordination_review(
            investigation_id,
            [finding],
            persisted.evidence,
            persisted.hypotheses,
            MultiAgentRunStatus.COMPLETED,
            "Persisted V7 review",
            "Review uncertainty",
            model_provider=ModelProvider.OPENAI,
            model_name="gpt-test",
        )
        self.result = AgentsRcaRuntimeResult(
            tasks=[task],
            executions=[execution],
            findings=[finding],
            review=review,
            run_summary=MultiAgentRunSummary(
                status=MultiAgentRunStatus.COMPLETED,
                model_provider=ModelProvider.OPENAI,
                model_name="gpt-test",
            ),
        )
        return self.result


def _v7_orchestrator(repository, runtime, report_generator):
    providers = build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=report_generator,
        coordinator=DiagnosisCoordinator(providers),
        action_planner=ActionPlanner(),
        agents_runtime=runtime,
    )


def _hypothesis(
    *,
    cause_type: CauseType = CauseType.DEPLOYMENT_REGRESSION,
    summary: str = "A recent deployment is the likely cause.",
    confidence: float = 0.88,
    supporting_evidence_ids: list[str] | None = None,
    contradicting_evidence_ids: list[str] | None = None,
) -> Hypothesis:
    return Hypothesis(
        cause_type=cause_type,
        summary=summary,
        confidence=confidence,
        supporting_evidence_ids=supporting_evidence_ids or [],
        contradicting_evidence_ids=contradicting_evidence_ids or [],
        next_actions=["Compare with the previous deployment."],
    )


def test_report_escapes_dynamic_fields_and_shows_evidence_id_and_status():
    event, evidence, hypotheses = _v7_baseline()
    event = event.model_copy(
        update={"service": "checkout ## forged", "title": "Bearer report-secret"}
    )
    evidence[0] = evidence[0].model_copy(
        update={"summary": "## injected\nowner@example.com"}
    )

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert "report-secret" not in report.markdown
    assert "owner@example.com" not in report.markdown
    assert "\n## injected" not in report.markdown
    assert "`ev-baseline` [`success`] [`deploy/deployment`]" in report.markdown
