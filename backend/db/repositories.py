from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.actions import ActionStatus, VerificationStatus


class InMemoryInvestigationRepository:
    def __init__(self) -> None:
        self._records: dict[str, InvestigationRecord] = {}

    def save(self, record: InvestigationRecord) -> InvestigationRecord:
        self._records[record.id] = record
        return record

    def get(self, investigation_id: str) -> InvestigationRecord:
        try:
            return self._records[investigation_id]
        except KeyError as exc:
            raise ValueError(f"Unknown investigation: {investigation_id}") from exc

    def list(self) -> list[InvestigationRecord]:
        return sorted(
            self._records.values(), key=lambda record: record.created_at, reverse=True
        )

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
        self._records[record.id] = record
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
                self._records[record.id] = record
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
                self._records[record.id] = record
                return suggestion
        raise ValueError(f"Unknown verification suggestion: {verification_id}")
