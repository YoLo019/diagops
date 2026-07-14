from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine

from backend.db.models import (
    InvestigationRecord,
    InvestigationStatus,
    InvestigationSummary,
)
from backend.db.repositories import _validate_multi_agent_result
from backend.db.schema import (
    agent_executions,
    agent_findings,
    context_facts,
    coordination_reviews,
    diagnosis_plans,
    diagnosis_tasks,
    events,
    evidence_items,
    hypotheses,
    investigations,
    llm_analyses,
    memory_items,
    provider_results,
    react_traces,
    recommended_actions,
    reports,
    specialist_results,
    tool_calls,
    verification_suggestions,
)
from backend.db.serialization import record_to_rows, rows_to_record
from backend.domain.actions import ActionStatus, VerificationStatus
from backend.domain.agent_context import ContextFact
from backend.domain.agent_findings import AgentFinding, CoordinationReview
from backend.domain.agent_plan import AgentExecution, DiagnosisPlan, DiagnosisTask
from backend.domain.human_transitions import (
    validate_action_transition,
    validate_verification_transition,
)
from backend.domain.hypotheses import CauseType
from backend.domain.memory import MemoryItem
from backend.domain.multi_agent import AgentExecutionLayer
from backend.domain.react_trace import ReActTrace
from backend.domain.tool_calls import ToolCallRecord
from backend.safety.redaction import redact_text


class SQLiteInvestigationRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save(self, record: InvestigationRecord) -> InvestigationRecord:
        rows = record_to_rows(record)
        with self.engine.begin() as connection:
            exists = connection.execute(
                select(investigations.c.id).where(investigations.c.id == record.id)
            ).scalar_one_or_none()
            if exists is None:
                connection.execute(insert(investigations).values(rows["investigation"]))
            else:
                connection.execute(
                    update(investigations)
                    .where(investigations.c.id == record.id)
                    .values(
                        **{
                            key: value
                            for key, value in rows["investigation"].items()
                            if key != "id"
                        }
                    )
                )
            connection.execute(
                delete(events).where(events.c.investigation_id == record.id)
            )
            connection.execute(
                insert(events).values(investigation_id=record.id, payload=rows["events"][0])
            )
            self._replace_children(
                connection, evidence_items, record.id, rows["evidence_items"]
            )
            self._replace_children(connection, hypotheses, record.id, rows["hypotheses"])
            self._replace_children(
                connection,
                recommended_actions,
                record.id,
                rows["recommended_actions"],
            )
            self._replace_children(
                connection,
                verification_suggestions,
                record.id,
                rows["verification_suggestions"],
            )
            self._replace_children(
                connection, provider_results, record.id, rows["provider_results"]
            )
            self._replace_children(
                connection, specialist_results, record.id, rows["specialist_results"]
            )
            connection.execute(
                delete(reports).where(reports.c.investigation_id == record.id)
            )
            if rows["report"] is not None:
                connection.execute(
                    insert(reports).values(
                        investigation_id=record.id,
                        payload=rows["report"],
                    )
                )
        return record

    def get(self, investigation_id: str) -> InvestigationRecord:
        with self.engine.connect() as connection:
            return self._get_with_connection(connection, investigation_id)

    def list(self) -> list[InvestigationRecord]:
        with self.engine.connect() as connection:
            investigation_ids = [
                row.id
                for row in connection.execute(
                    select(investigations.c.id).order_by(
                        investigations.c.created_at.desc()
                    )
                )
            ]
        return [self.get(investigation_id) for investigation_id in investigation_ids]

    def list_summaries(self) -> list[InvestigationSummary]:
        """直接查询列表所需列，避免加载可能损坏或体积较大的历史详情。"""
        top_hypothesis = (
            select(hypotheses.c.payload)
            .where(hypotheses.c.investigation_id == investigations.c.id)
            .order_by(hypotheses.c.position)
            .limit(1)
            .scalar_subquery()
        )
        action_count = (
            select(func.count())
            .select_from(recommended_actions)
            .where(recommended_actions.c.investigation_id == investigations.c.id)
            .scalar_subquery()
        )
        verification_count = (
            select(func.count())
            .select_from(verification_suggestions)
            .where(verification_suggestions.c.investigation_id == investigations.c.id)
            .scalar_subquery()
        )
        statement = select(
            investigations.c.id,
            investigations.c.status,
            investigations.c.event,
            investigations.c.failure_reason,
            top_hypothesis.label("top_hypothesis"),
            action_count.label("action_count"),
            verification_count.label("verification_count"),
        ).order_by(investigations.c.created_at.desc())
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()

        summaries = []
        for row in rows:
            event = row["event"]
            top = row["top_hypothesis"]
            summaries.append(
                InvestigationSummary(
                    id=row["id"],
                    status=row["status"],
                    service=redact_text(event["service"]),
                    title=redact_text(event["title"]),
                    top_cause_type=top["cause_type"] if top else "unknown",
                    confidence=top["confidence"] if top else 0.0,
                    action_count=row["action_count"],
                    verification_count=row["verification_count"],
                    failure_reason=(
                        redact_text(row["failure_reason"])
                        if row["failure_reason"]
                        else None
                    ),
                )
            )
        return summaries

    def update_status(
        self,
        investigation_id: str,
        status: InvestigationStatus,
        *,
        failure_reason: str | None = None,
    ) -> InvestigationRecord:
        record = self.get(investigation_id)
        record.status = status
        record.failure_reason = failure_reason
        record.updated_at = datetime.now(UTC)
        if status == InvestigationStatus.COMPLETED:
            record.completed_at = record.updated_at
        self.save(record)
        return record

    def update_action_status(
        self,
        investigation_id: str,
        action_id: str,
        *,
        status: ActionStatus | str,
        note: str | None = None,
    ):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                record = self._get_with_connection(connection, investigation_id)
                updated = validate_action_transition(record, action_id, status, note)
                result = connection.execute(
                    update(recommended_actions)
                    .where(
                        recommended_actions.c.investigation_id == investigation_id,
                        recommended_actions.c.id == action_id,
                    )
                    .values(payload=updated.model_dump(mode="json"))
                )
                if result.rowcount != 1:
                    raise ValueError(f"Unknown action: {action_id}")
                connection.execute(
                    update(investigations)
                    .where(investigations.c.id == investigation_id)
                    .values(updated_at=datetime.now(UTC).isoformat())
                )
                connection.commit()
                return updated
            except Exception:
                connection.rollback()
                raise

    def update_verification_status(
        self,
        investigation_id: str,
        verification_id: str,
        *,
        status: VerificationStatus | str,
        result_note: str | None = None,
        result_evidence_ids: list[str] | None = None,
        related_action_ids: list[str] | None = None,
        related_cause_types: list[CauseType | str] | None = None,
    ):
        with self.engine.connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                record = self._get_with_connection(connection, investigation_id)
                updated = validate_verification_transition(
                    record,
                    verification_id,
                    status,
                    result_note,
                    result_evidence_ids or [],
                    related_action_ids or [],
                    related_cause_types or [],
                )
                result = connection.execute(
                    update(verification_suggestions)
                    .where(
                        verification_suggestions.c.investigation_id == investigation_id,
                        verification_suggestions.c.id == verification_id,
                    )
                    .values(payload=updated.model_dump(mode="json"))
                )
                if result.rowcount != 1:
                    raise ValueError(
                        f"Unknown verification suggestion: {verification_id}"
                    )
                connection.execute(
                    update(investigations)
                    .where(investigations.c.id == investigation_id)
                    .values(updated_at=datetime.now(UTC).isoformat())
                )
                connection.commit()
                return updated
            except Exception:
                connection.rollback()
                raise

    def save_plan(self, plan: DiagnosisPlan) -> DiagnosisPlan:
        payload = plan.model_dump(mode="json")
        row = {
            "id": plan.id,
            "investigation_id": plan.investigation_id,
            "created_at": payload["created_at"],
            "payload": payload,
        }
        with self.engine.begin() as connection:
            connection.execute(
                delete(diagnosis_plans).where(
                    diagnosis_plans.c.investigation_id == plan.investigation_id
                )
            )
            connection.execute(insert(diagnosis_plans).values(row))
            self._replace_tasks(connection, plan.investigation_id, plan.tasks)
        return plan

    def get_plan(self, investigation_id: str) -> DiagnosisPlan | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(diagnosis_plans).where(
                    diagnosis_plans.c.investigation_id == investigation_id
                )
            ).mappings().one_or_none()
        if row is None:
            return None
        return DiagnosisPlan(**row["payload"]).model_copy(
            update={"tasks": self.list_tasks(investigation_id)}
        )

    def save_tasks(
        self,
        investigation_id: str,
        tasks: Sequence[DiagnosisTask],
    ) -> list[DiagnosisTask]:
        with self.engine.begin() as connection:
            self._replace_tasks(connection, investigation_id, tasks)
        return list(tasks)

    def list_tasks(self, investigation_id: str) -> list[DiagnosisTask]:
        with self.engine.connect() as connection:
            rows = self._fetch_children(connection, diagnosis_tasks, investigation_id)
        return [DiagnosisTask(**row["payload"]) for row in rows]

    def save_executions(
        self,
        investigation_id: str,
        executions: Sequence[AgentExecution],
    ) -> list[AgentExecution]:
        rows = [
            self._agent_payload_row(investigation_id, execution, task_id=execution.task_id)
            for execution in executions
        ]
        with self.engine.begin() as connection:
            self._upsert_payload_rows(connection, agent_executions, rows)
        return list(executions)

    def list_executions(self, investigation_id: str) -> list[AgentExecution]:
        with self.engine.connect() as connection:
            rows = self._fetch_agent_rows(connection, agent_executions, investigation_id)
        return [AgentExecution(**row["payload"]) for row in rows]

    def save_context_facts(
        self,
        investigation_id: str,
        facts: Sequence[ContextFact],
    ) -> list[ContextFact]:
        rows = [
            self._agent_payload_row(investigation_id, fact, status=None, task_id=None)
            for fact in facts
        ]
        with self.engine.begin() as connection:
            self._upsert_payload_rows(connection, context_facts, rows)
        return list(facts)

    def list_context_facts(self, investigation_id: str) -> list[ContextFact]:
        with self.engine.connect() as connection:
            rows = self._fetch_agent_rows(connection, context_facts, investigation_id)
        return [ContextFact(**row["payload"]) for row in rows]

    def save_tool_calls(
        self,
        investigation_id: str,
        calls: Sequence[ToolCallRecord],
    ) -> list[ToolCallRecord]:
        rows = [
            self._agent_payload_row(investigation_id, call, task_id=call.task_id)
            for call in calls
        ]
        with self.engine.begin() as connection:
            self._upsert_payload_rows(connection, tool_calls, rows)
        return list(calls)

    def list_tool_calls(self, investigation_id: str) -> list[ToolCallRecord]:
        with self.engine.connect() as connection:
            rows = self._fetch_agent_rows(connection, tool_calls, investigation_id)
        return [ToolCallRecord(**row["payload"]) for row in rows]

    def save_memory_items(self, items: Sequence[MemoryItem]) -> list[MemoryItem]:
        rows = []
        for item in items:
            payload = item.model_dump(mode="json")
            rows.append(
                {
                    "id": item.id,
                    "service": item.service,
                    "environment": item.environment,
                    "source_investigation_id": item.source_investigation_id,
                    "created_at": payload["created_at"],
                    "payload": payload,
                }
            )
        with self.engine.begin() as connection:
            self._upsert_payload_rows(connection, memory_items, rows)
        return list(items)

    def list_memory(
        self,
        service: str,
        environment: str,
        *,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        statement = (
            select(memory_items)
            .where(memory_items.c.service == service)
            .where(memory_items.c.environment == environment)
            .order_by(memory_items.c.created_at.desc(), memory_items.c.id.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(statement).mappings().all()
        return [MemoryItem(**row["payload"]) for row in rows]

    def save_agent_findings(
        self,
        investigation_id: str,
        findings: Sequence[AgentFinding],
    ) -> list[AgentFinding]:
        rows = []
        for finding in findings:
            row = self._agent_payload_row(investigation_id, finding, status=None)
            row["agent_name"] = row["payload"]["agent_name"]
            rows.append(row)
        with self.engine.begin() as connection:
            self._upsert_payload_rows(connection, agent_findings, rows)
        return list(findings)

    def list_agent_findings(self, investigation_id: str) -> list[AgentFinding]:
        with self.engine.connect() as connection:
            rows = self._fetch_agent_rows(connection, agent_findings, investigation_id)
        return [AgentFinding(**row["payload"]) for row in rows]

    def save_coordination_review(
        self,
        review: CoordinationReview,
    ) -> CoordinationReview:
        payload = review.model_dump(mode="json")
        row = {
            "id": review.id,
            "investigation_id": review.investigation_id,
            "created_at": payload["created_at"],
            "payload": payload,
        }
        with self.engine.begin() as connection:
            connection.execute(
                delete(coordination_reviews).where(
                    coordination_reviews.c.investigation_id == review.investigation_id
                )
            )
            connection.execute(insert(coordination_reviews).values(row))
        return review

    def save_multi_agent_result(
        self,
        investigation_id: str,
        findings: Sequence[AgentFinding],
        executions: Sequence[AgentExecution],
        review: CoordinationReview | None,
    ) -> None:
        """在单个事务中替换一次 SDK Agent 运行的完整持久化投影。"""
        validated_findings, validated_executions, validated_review = (
            _validate_multi_agent_result(
                investigation_id, list(findings), list(executions), review
            )
        )
        finding_rows = []
        for finding in validated_findings:
            row = self._agent_payload_row(investigation_id, finding, status=None)
            row["agent_name"] = row["payload"]["agent_name"]
            finding_rows.append(row)
        execution_rows = [
            self._agent_payload_row(
                investigation_id, execution, task_id=execution.task_id
            )
            for execution in validated_executions
        ]
        review_row = None
        if validated_review is not None:
            payload = validated_review.model_dump(mode="json")
            review_row = {
                "id": validated_review.id,
                "investigation_id": investigation_id,
                "created_at": payload["created_at"],
                "payload": payload,
            }

        with self.engine.begin() as connection:
            self._delete_sdk_agent_rows(
                connection, agent_findings, investigation_id
            )
            self._delete_sdk_agent_rows(
                connection, agent_executions, investigation_id
            )
            self._upsert_payload_rows(connection, agent_findings, finding_rows)
            self._upsert_payload_rows(connection, agent_executions, execution_rows)

            existing_review = connection.execute(
                select(coordination_reviews).where(
                    coordination_reviews.c.investigation_id == investigation_id
                )
            ).mappings().one_or_none()
            if review_row is not None:
                connection.execute(
                    delete(coordination_reviews).where(
                        coordination_reviews.c.investigation_id == investigation_id
                    )
                )
                self._insert_multi_agent_review(connection, review_row)
            elif (
                existing_review is not None
                and existing_review["payload"].get("execution_layer")
                == AgentExecutionLayer.OPENAI_AGENTS_SDK.value
            ):
                connection.execute(
                    delete(coordination_reviews).where(
                        coordination_reviews.c.investigation_id == investigation_id
                    )
                )

    def get_coordination_review(
        self,
        investigation_id: str,
    ) -> CoordinationReview | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(coordination_reviews).where(
                    coordination_reviews.c.investigation_id == investigation_id
                )
            ).mappings().one_or_none()
        return None if row is None else CoordinationReview(**row["payload"])

    def get_react_trace(self, investigation_id: str) -> ReActTrace | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(react_traces).where(
                    react_traces.c.investigation_id == investigation_id
                )
            ).mappings().one_or_none()
        return None if row is None else ReActTrace.model_validate(row["payload"])

    def _replace_children(
        self,
        connection: Any,
        table: Any,
        investigation_id: str,
        rows: list[dict[str, Any]],
    ) -> None:
        connection.execute(
            delete(table).where(table.c.investigation_id == investigation_id)
        )
        if rows:
            connection.execute(insert(table), rows)

    def _delete_sdk_agent_rows(
        self,
        connection: Any,
        table: Any,
        investigation_id: str,
    ) -> None:
        rows = connection.execute(
            select(table.c.id, table.c.payload).where(
                table.c.investigation_id == investigation_id
            )
        ).mappings()
        sdk_ids = [
            row["id"]
            for row in rows
            if row["payload"].get("execution_layer")
            == AgentExecutionLayer.OPENAI_AGENTS_SDK.value
        ]
        if sdk_ids:
            connection.execute(delete(table).where(table.c.id.in_(sdk_ids)))

    def _insert_multi_agent_review(
        self,
        connection: Any,
        row: dict[str, Any],
    ) -> None:
        connection.execute(insert(coordination_reviews).values(row))

    def _get_with_connection(
        self, connection: Any, investigation_id: str
    ) -> InvestigationRecord:
        investigation = connection.execute(
            select(investigations).where(investigations.c.id == investigation_id)
        ).mappings().one_or_none()
        if investigation is None:
            raise ValueError(f"Unknown investigation: {investigation_id}")
        return rows_to_record(
            {
                "investigation": dict(investigation),
                "evidence_items": self._fetch_children(
                    connection, evidence_items, investigation_id
                ),
                "hypotheses": self._fetch_children(
                    connection, hypotheses, investigation_id
                ),
                "recommended_actions": self._fetch_children(
                    connection, recommended_actions, investigation_id
                ),
                "verification_suggestions": self._fetch_children(
                    connection, verification_suggestions, investigation_id
                ),
                "provider_results": self._fetch_children(
                    connection, provider_results, investigation_id
                ),
                "specialist_results": self._fetch_children(
                    connection, specialist_results, investigation_id
                ),
                "report": self._fetch_report(connection, investigation_id),
                "llm_analysis": self._fetch_llm_analysis(
                    connection, investigation_id
                ),
            }
        )

    def _fetch_children(
        self,
        connection: Any,
        table: Any,
        investigation_id: str,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                select(table)
                .where(table.c.investigation_id == investigation_id)
                .order_by(table.c.position)
            )
            .mappings()
            .all()
        ]

    def _replace_tasks(
        self,
        connection: Any,
        investigation_id: str,
        tasks: Sequence[DiagnosisTask],
    ) -> None:
        rows = []
        for position, task in enumerate(tasks):
            payload = task.model_dump(mode="json")
            rows.append(
                {
                    "id": task.id,
                    "investigation_id": investigation_id,
                    "position": position,
                    "status": payload["status"],
                    "created_at": payload["created_at"],
                    "payload": payload,
                }
            )
        self._replace_children(connection, diagnosis_tasks, investigation_id, rows)

    def _agent_payload_row(
        self,
        investigation_id: str,
        model: Any,
        *,
        status: str | None = "status",
        task_id: str | None = None,
    ) -> dict[str, Any]:
        payload = model.model_dump(mode="json")
        row = {
            "id": model.id,
            "investigation_id": investigation_id,
            "created_at": self._payload_created_at(payload),
            "payload": payload,
        }
        if status is not None:
            row["status"] = payload[status]
        if task_id is not None:
            row["task_id"] = task_id
        return row

    def _upsert_payload_rows(
        self,
        connection: Any,
        table: Any,
        rows: Sequence[dict[str, Any]],
    ) -> None:
        if not rows:
            return
        connection.execute(delete(table).where(table.c.id.in_([row["id"] for row in rows])))
        connection.execute(insert(table), list(rows))

    def _fetch_agent_rows(
        self,
        connection: Any,
        table: Any,
        investigation_id: str,
    ) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                select(table)
                .where(table.c.investigation_id == investigation_id)
                .order_by(table.c.created_at, table.c.id)
            )
            .mappings()
            .all()
        ]

    def _payload_created_at(self, payload: dict[str, Any]) -> str:
        return (
            payload.get("created_at")
            or payload.get("started_at")
            or payload.get("completed_at")
            or datetime.now(UTC).isoformat()
        )

    def _fetch_report(
        self,
        connection: Any,
        investigation_id: str,
    ) -> dict[str, Any] | None:
        report = connection.execute(
            select(reports).where(reports.c.investigation_id == investigation_id)
        ).mappings().one_or_none()
        return dict(report) if report is not None else None

    def _fetch_llm_analysis(
        self,
        connection: Any,
        investigation_id: str,
    ) -> dict[str, Any] | None:
        analysis = connection.execute(
            select(llm_analyses).where(llm_analyses.c.investigation_id == investigation_id)
        ).mappings().one_or_none()
        return dict(analysis) if analysis is not None else None
