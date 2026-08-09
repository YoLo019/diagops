"""verified-memory lookup 的 guard 行为（spec 8.1）。"""

from datetime import UTC, datetime, timedelta

import pytest

from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.agent_findings import CoordinationReview, RootCauseCandidate
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceSourceClass
from backend.domain.memory import MemoryItem, MemoryType, MemoryVerificationStatus
from backend.domain.tool_queries import MemoryQuery
from backend.tools.provider_tools import VerifiedMemoryLookup

_STARTED = datetime(2026, 7, 20, 9, 30, tzinfo=UTC)


def _event() -> IncidentEvent:
    return IncidentEvent(
        source=IncidentSource.WEBHOOK,
        service="checkout-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="checkout 5xx",
        description="synthetic",
        started_at=_STARTED,
    )


@pytest.fixture
def repository():
    return InMemoryInvestigationRepository()


def _seed_source(repository, *, investigation_id="inv-source", candidate_id="candidate-1"):
    record = repository.save(
        InvestigationRecord(id=investigation_id, event=_event())
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=record.id,
            candidates=[
                RootCauseCandidate(
                    id=candidate_id,
                    affected_entity="payment-service",
                    failure_mechanism="timeout",
                    summary="payment timeout",
                    rank=1,
                    confidence=0.9,
                )
            ],
        )
    )
    return record


def _verified_memory(**overrides):
    values = {
        "service": "checkout-service",
        "environment": "prod",
        "memory_type": MemoryType.INVESTIGATION_SUMMARY,
        "summary": "historical payment timeout",
        "source_investigation_id": "inv-source",
        "verification_status": MemoryVerificationStatus.VERIFIED,
        "verified_at": _STARTED - timedelta(days=3),
        "verified_by": "operator",
        "root_candidate_id": "candidate-1",
        "created_at": _STARTED - timedelta(days=3),
    }
    values.update(overrides)
    return MemoryItem(**values)


def test_lookup_emits_only_verified_records_with_provenance(repository):
    _seed_source(repository)
    repository.save_memory_items([_verified_memory()])
    lookup = VerifiedMemoryLookup(repository)

    evidence = lookup.lookup(_event())

    assert len(evidence) == 1
    item = evidence[0]
    assert item.provider == EvidenceProvider.VERIFIED_INCIDENT
    assert item.kind == EvidenceKind.VERIFIED_INCIDENT
    assert item.provenance is not None
    assert item.provenance.source_class == EvidenceSourceClass.RECORDED_LOCAL
    assert item.payload["source_investigation_id"] == "inv-source"
    assert item.payload["root_candidate_id"] == "candidate-1"
    assert "evidence_ids" not in item.payload


def test_lookup_rejects_unverified_and_future_records(repository):
    _seed_source(repository)
    repository.save_memory_items(
        [
            _verified_memory(verification_status=MemoryVerificationStatus.UNVERIFIED),
            _verified_memory(verified_at=_STARTED + timedelta(minutes=1)),
            _verified_memory(created_at=_STARTED + timedelta(minutes=1)),
        ]
    )

    assert VerifiedMemoryLookup(repository).lookup(_event()) == []


def test_lookup_rejects_missing_source_or_candidate(repository):
    _seed_source(repository)
    repository.save_memory_items(
        [
            _verified_memory(source_investigation_id="inv-missing"),
            _verified_memory(root_candidate_id="candidate-missing"),
        ]
    )

    assert VerifiedMemoryLookup(repository).lookup(_event()) == []


def test_lookup_rejects_current_and_ancestor_sources(repository):
    source = _seed_source(repository)
    parent = repository.save(
        InvestigationRecord(
            id="inv-parent",
            event=_event(),
            source_investigation_id=source.id,
        )
    )
    repository.save_coordination_review(
        CoordinationReview(
            investigation_id=parent.id,
            candidates=[
                RootCauseCandidate(
                    id="candidate-1",
                    affected_entity="payment-service",
                    failure_mechanism="timeout",
                    summary="payment timeout",
                    rank=1,
                    confidence=0.9,
                )
            ],
        )
    )
    current = repository.save(
        InvestigationRecord(
            id="inv-current",
            event=_event(),
            source_investigation_id=parent.id,
        )
    )
    repository.save_memory_items(
        [
            _verified_memory(source_investigation_id=source.id),
            _verified_memory(source_investigation_id=parent.id),
            _verified_memory(source_investigation_id=current.id),
        ]
    )
    lookup = VerifiedMemoryLookup(
        repository, current_investigation_id=lambda: current.id
    )

    assert lookup.lookup(_event()) == []


def test_lookup_filters_by_candidate_fields_and_limit(repository):
    _seed_source(repository)
    repository.save_memory_items([_verified_memory()])
    lookup = VerifiedMemoryLookup(repository)

    assert lookup.lookup(_event(), MemoryQuery(affected_entity="payment-service"))
    assert lookup.lookup(_event(), MemoryQuery(affected_entity="other-service")) == []
    assert lookup.lookup(_event(), MemoryQuery(failure_mechanism="cpu saturation")) == []
    assert lookup.lookup(_event(), MemoryQuery(limit=1))
