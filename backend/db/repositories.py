from backend.db.models import InvestigationRecord


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
