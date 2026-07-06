from backend.domain.actions import RecommendedAction, VerificationSuggestion
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.reports import IncidentReport


class ReportGenerator:
    def generate(
        self,
        investigation_id: str,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        actions: list[RecommendedAction] | None = None,
        verification_suggestions: list[VerificationSuggestion] | None = None,
    ) -> IncidentReport:
        if not hypotheses:
            raise ValueError("hypotheses must contain at least one item")

        actions = actions or []
        verification_suggestions = verification_suggestions or []
        sorted_evidence = sorted(evidence, key=lambda item: item.timestamp)
        self._validate_evidence_ids(sorted_evidence, hypotheses, actions)

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
            f"# {event.service} RCA 诊断报告",
            "",
            "## 摘要",
            "",
            f"- 服务：`{event.service}`",
            f"- 环境：`{event.environment}`",
            f"- 严重级别：`{event.severity}`",
            f"- 开始时间：`{event.started_at.isoformat()}`",
            f"- 结论：{top.summary}",
            f"- 置信度：{top.confidence:.2f}",
            "",
            "## 最可能根因",
            "",
            f"- 类型：`{top.cause_type}`",
            f"- 说明：{top.summary}",
            "",
            "## 证据链",
            "",
        ]

        for item in evidence:
            lines.append(
                f"- `{item.timestamp.isoformat()}` [{item.provider}/{item.kind}] "
                f"{item.summary}"
            )

        lines.extend(["", "## 支持该结论的证据", ""])
        for item in supporting_evidence:
            lines.append(f"- {item.summary}")

        if contradicting_evidence:
            lines.extend(["", "## 反向证据/不确定性", ""])
            for item in contradicting_evidence:
                lines.append(f"- {item.summary}")

        alternative_hypotheses = hypotheses[1:]
        if alternative_hypotheses:
            lines.extend(["", "## 其他可能假设", ""])
            for hypothesis in alternative_hypotheses:
                lines.append(
                    f"- `{hypothesis.cause_type}` {hypothesis.summary} "
                    f"(置信度：{hypothesis.confidence:.2f})"
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
                lines.append(f"- `{action.id}` `{action.action_type}` {action.title}")
                lines.append(f"  - 风险等级：`{action.risk_level}`")
                lines.append(f"  - 审批要求：{approval_text}")
                lines.append(f"  - 状态：`{action.status}`")
                lines.append(f"  - 说明：{action.description}")
                lines.append(f"  - 证据：{', '.join(action.supporting_evidence_ids)}")
                lines.append("  - 执行状态：V2 未执行该动作")
            return

        if top.next_actions:
            for action in top.next_actions:
                lines.append(f"- {action}")
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
                lines.append(f"- `{action.id}` {action.title} (`{action.risk_level}`)")
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
                lines.append(f"- `{suggestion.id}` `{suggestion.status}` {suggestion.title}")
                lines.append(f"  - 说明：{suggestion.description}")
                lines.append(f"  - 期望信号：{suggestion.expected_signal}")
        else:
            lines.append("- 当前没有验证建议")

    def _validate_evidence_ids(
        self,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        actions: list[RecommendedAction],
    ) -> None:
        evidence_ids = {item.id for item in evidence}
        referenced_ids: list[str] = []

        for hypothesis in hypotheses:
            referenced_ids.extend(hypothesis.supporting_evidence_ids)
            referenced_ids.extend(hypothesis.contradicting_evidence_ids)

        for action in actions:
            referenced_ids.extend(action.supporting_evidence_ids)

        for evidence_id in referenced_ids:
            if evidence_id not in evidence_ids:
                raise ValueError(f"missing evidence id: {evidence_id}")
