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
    ) -> IncidentReport:
        if not hypotheses:
            raise ValueError("hypotheses must contain at least one item")

        sorted_evidence = sorted(evidence, key=lambda item: item.timestamp)
        self._validate_top_evidence_ids(sorted_evidence, hypotheses[0])

        top = hypotheses[0]
        timeline = [
            {"time": item.timestamp.isoformat(), "event": item.summary}
            for item in sorted_evidence
        ]

        markdown = self._render_markdown(event, sorted_evidence, hypotheses)

        return IncidentReport(
            investigation_id=investigation_id,
            summary=top.summary,
            timeline=timeline,
            hypotheses=hypotheses,
            markdown=markdown,
        )

    def _render_markdown(
        self,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
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
                f"- `{item.timestamp.isoformat()}` [{item.provider}/{item.kind}] {item.summary}"
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

        lines.extend(["", "## 建议动作", ""])
        for action in top.next_actions:
            lines.append(f"- {action}")

        lines.extend(["", "## 事实与推断", ""])
        lines.append("### 观察到的事实")
        lines.append("- 事件输入描述了服务、环境、严重级别、开始时间和告警信号。")
        lines.append("- 证据链仅包含输入 evidence 中真实存在的 Provider 结构化证据。")
        lines.append("### 推断结论/不确定性")
        lines.append("- 最可能根因来自 RCA 假设排序，需要工程师结合现场进一步确认。")
        lines.append("- 反向证据会作为不确定性列出；缺失的证据引用会中止报告生成。")
        lines.append("- 系统不会自动执行回滚、重启、扩容等生产操作。")

        return "\n".join(lines)

    def _validate_top_evidence_ids(
        self, evidence: list[EvidenceItem], top: Hypothesis
    ) -> None:
        evidence_ids = {item.id for item in evidence}
        referenced_ids = [
            *top.supporting_evidence_ids,
            *top.contradicting_evidence_ids,
        ]

        for evidence_id in referenced_ids:
            if evidence_id not in evidence_ids:
                raise ValueError(f"missing evidence id: {evidence_id}")
