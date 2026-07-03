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

        top = hypotheses[0]
        timeline = [
            {"time": item.timestamp.isoformat(), "event": item.summary}
            for item in sorted(evidence, key=lambda item: item.timestamp)
        ]

        markdown = self._render_markdown(event, evidence, hypotheses)

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
        for evidence_id in top.supporting_evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item:
                lines.append(f"- {item.summary}")

        lines.extend(["", "## 建议动作", ""])
        for action in top.next_actions:
            lines.append(f"- {action}")

        lines.extend(["", "## 事实与推断", ""])
        lines.append("- 事实来自事件输入和 Provider 返回的结构化证据。")
        lines.append("- 根因是假设，需要工程师结合现场进一步确认。")
        lines.append("- 系统不会自动执行回滚、重启、扩容等生产操作。")

        return "\n".join(lines)
