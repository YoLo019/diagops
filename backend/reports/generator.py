import re

from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.agent_findings import AgentFinding, CoordinationReview
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.domain.reports import IncidentReport
from backend.safety.redaction import (
    escape_markdown,
    escape_markdown_code,
    redact_model,
    safe_failure,
)

DECISION_LABELS = {
    "agreement": "多 Agent 复核一致",
    "conflict": "存在冲突，需要人工确认",
    "agent_leads": "多 Agent 主要候选，尚未确认",
    "fallback": "多 Agent 复核未完成，以下为确定性 RCA 结果",
}


class ReportGenerator:
    def generate(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        actions: list[RecommendedAction] | None = None,
        verification_suggestions: list[VerificationSuggestion] | None = None,
        coordination_review: CoordinationReview | None = None,
        multi_agent_run: MultiAgentRunSummary | None = None,
        agent_findings: list[AgentFinding] | None = None,
    ) -> IncidentReport:
        if not hypotheses:
            raise ValueError("hypotheses must contain at least one item")

        event = redact_model(event)
        hypotheses = [redact_model(item) for item in hypotheses]
        actions = [redact_model(item) for item in (actions or [])]
        verification_suggestions = [
            redact_model(item) for item in (verification_suggestions or [])
        ]
        agent_findings = [redact_model(item) for item in (agent_findings or [])]
        coordination_review = (
            redact_model(coordination_review)
            if coordination_review is not None
            else None
        )
        multi_agent_run = (
            redact_model(multi_agent_run) if multi_agent_run is not None else None
        )
        sorted_evidence = sorted(
            (redact_model(item) for item in evidence), key=lambda item: item.timestamp
        )
        self._validate_evidence_ids(
            investigation_id,
            sorted_evidence,
            hypotheses,
            actions,
            coordination_review,
            agent_findings,
        )

        top = hypotheses[0]
        timeline = [
            {"time": item.timestamp.isoformat(), "event": item.summary}
            for item in sorted_evidence
        ]

        markdown = self._render_markdown(
            event,
            sorted_evidence,
            hypotheses,
            actions,
            verification_suggestions,
            coordination_review,
            multi_agent_run,
            agent_findings,
        )

        return IncidentReport(
            investigation_id=investigation_id,
            summary=top.summary,
            timeline=timeline,
            hypotheses=hypotheses,
            markdown=markdown,
            action_ids=[action.id for action in actions],
            verification_suggestion_ids=[
                suggestion.id for suggestion in verification_suggestions
            ],
        )

    def _render_markdown(
        self,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        actions: list[RecommendedAction],
        verification_suggestions: list[VerificationSuggestion],
        coordination_review: CoordinationReview | None,
        multi_agent_run: MultiAgentRunSummary | None,
        agent_findings: list[AgentFinding],
    ) -> str:
        top = hypotheses[0]
        evidence_by_id = {item.id: item for item in evidence}
        supporting_evidence = [
            evidence_by_id[evidence_id] for evidence_id in top.supporting_evidence_ids
        ]
        contradicting_evidence = [
            evidence_by_id[evidence_id] for evidence_id in top.contradicting_evidence_ids
        ]

        lines = [
            f"# {escape_markdown(event.service)} RCA 诊断报告",
            "",
            "## 摘要",
            "",
            f"- 服务：{_code(event.service)}",
            f"- 环境：{_code(event.environment)}",
            f"- 严重级别：{_code(event.severity)}",
            f"- 开始时间：{_code(event.started_at.isoformat())}",
            f"- 结论：{escape_markdown(top.summary)}",
            f"- 置信度：{escape_markdown(f'{top.confidence:.2f}')}",
            "",
            "## 最可能根因",
            "",
            f"- 类型：{_code(top.cause_type)}",
            f"- 说明：{escape_markdown(top.summary)}",
            "",
            "## 证据链",
            "",
        ]

        for item in evidence:
            lines.append(_evidence_line(item))

        lines.extend(["", "## 支持该结论的证据", ""])
        for item in supporting_evidence:
            lines.append(_evidence_line(item))

        if contradicting_evidence:
            lines.extend(["", "## 反向证据/不确定性", ""])
            for item in contradicting_evidence:
                lines.append(_evidence_line(item))

        alternative_hypotheses = hypotheses[1:]
        if alternative_hypotheses:
            lines.extend(["", "## 其他可能假设", ""])
            for hypothesis in alternative_hypotheses:
                lines.append(
                    f"- {_code(hypothesis.cause_type)} "
                    f"{escape_markdown(hypothesis.summary)} "
                    f"(置信度：{escape_markdown(f'{hypothesis.confidence:.2f}')})"
                )

        self._append_v7_section(
            lines,
            top,
            coordination_review,
            multi_agent_run,
            agent_findings,
        )
        self._append_actions_section(lines, actions, top)
        self._append_approval_section(lines, actions)
        self._append_verification_section(lines, verification_suggestions)

        lines.extend(["", "## 事实与推断", ""])
        lines.append("### 观察到的事实")
        lines.append("- 事件输入描述了服务、环境、严重级别、开始时间和告警信号。")
        lines.append("- 证据链仅包含输入 evidence 中真实存在的 Provider 结构化证据。")
        lines.append("### 推断结论/不确定性")
        lines.append("- 最可能根因来自 RCA 假设排序，需要工程师结合现场进一步确认。")
        lines.append("- 反向证据会作为不确定性列出；缺失的证据引用会中止报告生成。")
        lines.append("- V2 不会自动执行回滚、重启、扩容、配置变更等生产操作。")

        return "\n".join(lines)

    def _append_v7_section(
        self,
        lines: list[str],
        top: Hypothesis,
        review: CoordinationReview | None,
        run: MultiAgentRunSummary | None,
        findings: list[AgentFinding],
    ) -> None:
        if run is None:
            return

        is_v7_review = (
            review is not None
            and review.execution_layer == AgentExecutionLayer.OPENAI_AGENTS_SDK
            and review.run_status == run.status
            and run.status
            in {MultiAgentRunStatus.COMPLETED, MultiAgentRunStatus.PARTIAL}
        )
        decision = (
            review.decision_status.value
            if is_v7_review and review.decision_status is not None
            else "fallback"
        )
        lines.extend(
            [
                "",
                "## 混合 RCA 裁决",
                "",
                f"- 运行状态：{_code(run.status)}",
            ]
        )
        if run.failure_reason or not is_v7_review:
            lines.append(
                f"- 安全失败分类：{_code(safe_failure(run.status.value))}"
            )

        lines.extend(
            [
                "",
                "### 确定性推断",
                "",
                f"- 基线原因：{_code(top.cause_type)}",
                f"- 基线置信度：{escape_markdown(f'{top.confidence:.2f}')}",
            ]
        )
        if is_v7_review and review.baseline_cause_type is not None:
            lines.append(f"- review 基线：{_code(review.baseline_cause_type)}")

        lines.extend(
            [
                "",
                "### Agent 推断",
                "",
                f"- 裁决：{DECISION_LABELS[decision]}",
            ]
        )
        if is_v7_review:
            selected = (
                _code(review.selected_cause_type)
                if review.selected_cause_type is not None
                else "无（保留冲突）"
            )
            lines.append(f"- 选定原因：{selected}")
            if review.summary:
                lines.append(f"- Review 摘要：{_safe_v7_text(review.summary)}")
        else:
            lines.append(f"- V7 选定原因：无（沿用确定性结果 {_code(top.cause_type)}）")
            lines.append("- 未用 V7 review 替换确定性 RCA。")

        for finding in sorted(
            findings,
            key=lambda item: (item.agent_name.value, item.analysis_round, item.created_at),
        ):
            cause = (
                f" 候选：{_code(finding.related_cause_type)}"
                if finding.related_cause_type is not None
                else ""
            )
            lines.append(
                f"- {_code(finding.agent_name)} 第 "
                f"{escape_markdown(finding.analysis_round)} 轮："
                f"{_safe_v7_text(finding.summary)}{cause}"
            )
            if finding.evidence_ids:
                evidence_ids = "、".join(_code(item) for item in finding.evidence_ids)
                lines.append(f"  - Evidence IDs：{evidence_ids}")
            if finding.revises_finding_id is not None:
                lines.append(f"  - 修订自：{_code(finding.revises_finding_id)}")

        if is_v7_review:
            review_evidence_ids = list(
                dict.fromkeys(
                    evidence_id
                    for candidate in review.candidates
                    for evidence_id in (
                        candidate.supporting_evidence_ids
                        + candidate.contradicting_evidence_ids
                    )
                )
            )
            if review_evidence_ids:
                rendered = "、".join(_code(item) for item in review_evidence_ids)
                lines.append(f"- Review Evidence IDs：{rendered}")

        uncertainty = (
            _safe_v7_text(review.uncertainty)
            if is_v7_review and review.uncertainty
            else "V7 复核未完成，仅保留确定性 RCA 推断，需要工程师确认。"
        )
        lines.extend(["", "### 不确定性", "", f"- {uncertainty}"])

    def _append_actions_section(
        self,
        lines: list[str],
        actions: list[RecommendedAction],
        top: Hypothesis,
    ) -> None:
        lines.extend(["", "## 建议动作", ""])
        if actions:
            for action in actions:
                approval_text = "需要人工审批" if action.requires_approval else "不需要审批"
                lines.append(
                    f"- {_code(action.id)} {_code(action.action_type)} "
                    f"{escape_markdown(action.title)}"
                )
                lines.append(f"  - 风险等级：{_code(action.risk_level)}")
                lines.append(f"  - 审批要求：{approval_text}")
                lines.append(f"  - 状态：{_code(action.status)}")
                lines.append(f"  - 说明：{escape_markdown(action.description)}")
                lines.append(
                    "  - 证据："
                    + ", ".join(_code(item) for item in action.supporting_evidence_ids)
                )
                lines.append("  - 执行状态：V2 未执行该动作")
            return

        if top.next_actions:
            for action in top.next_actions:
                lines.append(f"- {escape_markdown(action)}")
            lines.append("- 执行状态：V2 未执行上述动作")
        else:
            lines.append("- 当前没有建议动作")

    def _append_approval_section(
        self,
        lines: list[str],
        actions: list[RecommendedAction],
    ) -> None:
        approval_actions = [action for action in actions if action.requires_approval]
        lines.extend(["", "## 需要审批的动作", ""])
        if approval_actions:
            for action in approval_actions:
                lines.append(
                    f"- {_code(action.id)} {escape_markdown(action.title)} "
                    f"({_code(action.risk_level)})"
                )
        else:
            lines.append("- 当前没有需要审批的动作")

    def _append_verification_section(
        self,
        lines: list[str],
        verification_suggestions: list[VerificationSuggestion],
    ) -> None:
        lines.extend(["", "## 验证建议", ""])
        if verification_suggestions:
            for suggestion in verification_suggestions:
                lines.append(
                    f"- {_code(suggestion.id)} {_code(suggestion.status)} "
                    f"{escape_markdown(suggestion.title)}"
                )
                lines.append(f"  - 说明：{escape_markdown(suggestion.description)}")
                lines.append(
                    f"  - 期望信号：{escape_markdown(suggestion.expected_signal)}"
                )
        else:
            lines.append("- 当前没有验证建议")

    def _validate_evidence_ids(
        self,
        investigation_id: str,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        actions: list[RecommendedAction],
        coordination_review: CoordinationReview | None = None,
        agent_findings: list[AgentFinding] | None = None,
    ) -> None:
        evidence_ids = {item.id for item in evidence}
        agent_findings = agent_findings or []
        finding_by_id: dict[str, AgentFinding] = {}
        for finding in agent_findings:
            if finding.id in finding_by_id:
                raise ValueError(f"duplicate finding id: {finding.id}")
            if finding.investigation_id != investigation_id:
                raise ValueError(f"finding investigation mismatch: {finding.id}")
            if finding.execution_layer != AgentExecutionLayer.OPENAI_AGENTS_SDK:
                raise ValueError(f"finding execution layer mismatch: {finding.id}")
            finding_by_id[finding.id] = finding

        for finding in agent_findings:
            if finding.analysis_round != 2:
                if finding.revises_finding_id is not None:
                    raise ValueError(f"invalid finding revision: {finding.id}")
                continue
            revised = finding_by_id.get(finding.revises_finding_id)
            if (
                revised is None
                or revised.analysis_round != 1
                or revised.agent_name != finding.agent_name
                or revised.investigation_id != finding.investigation_id
            ):
                raise ValueError(f"invalid finding revision: {finding.id}")

        if (
            coordination_review is not None
            and coordination_review.investigation_id != investigation_id
        ):
            raise ValueError("review investigation mismatch")

        referenced_ids: list[str] = []

        for hypothesis in hypotheses:
            referenced_ids.extend(hypothesis.supporting_evidence_ids)
            referenced_ids.extend(hypothesis.contradicting_evidence_ids)

        for action in actions:
            referenced_ids.extend(action.supporting_evidence_ids)

        for finding in agent_findings:
            referenced_ids.extend(finding.evidence_ids)

        if coordination_review is not None:
            for candidate in coordination_review.candidates:
                referenced_ids.extend(candidate.supporting_evidence_ids)
                referenced_ids.extend(candidate.contradicting_evidence_ids)

        for evidence_id in referenced_ids:
            if evidence_id not in evidence_ids:
                raise ValueError(f"missing evidence id: {evidence_id}")

        if coordination_review is not None:
            for candidate in coordination_review.candidates:
                for finding_id in (
                    candidate.supporting_finding_ids
                    + candidate.contradicting_finding_ids
                ):
                    if finding_id not in finding_by_id:
                        raise ValueError(f"missing finding id: {finding_id}")


def _safe_v7_text(text: str) -> str:
    if re.search(
        r"(?is)(已执行|已修复|修复成功|回滚|重启|扩容|缩容|配置.*修改|"
        r"修复.*完成|配置变更|"
        r"\b(?:i|we)\s+(?:successfully\s+)?updated\s+(?:the\s+)?"
        r"(?:production\s+)?config(?:uration)?(?:\s+successfully)?\b|"
        r"\b(?:i|we)\s+(?:already\s+)?rolled\s+back\s+(?:the\s+)?"
        r"(?:deployment|service|release)\b|"
        r"\b(?:i|we)\s+(?:already\s+)?applied\s+(?:the\s+)?"
        r"config(?:uration)?\s+(?:change|update)\b|"
        r"\b(?:ssh|rollback|restart|scale|rebooted|repaired|fixed|executed)\b)",
        text,
    ):
        return "[未验证操作声明已省略]"

    return escape_markdown(text)


def _code(value: object) -> str:
    return f"`{escape_markdown_code(value)}`"


def _evidence_line(item: EvidenceItem) -> str:
    return (
        f"- {_code(item.id)} [{_code(item.status)}] "
        f"[{_code(f'{item.provider}/{item.kind}')}] {escape_markdown(item.summary)}"
    )
