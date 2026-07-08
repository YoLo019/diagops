from __future__ import annotations

from typing import Protocol

from backend.domain.agent_context import ContextFact


class _InvestigationRepository(Protocol):
    def get(self, investigation_id: str): ...


class SharedContextStore:
    def __init__(self, repository: _InvestigationRepository) -> None:
        self.repository = repository
        self._facts: dict[str, list[ContextFact]] = {}

    def add_fact(self, investigation_id: str, fact: ContextFact) -> ContextFact:
        record = self.repository.get(investigation_id)
        evidence_ids = {evidence.id for evidence in record.evidence}
        unknown_ids = [
            evidence_id
            for evidence_id in fact.evidence_ids
            if evidence_id not in evidence_ids
        ]
        if unknown_ids:
            raise ValueError(f"unknown evidence id: {', '.join(unknown_ids)}")

        methods = self._repository_context_methods()
        if methods is not None:
            save_context_facts, _list_context_facts = methods
            save_context_facts(investigation_id, [fact])
        else:
            self._upsert_memory_fact(investigation_id, fact)
        return fact

    def list_facts(self, investigation_id: str) -> list[ContextFact]:
        methods = self._repository_context_methods()
        if methods is not None:
            _save_context_facts, list_context_facts = methods
            return list(list_context_facts(investigation_id))
        return list(self._facts.get(investigation_id, []))

    def _repository_context_methods(self):
        save_context_facts = getattr(self.repository, "save_context_facts", None)
        list_context_facts = getattr(self.repository, "list_context_facts", None)
        if callable(save_context_facts) and callable(list_context_facts):
            return save_context_facts, list_context_facts
        return None

    def _upsert_memory_fact(self, investigation_id: str, fact: ContextFact) -> None:
        facts = self._facts.setdefault(investigation_id, [])
        for index, existing in enumerate(facts):
            if existing.id == fact.id:
                facts[index] = fact
                return
        facts.append(fact)


__all__ = ["SharedContextStore"]
