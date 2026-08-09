import re

from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.agent_findings import (
    AgentFinding,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, validate_usable_evidence
from backend.domain.hypotheses import Hypothesis
from backend.domain.multi_agent import (
    AgentExecutionLayer,
    AuthorityMode,
    DiagnosticStatus,
    MultiAgentRunStatus,
    MultiAgentRunSummary,
)
from backend.domain.reports import IncidentReport
from backend.domain.v11_contracts import validate_v11_final_status
from backend.safety.redaction import (
    escape_markdown,
    escape_markdown_code,
    redact_model,
    safe_failure,
)
from backend.services.v11_public import (
    public_v11_action as _public_v11_action,
)
from backend.services.v11_public import (
    public_v11_assessment as _public_v11_assessment,
)
from backend.services.v11_public import (
    public_v11_candidate as _public_v11_candidate,
)
from backend.services.v11_public import (
    public_v11_finding as _public_v11_finding,
)
from backend.services.v11_public import (
    public_v11_lead_decision as _public_v11_lead_decision,
)
from backend.services.v11_public import (
    public_v11_verification as _public_v11_verification,
)
from backend.services.v11_public import (
    scrub_v11_text as _scrub_v11_text,
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
        v11_projection = self._is_v11_projection(coordination_review, multi_agent_run)
        if not hypotheses and not v11_projection:
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
            verification_suggestions,
        )

        if v11_projection:
            return self.generate_v11(
                investigation_id=investigation_id,
                event=event,
                evidence=sorted_evidence,
                actions=actions,
                verification_suggestions=verification_suggestions,
                coordination_review=coordination_review,
                multi_agent_run=multi_agent_run,
                agent_findings=agent_findings,
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

    def generate_v11(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        *,
        actions: list[RecommendedAction] | None = None,
        verification_suggestions: list[VerificationSuggestion] | None = None,
        coordination_review: CoordinationReview | None = None,
        multi_agent_run: MultiAgentRunSummary | None = None,
        agent_findings: list[AgentFinding] | None = None,
    ) -> IncidentReport:
        """按 V11 review/runtime 投影生成报告，不进入旧 hypotheses 分支。"""
        event = redact_model(event)
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
            [],
            actions,
            coordination_review,
            agent_findings,
            verification_suggestions,
        )
        return self._generate_v11(
            investigation_id=investigation_id,
            event=event,
            evidence=sorted_evidence,
            actions=actions,
            verification_suggestions=verification_suggestions,
            coordination_review=coordination_review,
            multi_agent_run=multi_agent_run,
            agent_findings=agent_findings,
        )

    @staticmethod
    def _is_v11_projection(
        review: CoordinationReview | None,
        run: MultiAgentRunSummary | None,
    ) -> bool:
        return bool(
            review is not None and review.authority_mode == AuthorityMode.AGENT
            or run is not None and run.authority_mode == AuthorityMode.AGENT
        )

    def _generate_v11(
        self,
        *,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        actions: list[RecommendedAction],
        verification_suggestions: list[VerificationSuggestion],
        coordination_review: CoordinationReview | None,
        multi_agent_run: MultiAgentRunSummary | None,
        agent_findings: list[AgentFinding],
    ) -> IncidentReport:
        if coordination_review is None or multi_agent_run is None:
            raise ValueError("V11 report requires a coordination review and run summary")
        if (
            coordination_review.authority_mode != AuthorityMode.AGENT
            or multi_agent_run.authority_mode != AuthorityMode.AGENT
            or coordination_review.runtime_run_id is None
            or multi_agent_run.runtime_run_id is None
            or coordination_review.runtime_run_id != multi_agent_run.runtime_run_id
        ):
            raise ValueError("V11 report authority and runtime owner must match")
        validate_v11_final_status(coordination_review, multi_agent_run)

        candidates_by_id = {
            candidate.id: _public_v11_candidate(candidate)
            for candidate in coordination_review.candidates
        }
        accepted_ids = coordination_review.authoritative_candidate_ids
        diagnoses = [candidates_by_id[item] for item in accepted_ids if item in candidates_by_id]
        alternatives = [
            candidates_by_id[candidate.id]
            for candidate in coordination_review.candidates
            if candidate.id not in set(accepted_ids)
        ]
        if coordination_review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE:
            diagnoses = []
            alternatives = []
        actions = [_public_v11_action(item) for item in actions]
        verification_suggestions = [
            _public_v11_verification(item) for item in verification_suggestions
        ]
        critic_assessments = [
            _public_v11_assessment(item)
            for item in coordination_review.critic_assessments
        ]
        public_findings = [_public_v11_finding(item) for item in agent_findings]
        safe_review_summary = _scrub_v11_text(coordination_review.summary)

        safe_review = coordination_review.model_copy(
            update={
                "critic_assessments": critic_assessments,
                "lead_decision": (
                    _public_v11_lead_decision(coordination_review.lead_decision)
                    if coordination_review.lead_decision is not None
                    else None
                ),
                "summary": safe_review_summary,
                "uncertainty": _scrub_v11_text(coordination_review.uncertainty),
            }
        )
        summary = (
            diagnoses[0].summary
            if diagnoses
            else safe_review_summary
            or _scrub_v11_text(multi_agent_run.failure_reason)
            or "V11 没有形成可接受的诊断结论。"
        )
        markdown = self._render_v11_markdown(
            event,
            evidence,
            diagnoses,
            alternatives,
            actions,
            verification_suggestions,
            safe_review,
            multi_agent_run,
            public_findings,
        )
        evidence_gaps = list(
            dict.fromkeys(
                assessment.gap
                for assessment in critic_assessments
                if assessment.gap
            )
        )[:32]
        return IncidentReport(
            investigation_id=investigation_id,
            summary=summary,
            timeline=[
                {"time": item.timestamp.isoformat(), "event": item.summary}
                for item in evidence
            ],
            hypotheses=[],
            markdown=markdown,
            action_ids=[action.id for action in actions],
            verification_suggestion_ids=[
                suggestion.id for suggestion in verification_suggestions
            ],
            diagnoses=diagnoses,
            alternatives=alternatives,
            diagnostic_status=safe_review.diagnostic_status,
            authority_mode=AuthorityMode.AGENT,
            critic_assessments=critic_assessments,
            critic_summary=safe_review_summary or None,
            evidence_gaps=evidence_gaps,
            total_input_tokens=multi_agent_run.total_input_tokens,
            total_output_tokens=multi_agent_run.total_output_tokens,
            elapsed_time_ms=multi_agent_run.elapsed_time_ms,
            runtime_run_id=multi_agent_run.runtime_run_id,
        )

    def _render_v11_markdown(
        self,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        diagnoses: list[RootCauseCandidate],
        alternatives: list[RootCauseCandidate],
        actions: list[RecommendedAction],
        verification_suggestions: list[VerificationSuggestion],
        review: CoordinationReview,
        run: MultiAgentRunSummary,
        findings: list[AgentFinding],
    ) -> str:
        evidence_by_id = {item.id: item for item in evidence}
        status = review.diagnostic_status.value if review.diagnostic_status else "pending"
        conclusion = (
            diagnoses[0].summary
            if diagnoses
            else review.summary or "无可接受诊断结论"
        )
        lines = [
            f"# {escape_markdown(event.service)} RCA 诊断报告",
            "",
            "## V11 诊断摘要",
            "",
            f"- 权威模式：{_code(AuthorityMode.AGENT)}",
            f"- 诊断状态：{_code(status)}",
            f"- 运行 ID：{_code(run.runtime_run_id)}",
            f"- 服务：{_code(event.service)}",
            f"- 环境：{_code(event.environment)}",
            f"- 结论：{_safe_v11_text(conclusion)}",
            "",
            "## 接受的诊断",
            "",
        ]
        if diagnoses:
            for candidate in diagnoses:
                lines.extend(self._v11_candidate_lines(candidate, evidence_by_id))
        else:
            lines.append("- 当前没有接受的诊断候选。")

        lines.extend(["", "## 备选候选", ""])
        if alternatives:
            for candidate in alternatives:
                lines.extend(self._v11_candidate_lines(candidate, evidence_by_id))
        else:
            lines.append("- 当前没有备选候选。")

        lines.extend(["", "## Critic 评估", ""])
        if review.critic_assessments:
            for assessment in review.critic_assessments:
                lines.append(
                    f"- {_code(assessment.candidate_id)} {_code(assessment.verdict)} "
                    f"{_safe_v11_text(assessment.summary)} "
                    f"（第 {assessment.review_round} 轮，Actor：{_code('CriticAgent')}）"
                )
                if assessment.supplemental_task_ids:
                    lines.append(
                        "  - Task IDs："
                        + ", ".join(
                            _code(task_id)
                            for task_id in assessment.supplemental_task_ids
                        )
                    )
                for check in assessment.checks:
                    lines.append(
                        f"  - {_code(check.name)}：{_code(check.status)} "
                        f"{_safe_v11_text(check.summary)}"
                    )
        else:
            lines.append("- 当前没有 Critic 评估。")

        lines.extend(["", "## Investigator Findings", ""])
        if findings:
            for finding in findings:
                actor = _code(str(finding.agent_name))
                instance = (
                    f"，Instance：{_code(finding.agent_instance_id)}"
                    if finding.agent_instance_id
                    else ""
                )
                lines.append(f"- {_code(finding.id)}（Actor：{actor}{instance}）")
                lines.append(
                    f"  - Task ID：{_code(finding.task_id) if finding.task_id else '未指定'}"
                )
                lines.append(f"  - analysis round：{finding.analysis_round}")
                lines.append(f"  - 类型：{_code(finding.finding_type)}")
                lines.append(f"  - 摘要：{_safe_v11_text(finding.summary)}")
                if finding.rationale:
                    lines.append(f"  - 公开依据：{_safe_v11_text(finding.rationale)}")
                if finding.evidence_ids:
                    lines.append(
                        "  - 证据："
                        + ", ".join(_code(item) for item in finding.evidence_ids)
                    )
        else:
            lines.append("- 当前没有 Investigator finding。")

        gaps = [assessment.gap for assessment in review.critic_assessments if assessment.gap]
        lines.extend(["", "## 证据缺口", ""])
        lines.extend(f"- {_safe_v11_text(gap)}" for gap in dict.fromkeys(gaps))
        if not gaps:
            lines.append("- 未记录证据缺口。")

        lines.extend(["", "## 证据链", ""])
        lines.extend(_evidence_line(item) for item in evidence)
        self._append_v11_actions_section(lines, actions, verification_suggestions)

        lines.extend(["", "## Lead 决策", ""])
        if review.lead_decision is not None:
            decision = review.lead_decision
            lines.append(f"- Action：{_code(decision.action)}")
            lines.append(f"- 摘要：{_safe_v11_text(decision.summary)}")
            lines.append(
                "- Candidate IDs："
                + ", ".join(_code(item) for item in decision.candidate_ids)
                if decision.candidate_ids
                else "- Candidate IDs：无"
            )
            lines.append(
                "- Evidence IDs："
                + ", ".join(_code(item) for item in decision.evidence_ids)
                if decision.evidence_ids
                else "- Evidence IDs：无"
            )
            lines.append(
                "- Task IDs："
                + ", ".join(_code(item) for item in decision.task_ids)
                if decision.task_ids
                else "- Task IDs：无"
            )
            if decision.stop_reason:
                lines.append(f"- Stop reason：{_safe_v11_text(decision.stop_reason)}")
        else:
            lines.append("- 当前没有最终 Lead decision。")

        finding_task_ids = [
            finding.task_id for finding in findings if finding.task_id is not None
        ]
        review_task_ids = [
            task_id
            for assessment in review.critic_assessments
            for task_id in assessment.supplemental_task_ids
        ]
        lead_task_ids = (
            review.lead_decision.task_ids if review.lead_decision is not None else []
        )
        task_ids = list(dict.fromkeys(finding_task_ids + review_task_ids + lead_task_ids))
        actors = list(dict.fromkeys(str(finding.agent_name) for finding in findings))
        if review.critic_assessments:
            actors.append("CriticAgent")
        if review.lead_decision is not None:
            actors.append("LeadAgent")
        actor_text = (
            ", ".join(_code(actor) for actor in dict.fromkeys(actors))
            if actors
            else "无"
        )
        task_text = (
            ", ".join(_code(task_id) for task_id in task_ids)
            if task_ids
            else "无"
        )
        lines.extend(
            [
                "",
                "## 运行信息",
                "",
                f"- Actor：{actor_text}",
                f"- Task IDs：{task_text}",
                f"- Investigator 数量：{run.investigator_count}",
                f"- 完成轮次：{run.completed_rounds}",
                f"- 输入 tokens：{run.total_input_tokens}",
                f"- 输出 tokens：{run.total_output_tokens}",
                f"- 耗时毫秒：{run.elapsed_time_ms}",
                f"- 失败信息：{_safe_v11_text(run.failure_reason) if run.failure_reason else '无'}",
            ]
        )
        if not diagnoses and review.diagnostic_status == DiagnosticStatus.INCONCLUSIVE:
            lines.extend(["", "- not_activated：没有激活可接受的 V11 诊断投影。"])
        return "\n".join(lines)

    @staticmethod
    def _v11_candidate_lines(
        candidate: RootCauseCandidate,
        evidence_by_id: dict[str, EvidenceItem],
    ) -> list[str]:
        lines = [
            f"- {_code(candidate.id)}（置信度：{candidate.confidence:.2f}）",
            f"  - 受影响实体：{_safe_v11_text(candidate.affected_entity or '未指定')}",
            f"  - 失效机制：{_safe_v11_text(candidate.failure_mechanism or '未指定')}",
            f"  - 说明：{_safe_v11_text(candidate.summary)}",
        ]
        if candidate.supporting_evidence_ids:
            lines.append(
                "  - 支持证据："
                + ", ".join(
                    _code(evidence_by_id[item].id)
                    for item in candidate.supporting_evidence_ids
                    if item in evidence_by_id
                )
            )
        if candidate.contradicting_evidence_ids:
            lines.append(
                "  - 反向证据："
                + ", ".join(
                    _code(evidence_by_id[item].id)
                    for item in candidate.contradicting_evidence_ids
                    if item in evidence_by_id
                )
            )
        if candidate.uncertainty:
            lines.append(f"  - 不确定性：{_safe_v11_text(candidate.uncertainty)}")
        return lines

    @staticmethod
    def _append_v11_actions_section(
        lines: list[str],
        actions: list[RecommendedAction],
        verification_suggestions: list[VerificationSuggestion],
    ) -> None:
        lines.extend(["", "## 建议动作与验证", ""])
        if actions:
            for action in actions:
                lines.append(
                    f"- {_code(action.id)} {_safe_v11_text(action.title)} "
                    f"（{_code(action.risk_level)}，只读建议）"
                )
                lines.append(
                    "  - 候选："
                    + ", ".join(_code(item) for item in action.related_candidate_ids)
                )
                lines.append(
                    "  - 证据："
                    + ", ".join(_code(item) for item in action.supporting_evidence_ids)
                )
                lines.append(f"  - 说明：{_safe_v11_text(action.description)}")
        else:
            lines.append("- 当前没有建议动作。")
        if verification_suggestions:
            lines.append("- 验证建议：")
            for suggestion in verification_suggestions:
                candidate_refs = ", ".join(
                    _code(item) for item in suggestion.related_candidate_ids
                )
                lines.append(
                    f"  - {_code(suggestion.id)} {_safe_v11_text(suggestion.title)} "
                    f"（候选：{candidate_refs}）"
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
        verification_suggestions: list[VerificationSuggestion] | None = None,
    ) -> None:
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

        if (
            coordination_review is not None
            and coordination_review.authority_mode == AuthorityMode.AGENT
        ):
            runtime_run_id = coordination_review.runtime_run_id
            if runtime_run_id is None:
                raise ValueError("V11 review lacks runtime owner")
            if any(item.runtime_run_id != runtime_run_id for item in evidence):
                raise ValueError("V11 report evidence owner mismatch")
            candidate_ids = {item.id for item in coordination_review.candidates}
            accepted_candidate_ids = set(
                coordination_review.authoritative_candidate_ids
            )
            for action in actions:
                if action.runtime_run_id != runtime_run_id:
                    raise ValueError("V11 action owner mismatch")
                if not action.related_candidate_ids:
                    raise ValueError("V11 action lacks a candidate reference")
                if not set(action.related_candidate_ids) <= accepted_candidate_ids:
                    raise ValueError("V11 action references a non-accepted candidate")
                if not set(action.related_candidate_ids) <= candidate_ids:
                    raise ValueError("V11 action references an unknown candidate")
            for suggestion in verification_suggestions or []:
                if suggestion.runtime_run_id != runtime_run_id:
                    raise ValueError("V11 verification owner mismatch")
                if not suggestion.related_candidate_ids:
                    raise ValueError("V11 verification lacks a candidate reference")
                if not set(suggestion.related_candidate_ids) <= accepted_candidate_ids:
                    raise ValueError(
                        "V11 verification references a non-accepted candidate"
                    )
                if not set(suggestion.related_candidate_ids) <= candidate_ids:
                    raise ValueError(
                        "V11 verification references an unknown candidate"
                    )
            for finding in agent_findings:
                if finding.runtime_run_id != runtime_run_id:
                    raise ValueError("V11 finding owner mismatch")
            for assessment in coordination_review.critic_assessments:
                if assessment.candidate_id not in candidate_ids:
                    raise ValueError("Critic references an unknown candidate")

        referenced_ids: list[str] = []

        for hypothesis in hypotheses:
            referenced_ids.extend(hypothesis.supporting_evidence_ids)
            referenced_ids.extend(hypothesis.contradicting_evidence_ids)

        for action in actions:
            referenced_ids.extend(action.supporting_evidence_ids)

        for suggestion in verification_suggestions or []:
            referenced_ids.extend(suggestion.result_evidence_ids)

        for finding in agent_findings:
            referenced_ids.extend(finding.evidence_ids)

        if coordination_review is not None:
            for candidate in coordination_review.candidates:
                referenced_ids.extend(candidate.supporting_evidence_ids)
                referenced_ids.extend(candidate.contradicting_evidence_ids)
            for assessment in coordination_review.critic_assessments:
                referenced_ids.extend(assessment.supporting_evidence_ids)
                referenced_ids.extend(assessment.contradicting_evidence_ids)
                for check in assessment.checks:
                    referenced_ids.extend(check.evidence_ids)

        if (
            coordination_review is not None
            and coordination_review.authority_mode == AuthorityMode.AGENT
        ):
            validate_usable_evidence(
                evidence,
                referenced_ids,
                runtime_run_id=coordination_review.runtime_run_id,
            )
        else:
            evidence_ids = {item.id for item in evidence}
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


def _safe_v11_text(text: str) -> str:
    """只展示有界的结构化结论，避免把 prompt 或私有推理带入报告。"""
    return escape_markdown(_scrub_v11_text(text))


def _code(value: object) -> str:
    return f"`{escape_markdown_code(value)}`"


def _evidence_line(item: EvidenceItem) -> str:
    return (
        f"- {_code(item.id)} [{_code(item.status)}] "
        f"[{_code(f'{item.provider}/{item.kind}')}] {escape_markdown(item.summary)}"
    )
