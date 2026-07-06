from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.schema import (
    events,
    evidence_items,
    hypotheses,
    investigations,
    llm_analyses,
    provider_results,
    recommended_actions,
    reports,
    specialist_results,
    verification_suggestions,
)
from backend.db.serialization import record_to_rows, rows_to_record
from backend.domain.actions import ActionStatus, VerificationStatus


class SQLiteInvestigationRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def save(self, record: InvestigationRecord) -> InvestigationRecord:
        rows = record_to_rows(record)
        with self.engine.begin() as connection:
            self._delete_record_rows(connection, record.id)
            connection.execute(
                insert(investigations).values(rows["investigation"])
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
            investigation = connection.execute(
                select(investigations).where(investigations.c.id == investigation_id)
            ).mappings().one_or_none()
            if investigation is None:
                raise ValueError(f"Unknown investigation: {investigation_id}")
            rows = {
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
            }
        return rows_to_record(rows)

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
        record = self.get(investigation_id)
        parsed_status = ActionStatus(status)
        for action in record.actions:
            if action.id == action_id:
                action.status = parsed_status
                action.note = note
                record.updated_at = datetime.now(UTC)
                self.save(record)
                return action
        raise ValueError(f"Unknown action: {action_id}")

    def update_verification_status(
        self,
        investigation_id: str,
        verification_id: str,
        *,
        status: VerificationStatus | str,
        result_note: str | None = None,
    ):
        record = self.get(investigation_id)
        parsed_status = VerificationStatus(status)
        for suggestion in record.verification_suggestions:
            if suggestion.id == verification_id:
                suggestion.status = parsed_status
                suggestion.result_note = result_note
                record.updated_at = datetime.now(UTC)
                self.save(record)
                return suggestion
        raise ValueError(f"Unknown verification suggestion: {verification_id}")

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

    def _delete_record_rows(self, connection: Any, investigation_id: str) -> None:
        for table in (
            events,
            evidence_items,
            hypotheses,
            recommended_actions,
            verification_suggestions,
            reports,
            provider_results,
            specialist_results,
            llm_analyses,
        ):
            connection.execute(
                delete(table).where(table.c.investigation_id == investigation_id)
            )
        connection.execute(
            delete(investigations).where(investigations.c.id == investigation_id)
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

    def _fetch_report(
        self,
        connection: Any,
        investigation_id: str,
    ) -> dict[str, Any] | None:
        report = connection.execute(
            select(reports).where(reports.c.investigation_id == investigation_id)
        ).mappings().one_or_none()
        return dict(report) if report is not None else None
