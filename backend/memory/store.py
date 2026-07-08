from collections.abc import Sequence
from typing import Any

from backend.db.models import InvestigationRecord
from backend.domain.memory import MemoryItem, MemoryType


class MemoryStore:
    def __init__(self, repository: Any) -> None:
        self.repository = repository

    def save(self, item: MemoryItem) -> MemoryItem:
        return self.save_many([item])[0]

    def save_many(self, items: Sequence[MemoryItem]) -> list[MemoryItem]:
        return self.repository.save_memory_items(items)

    def list(
        self,
        service: str,
        environment: str,
        limit: int | None = None,
    ) -> list[MemoryItem]:
        return self.repository.list_memory(service, environment, limit=limit)

    def record_feedback(
        self,
        record: InvestigationRecord,
        root_cause_correct: bool | None = None,
        action_useful: bool | None = None,
        verification_result: str | None = None,
        note: str | None = None,
    ) -> MemoryItem:
        fields = {
            "root_cause_correct": root_cause_correct,
            "action_useful": action_useful,
            "verification_result": verification_result,
            "note": note,
        }
        summary = "Human feedback: " + "; ".join(
            f"{key}={value}" for key, value in fields.items() if value is not None
        )
        tags = [
            f"{key}:{str(value).lower()}"
            for key, value in fields.items()
            if key != "note" and value is not None
        ]
        return self.save(
            MemoryItem(
                service=record.event.service,
                environment=record.event.environment,
                memory_type=MemoryType.HUMAN_FEEDBACK,
                summary=summary,
                source_investigation_id=record.id,
                tags=tags,
            )
        )
