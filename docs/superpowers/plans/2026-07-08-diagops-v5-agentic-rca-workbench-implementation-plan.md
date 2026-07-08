# DiagOps V5 Agentic RCA Workbench Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add V5 agentic RCA findings, coordinator review, and a list-based RCA workbench that can later become an OpenDerisk-style graph view.

**Architecture:** Reuse the V4 investigation flow and repository style. Add deterministic LogAgent, MetricAgent, and DeploymentAgent finding builders, then rank candidates in a small coordinator service and expose the result through read-only APIs and frontend panels. Keep LLM enhancement disabled by default; do not add a real LLM path in this first implementation.

**Tech Stack:** Python, FastAPI, Pydantic, SQLAlchemy Core, SQLite, pytest, ruff, React, TypeScript, Vite.

---

## Spec

`docs/superpowers/specs/2026-07-08-diagops-v5-agentic-rca-workbench-design.md`

## File Structure

- Create `backend/domain/agent_findings.py`
  - Owns V5 Pydantic models: `AgentFinding`, `CoordinationReview`, `RootCauseCandidate`, and graph seed response models.
- Create `backend/diagnosis/finding_builders.py`
  - Builds deterministic findings from existing evidence for LogAgent, MetricAgent, and DeploymentAgent.
- Create `backend/diagnosis/coordination_review.py`
  - Validates findings against evidence and produces ranked root-cause candidates.
- Modify `backend/db/schema.py`
  - Adds `agent_findings` and `coordination_reviews` JSON payload tables.
- Modify `backend/db/repositories.py`
  - Adds in-memory save/list methods for V5 records.
- Modify `backend/db/sqlite_repository.py`
  - Adds SQLite save/list methods for V5 records.
- Modify `backend/diagnosis/orchestrator.py`
  - Runs V5 finding builders and coordinator after evidence/hypotheses exist.
- Modify `backend/api/agent_views.py`
  - Adds V5 read-only endpoints.
- Modify `frontend/src/api.ts`
  - Adds V5 types and API client methods.
- Modify `frontend/src/App.tsx`
  - Adds list-based V5 RCA workbench panels.
- Modify `frontend/src/styles.css`
  - Adds compact workbench styling only if current classes are insufficient.
- Modify `tests/**`
  - Adds focused V5 tests; updates smoke/golden checks.
- Modify `README.md`
  - Adds V5 usage notes after behavior exists.

No new runtime dependency is planned.

---

### Task 1: V5 Domain Models

**Files:**
- Create: `backend/domain/agent_findings.py`
- Test: `tests/domain/test_agent_findings.py`

- [ ] **Step 1: Write failing domain tests**

Create `tests/domain/test_agent_findings.py`:

```python
import pytest
from pydantic import ValidationError

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.hypotheses import CauseType


def test_agent_finding_requires_evidence_unless_gap():
    with pytest.raises(ValidationError, match="evidence_ids"):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=AgentName.LOG,
            finding_type=AgentFindingType.SIGNAL,
            summary="Log error spike",
            confidence=0.8,
        )

    gap = AgentFinding(
        investigation_id="inv-1",
        agent_name=AgentName.LOG,
        finding_type=AgentFindingType.GAP,
        summary="Log evidence is missing",
        confidence=0.2,
    )

    assert gap.evidence_ids == []


def test_agent_finding_confidence_is_bounded():
    with pytest.raises(ValidationError):
        AgentFinding(
            investigation_id="inv-1",
            agent_name=AgentName.METRIC,
            finding_type=AgentFindingType.SIGNAL,
            summary="Bad confidence",
            confidence=1.1,
            evidence_ids=["ev-1"],
        )


def test_coordination_review_orders_candidates_by_rank():
    lower = RootCauseCandidate(
        cause_type=CauseType.TRAFFIC_SPIKE,
        summary="Traffic may be elevated",
        rank=2,
        confidence=0.4,
        supporting_finding_ids=["finding-2"],
        supporting_evidence_ids=["ev-2"],
        rationale="Metric signal exists.",
        uncertainty="Deployment evidence is stronger.",
    )
    higher = RootCauseCandidate(
        cause_type=CauseType.DEPLOYMENT_REGRESSION,
        summary="Deployment likely caused the incident",
        rank=1,
        confidence=0.8,
        supporting_finding_ids=["finding-1"],
        supporting_evidence_ids=["ev-1"],
        rationale="Deployment and log evidence align.",
        uncertainty="Metrics should be checked.",
    )

    review = CoordinationReview(investigation_id="inv-1", candidates=[lower, higher])

    assert [candidate.rank for candidate in review.candidates] == [1, 2]
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/domain/test_agent_findings.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'backend.domain.agent_findings'`.

- [ ] **Step 3: Add minimal domain models**

Create `backend/domain/agent_findings.py`:

```python
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from backend.domain.hypotheses import CauseType


class AgentName(StrEnum):
    LOG = "LogAgent"
    METRIC = "MetricAgent"
    DEPLOYMENT = "DeploymentAgent"


class AgentFindingType(StrEnum):
    SIGNAL = "signal"
    ROOT_CAUSE = "root_cause"
    CONTRADICTION = "contradiction"
    GAP = "gap"


class AgentFindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AgentFinding(BaseModel):
    id: str = Field(default_factory=lambda: f"finding-{uuid4().hex}")
    investigation_id: str
    agent_name: AgentName
    finding_type: AgentFindingType
    summary: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(default_factory=list)
    related_cause_type: CauseType | None = None
    severity: AgentFindingSeverity = AgentFindingSeverity.MEDIUM
    rationale: str = ""
    gaps: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def require_evidence_unless_gap(self) -> "AgentFinding":
        if self.finding_type != AgentFindingType.GAP and not self.evidence_ids:
            raise ValueError("evidence_ids are required unless finding_type is gap")
        return self


class RootCauseCandidate(BaseModel):
    id: str = Field(default_factory=lambda: f"candidate-{uuid4().hex}")
    cause_type: CauseType
    summary: str = Field(min_length=1)
    rank: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    supporting_finding_ids: list[str] = Field(default_factory=list)
    contradicting_finding_ids: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""
    uncertainty: str = ""


class CoordinationReview(BaseModel):
    id: str = Field(default_factory=lambda: f"coordination-{uuid4().hex}")
    investigation_id: str
    candidates: list[RootCauseCandidate] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def sort_candidates(self) -> "CoordinationReview":
        self.candidates.sort(key=lambda candidate: candidate.rank)
        return self


class WorkbenchGraphNode(BaseModel):
    id: str
    label: str
    type: str


class WorkbenchGraphEdge(BaseModel):
    source: str
    target: str
    relation: str


class WorkbenchGraphSeed(BaseModel):
    nodes: list[WorkbenchGraphNode] = Field(default_factory=list)
    edges: list[WorkbenchGraphEdge] = Field(default_factory=list)
```

- [ ] **Step 4: Run domain tests**

Run:

```powershell
uv run pytest tests/domain/test_agent_findings.py -v
uv run ruff check backend/domain/agent_findings.py tests/domain/test_agent_findings.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/domain/agent_findings.py tests/domain/test_agent_findings.py
git commit -m "feat: add v5 agent finding models"
```

---

### Task 2: Persist V5 Findings And Reviews

**Files:**
- Modify: `backend/db/schema.py`
- Modify: `backend/db/repositories.py`
- Modify: `backend/db/sqlite_repository.py`
- Test: `tests/db/test_v5_agentic_rca_persistence.py`

- [ ] **Step 1: Write failing persistence tests**

Create `tests/db/test_v5_agentic_rca_persistence.py`:

```python
from sqlalchemy import create_engine

from backend.db.repositories import InMemoryInvestigationRepository
from backend.db.schema import metadata
from backend.db.sqlite_repository import SQLiteInvestigationRepository
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.hypotheses import CauseType


def make_finding() -> AgentFinding:
    return AgentFinding(
        id="finding-log-1",
        investigation_id="inv-1",
        agent_name=AgentName.LOG,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary="Log exceptions increased after deployment.",
        confidence=0.8,
        evidence_ids=["ev-log-1"],
        related_cause_type=CauseType.DEPLOYMENT_REGRESSION,
    )


def make_review() -> CoordinationReview:
    return CoordinationReview(
        id="coordination-1",
        investigation_id="inv-1",
        candidates=[
            RootCauseCandidate(
                id="candidate-1",
                cause_type=CauseType.DEPLOYMENT_REGRESSION,
                summary="Deployment regression is most likely.",
                rank=1,
                confidence=0.8,
                supporting_finding_ids=["finding-log-1"],
                supporting_evidence_ids=["ev-log-1"],
                rationale="Log evidence supports deployment regression.",
                uncertainty="Metric confirmation is useful.",
            )
        ],
    )


def test_in_memory_repository_saves_v5_findings_and_review():
    repo = InMemoryInvestigationRepository()
    finding = make_finding()
    review = make_review()

    assert repo.save_agent_findings("inv-1", [finding]) == [finding]
    assert repo.save_coordination_review(review) == review

    assert repo.list_agent_findings("inv-1") == [finding]
    assert repo.get_coordination_review("inv-1") == review


def test_sqlite_repository_saves_v5_findings_and_review():
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    repo = SQLiteInvestigationRepository(engine)
    finding = make_finding()
    review = make_review()

    assert repo.save_agent_findings("inv-1", [finding]) == [finding]
    assert repo.save_coordination_review(review) == review

    assert repo.list_agent_findings("inv-1") == [finding]
    assert repo.get_coordination_review("inv-1") == review
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/db/test_v5_agentic_rca_persistence.py -v
```

Expected: FAIL because repository methods and tables do not exist.

- [ ] **Step 3: Add schema tables**

Modify `backend/db/schema.py` after `context_facts`:

```python
agent_findings = Table(
    "agent_findings",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("agent_name", String, nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)

coordination_reviews = Table(
    "coordination_reviews",
    metadata,
    Column("id", String, primary_key=True),
    Column("investigation_id", ForeignKey("investigations.id"), nullable=False, index=True),
    Column("created_at", String, nullable=False, index=True),
    Column("payload", JSON, nullable=False),
)
```

Add indexes near the existing memory index:

```python
Index(
    "ix_agent_findings_investigation_agent_created_at",
    agent_findings.c.investigation_id,
    agent_findings.c.agent_name,
    agent_findings.c.created_at,
)
```

- [ ] **Step 4: Add in-memory repository methods**

Modify imports in `backend/db/repositories.py`:

```python
from backend.domain.agent_findings import AgentFinding, CoordinationReview
```

Add fields in `__init__`:

```python
self._agent_findings: dict[str, dict[str, AgentFinding]] = {}
self._coordination_reviews: dict[str, CoordinationReview] = {}
```

Add methods:

```python
def save_agent_findings(
    self,
    investigation_id: str,
    findings: list[AgentFinding],
) -> list[AgentFinding]:
    bucket = self._agent_findings.setdefault(investigation_id, {})
    for finding in findings:
        bucket[finding.id] = finding
    return list(findings)

def list_agent_findings(self, investigation_id: str) -> list[AgentFinding]:
    return list(self._agent_findings.get(investigation_id, {}).values())

def save_coordination_review(
    self,
    review: CoordinationReview,
) -> CoordinationReview:
    self._coordination_reviews[review.investigation_id] = review
    return review

def get_coordination_review(self, investigation_id: str) -> CoordinationReview | None:
    return self._coordination_reviews.get(investigation_id)
```

- [ ] **Step 5: Add SQLite repository methods**

Modify imports in `backend/db/sqlite_repository.py`:

```python
from backend.db.schema import (
    agent_findings,
    coordination_reviews,
    ...
)
from backend.domain.agent_findings import AgentFinding, CoordinationReview
```

Add methods near the V4 agent persistence methods:

```python
def save_agent_findings(
    self,
    investigation_id: str,
    findings: Sequence[AgentFinding],
) -> list[AgentFinding]:
    rows = []
    for finding in findings:
        payload = finding.model_dump(mode="json")
        rows.append(
            {
                "id": finding.id,
                "investigation_id": investigation_id,
                "agent_name": payload["agent_name"],
                "created_at": payload["created_at"],
                "payload": payload,
            }
        )
    with self.engine.begin() as connection:
        self._upsert_payload_rows(connection, agent_findings, rows)
    return list(findings)

def list_agent_findings(self, investigation_id: str) -> list[AgentFinding]:
    statement = (
        select(agent_findings)
        .where(agent_findings.c.investigation_id == investigation_id)
        .order_by(agent_findings.c.created_at, agent_findings.c.id)
    )
    with self.engine.connect() as connection:
        rows = connection.execute(statement).mappings().all()
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
    if row is None:
        return None
    return CoordinationReview(**row["payload"])
```

- [ ] **Step 6: Run persistence tests**

Run:

```powershell
uv run pytest tests/db/test_v5_agentic_rca_persistence.py tests/db/test_v4_agent_persistence.py -v
uv run ruff check backend/db/schema.py backend/db/repositories.py backend/db/sqlite_repository.py tests/db/test_v5_agentic_rca_persistence.py
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add backend/db/schema.py backend/db/repositories.py backend/db/sqlite_repository.py tests/db/test_v5_agentic_rca_persistence.py
git commit -m "feat: persist v5 agentic rca records"
```

---

### Task 3: Deterministic Finding Builders

**Files:**
- Create: `backend/diagnosis/finding_builders.py`
- Test: `tests/diagnosis/test_finding_builders.py`

- [ ] **Step 1: Write failing builder tests**

Create `tests/diagnosis/test_finding_builders.py`:

```python
from datetime import UTC, datetime

from backend.diagnosis.finding_builders import build_agent_findings
from backend.domain.agent_findings import AgentFindingType, AgentName
from backend.domain.events import IncidentEvent
from backend.domain.evidence import (
    EvidenceItem,
    EvidenceKind,
    EvidenceProvider,
    EvidenceStatus,
)
from backend.domain.hypotheses import CauseType


def make_event() -> IncidentEvent:
    return IncidentEvent(
        source="test",
        service="checkout-service",
        environment="prod",
        severity="critical",
        title="Checkout 500s",
        description="checkout-service has 500 errors after deployment",
        started_at=datetime(2026, 7, 8, 10, 0, tzinfo=UTC),
    )


def test_builders_create_log_metric_and_deployment_findings():
    evidence = [
        EvidenceItem(
            id="ev-log",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.LOG_PATTERN,
            status=EvidenceStatus.SUCCESS,
            summary="NullPointerException spiked after deployment.",
            payload={"exception": "NullPointerException"},
            confidence=0.9,
        ),
        EvidenceItem(
            id="ev-metric",
            provider=EvidenceProvider.METRIC,
            kind=EvidenceKind.METRIC_TREND,
            status=EvidenceStatus.SUCCESS,
            summary="5xx rate increased.",
            payload={"error_rate": "12%"},
            confidence=0.8,
        ),
        EvidenceItem(
            id="ev-deploy",
            provider=EvidenceProvider.DEPLOYMENT,
            kind=EvidenceKind.DEPLOYMENT_EVENT,
            status=EvidenceStatus.SUCCESS,
            summary="checkout-service deployed version 2026.07.08.",
            payload={"version": "2026.07.08"},
            confidence=0.9,
        ),
    ]

    findings = build_agent_findings("inv-1", make_event(), evidence)

    assert {finding.agent_name for finding in findings} == {
        AgentName.LOG,
        AgentName.METRIC,
        AgentName.DEPLOYMENT,
    }
    assert all(finding.investigation_id == "inv-1" for finding in findings)
    assert all(finding.evidence_ids for finding in findings)
    assert any(
        finding.related_cause_type == CauseType.DEPLOYMENT_REGRESSION
        for finding in findings
    )


def test_provider_failure_becomes_gap_finding():
    evidence = [
        EvidenceItem(
            id="ev-log-failed",
            provider=EvidenceProvider.LOG,
            kind=EvidenceKind.PROVIDER_ERROR,
            status=EvidenceStatus.FAILED,
            summary="log provider failed",
            payload={"reason": "timeout"},
            confidence=1.0,
            error_message="timeout",
        )
    ]

    findings = build_agent_findings("inv-1", make_event(), evidence)

    assert findings
    assert findings[0].agent_name == AgentName.LOG
    assert findings[0].finding_type == AgentFindingType.GAP
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/diagnosis/test_finding_builders.py -v
```

Expected: FAIL with missing module.

- [ ] **Step 3: Add deterministic builders**

Create `backend/diagnosis/finding_builders.py`:

```python
from collections.abc import Iterable

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingSeverity,
    AgentFindingType,
    AgentName,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceProvider, EvidenceStatus
from backend.domain.hypotheses import CauseType


def build_agent_findings(
    investigation_id: str,
    event: IncidentEvent,
    evidence: list[EvidenceItem],
) -> list[AgentFinding]:
    findings: list[AgentFinding] = []
    findings.extend(_build_for_provider(investigation_id, event, evidence, EvidenceProvider.LOG))
    findings.extend(
        _build_for_provider(investigation_id, event, evidence, EvidenceProvider.METRIC)
    )
    findings.extend(
        _build_for_provider(investigation_id, event, evidence, EvidenceProvider.DEPLOYMENT)
    )
    return findings


def _build_for_provider(
    investigation_id: str,
    event: IncidentEvent,
    evidence: list[EvidenceItem],
    provider: EvidenceProvider,
) -> list[AgentFinding]:
    provider_evidence = [item for item in evidence if item.provider == provider]
    failures = [item for item in provider_evidence if item.status == EvidenceStatus.FAILED]
    usable = [item for item in provider_evidence if item.status != EvidenceStatus.FAILED]

    if failures and not usable:
        return [
            AgentFinding(
                investigation_id=investigation_id,
                agent_name=_agent_for_provider(provider),
                finding_type=AgentFindingType.GAP,
                summary=f"{provider} evidence is unavailable.",
                confidence=0.2,
                evidence_ids=[item.id for item in failures],
                severity=AgentFindingSeverity.MEDIUM,
                rationale="Provider failure prevents this specialist from confirming a cause.",
                gaps=[item.error_message or item.summary for item in failures],
            )
        ]

    if not usable:
        return []

    cause_type = _infer_cause_type(event, usable, provider)
    return [
        AgentFinding(
            investigation_id=investigation_id,
            agent_name=_agent_for_provider(provider),
            finding_type=(
                AgentFindingType.ROOT_CAUSE if cause_type else AgentFindingType.SIGNAL
            ),
            summary=_summary_for_provider(provider, usable, cause_type),
            confidence=max(item.confidence for item in usable),
            evidence_ids=[item.id for item in usable],
            related_cause_type=cause_type,
            severity=AgentFindingSeverity.HIGH if cause_type else AgentFindingSeverity.MEDIUM,
            rationale="; ".join(item.summary for item in usable[:3]),
        )
    ]


def _agent_for_provider(provider: EvidenceProvider) -> AgentName:
    return {
        EvidenceProvider.LOG: AgentName.LOG,
        EvidenceProvider.METRIC: AgentName.METRIC,
        EvidenceProvider.DEPLOYMENT: AgentName.DEPLOYMENT,
    }[provider]


def _infer_cause_type(
    event: IncidentEvent,
    evidence: Iterable[EvidenceItem],
    provider: EvidenceProvider,
) -> CauseType | None:
    text = " ".join(
        [event.title, event.description]
        + [item.summary for item in evidence]
        + [str(item.payload) for item in evidence]
    ).lower()
    if provider == EvidenceProvider.DEPLOYMENT or any(
        word in text for word in ("deploy", "release", "rollback")
    ):
        return CauseType.DEPLOYMENT_REGRESSION
    if any(word in text for word in ("traffic", "qps", "spike")):
        return CauseType.TRAFFIC_SPIKE
    if any(word in text for word in ("timeout", "dependency", "downstream")):
        return CauseType.DOWNSTREAM_DEPENDENCY_FAILURE
    if any(word in text for word in ("database", "db", "slow query")):
        return CauseType.DATABASE_SLOWDOWN
    return None


def _summary_for_provider(
    provider: EvidenceProvider,
    evidence: list[EvidenceItem],
    cause_type: CauseType | None,
) -> str:
    label = {
        EvidenceProvider.LOG: "LogAgent",
        EvidenceProvider.METRIC: "MetricAgent",
        EvidenceProvider.DEPLOYMENT: "DeploymentAgent",
    }[provider]
    if cause_type is not None:
        return f"{label} found evidence for {cause_type}."
    return f"{label} found relevant incident signals."
```

- [ ] **Step 4: Run builder tests**

Run:

```powershell
uv run pytest tests/diagnosis/test_finding_builders.py -v
uv run ruff check backend/diagnosis/finding_builders.py tests/diagnosis/test_finding_builders.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/diagnosis/finding_builders.py tests/diagnosis/test_finding_builders.py
git commit -m "feat: build v5 agent findings from evidence"
```

---

### Task 4: Coordinator Review

**Files:**
- Create: `backend/diagnosis/coordination_review.py`
- Test: `tests/diagnosis/test_coordination_review.py`

- [ ] **Step 1: Write failing coordinator tests**

Create `tests/diagnosis/test_coordination_review.py`:

```python
import pytest

from backend.diagnosis.coordination_review import build_coordination_review
from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    AgentName,
)
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis


def evidence(evidence_id: str) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        summary="evidence",
        payload={},
        confidence=0.8,
    )


def finding(
    finding_id: str,
    agent_name: AgentName,
    cause_type: CauseType,
    confidence: float,
) -> AgentFinding:
    return AgentFinding(
        id=finding_id,
        investigation_id="inv-1",
        agent_name=agent_name,
        finding_type=AgentFindingType.ROOT_CAUSE,
        summary=f"{agent_name} supports {cause_type}",
        confidence=confidence,
        evidence_ids=[f"ev-{finding_id}"],
        related_cause_type=cause_type,
    )


def test_coordination_review_ranks_supported_candidate_first():
    findings = [
        finding("log", AgentName.LOG, CauseType.DEPLOYMENT_REGRESSION, 0.9),
        finding("deploy", AgentName.DEPLOYMENT, CauseType.DEPLOYMENT_REGRESSION, 0.8),
        finding("metric", AgentName.METRIC, CauseType.TRAFFIC_SPIKE, 0.4),
    ]
    evidence_items = [evidence(f"ev-{item.id}") for item in findings]

    review = build_coordination_review("inv-1", findings, evidence_items, [])

    assert review.candidates[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert review.candidates[0].rank == 1
    assert set(review.candidates[0].supporting_finding_ids) == {"log", "deploy"}


def test_coordination_review_rejects_unknown_evidence_reference():
    bad = AgentFinding(
        id="bad",
        investigation_id="inv-1",
        agent_name=AgentName.LOG,
        finding_type=AgentFindingType.SIGNAL,
        summary="bad evidence",
        confidence=0.7,
        evidence_ids=["ev-missing"],
    )

    with pytest.raises(ValueError, match="Unknown evidence id"):
        build_coordination_review("inv-1", [bad], [], [])


def test_coordination_review_uses_hypotheses_when_findings_are_weak():
    hypothesis = Hypothesis(
        cause_type=CauseType.UNKNOWN,
        summary="Unknown root cause.",
        confidence=0.2,
        supporting_evidence_ids=["ev-1"],
        next_actions=["Collect more evidence."],
    )

    review = build_coordination_review("inv-1", [], [evidence("ev-1")], [hypothesis])

    assert review.candidates[0].cause_type == CauseType.UNKNOWN
    assert "limited specialist findings" in review.candidates[0].uncertainty
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/diagnosis/test_coordination_review.py -v
```

Expected: FAIL with missing module.

- [ ] **Step 3: Add coordinator implementation**

Create `backend/diagnosis/coordination_review.py`:

```python
from collections import defaultdict

from backend.domain.agent_findings import (
    AgentFinding,
    AgentFindingType,
    CoordinationReview,
    RootCauseCandidate,
)
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import CauseType, Hypothesis


def build_coordination_review(
    investigation_id: str,
    findings: list[AgentFinding],
    evidence: list[EvidenceItem],
    hypotheses: list[Hypothesis],
) -> CoordinationReview:
    _validate_references(findings, evidence)
    candidates = _candidates_from_findings(findings)
    if not candidates:
        candidates = _candidates_from_hypotheses(hypotheses)
    return CoordinationReview(investigation_id=investigation_id, candidates=candidates)


def _validate_references(
    findings: list[AgentFinding],
    evidence: list[EvidenceItem],
) -> None:
    evidence_ids = {item.id for item in evidence}
    for finding in findings:
        missing = set(finding.evidence_ids) - evidence_ids
        if missing:
            raise ValueError(f"Unknown evidence id in finding {finding.id}: {sorted(missing)}")


def _candidates_from_findings(
    findings: list[AgentFinding],
) -> list[RootCauseCandidate]:
    grouped: dict[CauseType, list[AgentFinding]] = defaultdict(list)
    contradictions: dict[CauseType, list[AgentFinding]] = defaultdict(list)
    for finding in findings:
        if finding.related_cause_type is None:
            continue
        if finding.finding_type == AgentFindingType.CONTRADICTION:
            contradictions[finding.related_cause_type].append(finding)
        elif finding.finding_type != AgentFindingType.GAP:
            grouped[finding.related_cause_type].append(finding)

    scored = sorted(
        grouped.items(),
        key=lambda item: (_score_findings(item[1]), item[0]),
        reverse=True,
    )
    candidates: list[RootCauseCandidate] = []
    for rank, (cause_type, supporting) in enumerate(scored, start=1):
        contradicting = contradictions.get(cause_type, [])
        confidence = max(0.0, min(1.0, _score_findings(supporting) - 0.1 * len(contradicting)))
        candidates.append(
            RootCauseCandidate(
                cause_type=cause_type,
                summary=f"{cause_type} is candidate #{rank}.",
                rank=rank,
                confidence=confidence,
                supporting_finding_ids=[finding.id for finding in supporting],
                contradicting_finding_ids=[finding.id for finding in contradicting],
                supporting_evidence_ids=_unique_ids(
                    evidence_id
                    for finding in supporting
                    for evidence_id in finding.evidence_ids
                ),
                contradicting_evidence_ids=_unique_ids(
                    evidence_id
                    for finding in contradicting
                    for evidence_id in finding.evidence_ids
                ),
                rationale="; ".join(finding.summary for finding in supporting),
                uncertainty=_uncertainty_for(supporting, contradicting),
            )
        )
    return candidates


def _candidates_from_hypotheses(
    hypotheses: list[Hypothesis],
) -> list[RootCauseCandidate]:
    return [
        RootCauseCandidate(
            cause_type=hypothesis.cause_type,
            summary=hypothesis.summary,
            rank=rank,
            confidence=hypothesis.confidence,
            supporting_evidence_ids=hypothesis.supporting_evidence_ids,
            contradicting_evidence_ids=hypothesis.contradicting_evidence_ids,
            rationale="Seeded from existing RCA hypothesis.",
            uncertainty="limited specialist findings; use base RCA hypothesis.",
        )
        for rank, hypothesis in enumerate(hypotheses, start=1)
    ]


def _score_findings(findings: list[AgentFinding]) -> float:
    if not findings:
        return 0.0
    return min(1.0, sum(finding.confidence for finding in findings) / len(findings) + 0.1 * (len(findings) - 1))


def _uncertainty_for(
    supporting: list[AgentFinding],
    contradicting: list[AgentFinding],
) -> str:
    if contradicting:
        return "Some specialist findings contradict this candidate."
    if len(supporting) == 1:
        return "Only one specialist currently supports this candidate."
    return "Supported by multiple specialist findings."


def _unique_ids(values) -> list[str]:
    return list(dict.fromkeys(values))
```

- [ ] **Step 4: Run coordinator tests**

Run:

```powershell
uv run pytest tests/diagnosis/test_coordination_review.py -v
uv run ruff check backend/diagnosis/coordination_review.py tests/diagnosis/test_coordination_review.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/diagnosis/coordination_review.py tests/diagnosis/test_coordination_review.py
git commit -m "feat: rank v5 rca candidates"
```

---

### Task 5: Wire V5 Into The Orchestrator

**Files:**
- Modify: `backend/diagnosis/orchestrator.py`
- Test: `tests/diagnosis/test_orchestrator.py`
- Test: `tests/golden/test_golden_cases.py`

- [ ] **Step 1: Add failing orchestrator test**

Append to `tests/diagnosis/test_orchestrator.py`:

```python
def test_orchestrator_records_v5_findings_and_coordination_review():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)

    record = orchestrator.run(load_incident_case("deployment_regression"))

    findings = repository.list_agent_findings(record.id)
    review = repository.get_coordination_review(record.id)

    assert findings
    assert {finding.agent_name for finding in findings} >= {
        "LogAgent",
        "MetricAgent",
        "DeploymentAgent",
    }
    assert review is not None
    assert review.candidates
    assert review.candidates[0].supporting_evidence_ids
```

Append to `tests/golden/test_golden_cases.py`:

```python
def test_v5_deployment_regression_candidate_is_evidence_linked():
    orchestrator = build_v2_orchestrator()
    record = orchestrator.run(load_incident_case("deployment_regression"))
    repo = orchestrator.repository

    review = repo.get_coordination_review(record.id)
    findings = repo.list_agent_findings(record.id)
    evidence_ids = {item.id for item in record.evidence}
    finding_ids = {item.id for item in findings}

    assert review is not None
    assert review.candidates
    assert review.candidates[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    for candidate in review.candidates:
        assert set(candidate.supporting_evidence_ids) <= evidence_ids
        assert set(candidate.contradicting_evidence_ids) <= evidence_ids
        assert set(candidate.supporting_finding_ids) <= finding_ids
        assert set(candidate.contradicting_finding_ids) <= finding_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/diagnosis/test_orchestrator.py::test_orchestrator_records_v5_findings_and_coordination_review -v
```

Expected: FAIL because orchestrator does not save V5 records.

- [ ] **Step 3: Wire V5 after hypotheses are created**

Modify imports in `backend/diagnosis/orchestrator.py`:

```python
from backend.diagnosis.coordination_review import build_coordination_review
from backend.diagnosis.finding_builders import build_agent_findings
```

After:

```python
hypotheses = self.analyzer.analyze(event, evidence)
record.hypotheses = hypotheses
record.updated_at = datetime.now(UTC)
self.repository.save(record)
```

add:

```python
self._record_v5_coordination(record.id, event, evidence, hypotheses)
```

Add method:

```python
def _record_v5_coordination(
    self,
    investigation_id: str,
    event: IncidentEvent,
    evidence: list[EvidenceItem],
    hypotheses,
) -> None:
    try:
        findings = build_agent_findings(investigation_id, event, evidence)
        review = build_coordination_review(investigation_id, findings, evidence, hypotheses)
        save_findings = getattr(self.repository, "save_agent_findings", None)
        save_review = getattr(self.repository, "save_coordination_review", None)
        if callable(save_findings):
            save_findings(investigation_id, findings)
        if callable(save_review):
            save_review(review)
    except Exception as exc:
        logger.warning(
            "v5 coordination recording failed id=%s reason=%s",
            investigation_id,
            exc,
            exc_info=True,
        )
```

- [ ] **Step 4: Run orchestrator and golden tests**

Run:

```powershell
uv run pytest tests/diagnosis/test_orchestrator.py tests/golden/test_golden_cases.py -v
uv run ruff check backend/diagnosis/orchestrator.py tests/diagnosis/test_orchestrator.py tests/golden/test_golden_cases.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/diagnosis/orchestrator.py tests/diagnosis/test_orchestrator.py tests/golden/test_golden_cases.py
git commit -m "feat: record v5 agentic rca review"
```

---

### Task 6: V5 Agentic RCA APIs

**Files:**
- Modify: `backend/api/agent_views.py`
- Test: `tests/api/test_v5_agentic_rca_api.py`

- [ ] **Step 1: Write failing API tests**

Create `tests/api/test_v5_agentic_rca_api.py`:

```python
import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.services.container import reset_container


@pytest.fixture(autouse=True)
def reset_api_container():
    reset_container()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def investigation_id(client: TestClient) -> str:
    response = client.post("/events/simulated/deployment_regression")
    assert response.status_code == 200
    return response.json()["id"]


def test_v5_agentic_rca_endpoints_return_workbench_data(
    client: TestClient,
    investigation_id: str,
):
    findings_response = client.get(f"/investigations/{investigation_id}/agent-findings")
    review_response = client.get(f"/investigations/{investigation_id}/coordination-review")
    workbench_response = client.get(f"/investigations/{investigation_id}/rca-workbench")

    assert findings_response.status_code == 200
    assert review_response.status_code == 200
    assert workbench_response.status_code == 200

    findings = findings_response.json()
    review = review_response.json()
    workbench = workbench_response.json()

    assert findings
    assert review["candidates"]
    assert workbench["findings"] == findings
    assert workbench["candidates"] == review["candidates"]
    assert workbench["evidence"]
    assert workbench["graph_seed"]["nodes"]
    assert workbench["graph_seed"]["edges"]


@pytest.mark.parametrize(
    "suffix",
    ["agent-findings", "coordination-review", "rca-workbench"],
)
def test_unknown_investigation_v5_endpoints_return_404(client: TestClient, suffix: str):
    response = client.get(f"/investigations/inv-not-found/{suffix}")

    assert response.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/api/test_v5_agentic_rca_api.py -v
```

Expected: FAIL with 404 for missing endpoints.

- [ ] **Step 3: Add API endpoints and graph seed builder**

Modify `backend/api/agent_views.py` imports:

```python
from backend.domain.agent_findings import AgentFinding, CoordinationReview
```

Add helpers:

```python
def _list_agent_findings(investigation_id: str) -> list[AgentFinding]:
    list_findings = getattr(_repository(), "list_agent_findings", None)
    if not callable(list_findings):
        return []
    return list_findings(investigation_id)


def _get_coordination_review(investigation_id: str) -> CoordinationReview | None:
    get_review = getattr(_repository(), "get_coordination_review", None)
    if not callable(get_review):
        return None
    return get_review(investigation_id)
```

Add endpoints:

```python
@router.get("/{investigation_id}/agent-findings", response_model=list[AgentFinding])
def list_investigation_agent_findings(investigation_id: str) -> list[AgentFinding]:
    _get_investigation_record(investigation_id)
    return _list_agent_findings(investigation_id)


@router.get("/{investigation_id}/coordination-review", response_model=CoordinationReview | None)
def get_investigation_coordination_review(
    investigation_id: str,
) -> CoordinationReview | None:
    _get_investigation_record(investigation_id)
    return _get_coordination_review(investigation_id)


@router.get("/{investigation_id}/rca-workbench")
def get_investigation_rca_workbench(investigation_id: str):
    record = _get_investigation_record(investigation_id)
    findings = _list_agent_findings(investigation_id)
    review = _get_coordination_review(investigation_id)
    candidates = review.candidates if review is not None else []
    return {
        "investigation": record,
        "findings": findings,
        "candidates": candidates,
        "evidence": record.evidence,
        "graph_seed": _build_workbench_graph_seed(findings, candidates, record.evidence),
    }


def _build_workbench_graph_seed(findings, candidates, evidence):
    evidence_by_id = {item.id: item for item in evidence}
    nodes = []
    edges = []
    for finding in findings:
        agent_id = f"agent:{finding.agent_name}"
        nodes.append({"id": agent_id, "label": finding.agent_name, "type": "agent"})
        nodes.append({"id": finding.id, "label": finding.summary, "type": "finding"})
        edges.append({"source": agent_id, "target": finding.id, "relation": "produced"})
        for evidence_id in finding.evidence_ids:
            if evidence_id in evidence_by_id:
                nodes.append(
                    {
                        "id": evidence_id,
                        "label": evidence_by_id[evidence_id].summary,
                        "type": "evidence",
                    }
                )
                edges.append(
                    {"source": evidence_id, "target": finding.id, "relation": "cites"}
                )
    for candidate in candidates:
        nodes.append({"id": candidate.id, "label": candidate.summary, "type": "candidate"})
        for finding_id in candidate.supporting_finding_ids:
            edges.append({"source": finding_id, "target": candidate.id, "relation": "supports"})
        for finding_id in candidate.contradicting_finding_ids:
            edges.append({"source": finding_id, "target": candidate.id, "relation": "contradicts"})
    return {
        "nodes": list({node["id"]: node for node in nodes}.values()),
        "edges": edges,
    }
```

- [ ] **Step 4: Run API tests**

Run:

```powershell
uv run pytest tests/api/test_v5_agentic_rca_api.py tests/api/test_v4_agent_views_api.py -v
uv run ruff check backend/api/agent_views.py tests/api/test_v5_agentic_rca_api.py
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/api/agent_views.py tests/api/test_v5_agentic_rca_api.py
git commit -m "feat: expose v5 rca workbench api"
```

---

### Task 7: Frontend RCA Workbench

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `tests/frontend/test_frontend_smoke.py`

- [ ] **Step 1: Write failing frontend smoke expectations**

Modify `tests/frontend/test_frontend_smoke.py` in `test_app_contains_investigation_list_and_detail_ui_strings` to assert:

```python
assert "Agent 判断" in app
assert "候选根因排序" in app
assert "证据链预览" in app
```

Modify `test_api_exposes_v4_agent_process_methods` or add a new test:

```python
def test_api_exposes_v5_agentic_rca_methods() -> None:
    api = (FRONTEND / "src" / "api.ts").read_text(encoding="utf-8")

    for method_name in [
        "getAgentFindings",
        "getCoordinationReview",
        "getRcaWorkbench",
    ]:
        assert f"function {method_name}" in api
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
uv run pytest tests/frontend/test_frontend_smoke.py -v
```

Expected: FAIL because methods and labels do not exist.

- [ ] **Step 3: Add frontend API types and methods**

Modify `frontend/src/api.ts` after V4 types:

```ts
export type AgentFinding = {
  id: string;
  investigation_id: string;
  agent_name: string;
  finding_type: string;
  summary: string;
  confidence: number;
  evidence_ids: string[];
  related_cause_type?: string | null;
  severity: string;
  rationale: string;
  gaps: string[];
  created_at: string;
};

export type RootCauseCandidate = {
  id: string;
  cause_type: string;
  summary: string;
  rank: number;
  confidence: number;
  supporting_finding_ids: string[];
  contradicting_finding_ids: string[];
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  rationale: string;
  uncertainty: string;
};

export type CoordinationReview = {
  id: string;
  investigation_id: string;
  candidates: RootCauseCandidate[];
  created_at: string;
};

export type RcaWorkbench = {
  investigation: InvestigationRecord;
  findings: AgentFinding[];
  candidates: RootCauseCandidate[];
  evidence: EvidenceItem[];
  graph_seed: {
    nodes: Array<{ id: string; label: string; type: string }>;
    edges: Array<{ source: string; target: string; relation: string }>;
  };
};
```

Add methods near V4 API methods:

```ts
export function getAgentFindings(id: string) {
  return request<AgentFinding[]>(`/investigations/${id}/agent-findings`);
}

export function getCoordinationReview(id: string) {
  return request<CoordinationReview | null>(`/investigations/${id}/coordination-review`);
}

export function getRcaWorkbench(id: string) {
  return request<RcaWorkbench>(`/investigations/${id}/rca-workbench`);
}
```

- [ ] **Step 4: Add workbench panel to App**

Modify `frontend/src/App.tsx`:

1. Import `getRcaWorkbench` and V5 types.
2. In the investigation detail area, add a query keyed by `["rca-workbench", selectedInvestigationId]`.
3. Render a compact section:

```tsx
function RcaWorkbenchPanel({ investigationId }: { investigationId: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["rca-workbench", investigationId],
    queryFn: () => getRcaWorkbench(investigationId),
    enabled: Boolean(investigationId),
  });

  if (isLoading) {
    return <section className="panel">加载 RCA 工作台...</section>;
  }

  if (error) {
    return <section className="panel">RCA 工作台加载失败</section>;
  }

  const findingsByAgent = (data?.findings ?? []).reduce<Record<string, AgentFinding[]>>(
    (groups, finding) => {
      groups[finding.agent_name] = [...(groups[finding.agent_name] ?? []), finding];
      return groups;
    },
    {},
  );

  return (
    <section className="panel rca-workbench">
      <h2>Agent 判断</h2>
      {Object.entries(findingsByAgent).map(([agentName, findings]) => (
        <div className="compact-list" key={agentName}>
          <h3>{agentName}</h3>
          {findings.map((finding) => (
            <article className="compact-row" key={finding.id}>
              <strong>{finding.summary}</strong>
              <span>置信度 {(finding.confidence * 100).toFixed(0)}%</span>
              <span>证据 {finding.evidence_ids.join(", ") || "无"}</span>
              {finding.rationale ? <p>{finding.rationale}</p> : null}
            </article>
          ))}
        </div>
      ))}

      <h2>候选根因排序</h2>
      {(data?.candidates ?? []).map((candidate) => (
        <article className="compact-row" key={candidate.id}>
          <strong>
            #{candidate.rank} {candidate.cause_type}
          </strong>
          <span>置信度 {(candidate.confidence * 100).toFixed(0)}%</span>
          <p>{candidate.rationale}</p>
          <p>{candidate.uncertainty}</p>
        </article>
      ))}

      <h2>证据链预览</h2>
      {(data?.graph_seed.edges ?? []).map((edge) => (
        <div className="evidence-link" key={`${edge.source}-${edge.relation}-${edge.target}`}>
          <code>{edge.source}</code>
          <span>{edge.relation}</span>
          <code>{edge.target}</code>
        </div>
      ))}
    </section>
  );
}
```

Keep this component in `App.tsx` for V5. Split it only if the file becomes painful during implementation.

- [ ] **Step 5: Add minimal CSS**

Modify `frontend/src/styles.css` only if existing panel/list classes are not enough:

```css
.rca-workbench {
  display: grid;
  gap: 0.75rem;
}

.compact-list,
.compact-row {
  display: grid;
  gap: 0.35rem;
}

.evidence-link {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}
```

- [ ] **Step 6: Run frontend checks**

Run:

```powershell
uv run pytest tests/frontend/test_frontend_smoke.py -v
cd frontend
npm.cmd run build
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css tests/frontend/test_frontend_smoke.py
git commit -m "feat: show v5 rca workbench"
```

---

### Task 8: Docs, Safety Review, And Final Verification

**Files:**
- Modify: `README.md`
- Test: no new test file unless final verification exposes a gap.

- [ ] **Step 1: Add README V5 section**

Add a short section after the V4 section in `README.md`:

```markdown
## V5 Agentic RCA Workbench

DiagOps V5 adds a read-only RCA workbench on top of the V4 agent process:

- LogAgent, MetricAgent, and DeploymentAgent produce independent findings.
- The coordinator ranks multiple root-cause candidates instead of forcing one answer.
- Candidates cite supporting and contradicting findings plus evidence IDs.
- The frontend shows Agent 判断, 候选根因排序, and 证据链预览.

V5 remains read-only. It does not execute rollback, restart, scaling, SSH, or
configuration changes. Optional LLM enhancement is disabled by default and is
not required for the workbench.

### V5 RCA APIs

```text
GET /investigations/{id}/agent-findings
GET /investigations/{id}/coordination-review
GET /investigations/{id}/rca-workbench
```
```

- [ ] **Step 2: Run focused backend checks**

Run:

```powershell
uv run pytest tests/domain/test_agent_findings.py tests/diagnosis/test_finding_builders.py tests/diagnosis/test_coordination_review.py tests/api/test_v5_agentic_rca_api.py -v
```

Expected: PASS.

- [ ] **Step 3: Run full backend checks**

Run:

```powershell
uv run ruff check .
uv run pytest -v
```

Expected: PASS. Existing Starlette/TestClient deprecation warning is acceptable if tests pass.

- [ ] **Step 4: Run frontend build**

Run:

```powershell
cd frontend
npm.cmd run build
```

Expected: PASS.

- [ ] **Step 5: Manual API smoke**

If a backend server is not running, start it:

```powershell
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
$created = Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/events/simulated/deployment_regression"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/investigations/$($created.id)/agent-findings"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/investigations/$($created.id)/coordination-review"
Invoke-RestMethod -Uri "http://127.0.0.1:8000/investigations/$($created.id)/rca-workbench"
```

Expected:

1. Findings include `LogAgent`, `MetricAgent`, and `DeploymentAgent`.
2. Coordination review has at least one candidate.
3. Workbench has graph seed nodes and edges.

- [ ] **Step 6: Safety wording scan**

Run:

```powershell
rg -n "automatic rollback|automatic restart|automatic scale|automatic config change|自动回滚|自动重启|自动扩容|自动配置变更" README.md frontend/src backend docs/superpowers/specs/2026-07-08-diagops-v5-agentic-rca-workbench-design.md
```

Expected: Any matches must be explicit non-goals or safety warnings, not claims that DiagOps executes production changes.

- [ ] **Step 7: Commit docs**

```powershell
git add README.md
git commit -m "docs: document v5 agentic rca workbench"
```

---

## Final Review Checklist

- [ ] V5 is additive; V1-V4 APIs still work.
- [ ] LogAgent, MetricAgent, and DeploymentAgent findings are persisted per investigation.
- [ ] Coordinator review ranks multiple candidates and records traceable finding/evidence IDs.
- [ ] Workbench API returns `investigation`, `findings`, `candidates`, `evidence`, and `graph_seed`.
- [ ] Frontend shows `Agent 判断`, `候选根因排序`, and `证据链预览`.
- [ ] LLM enhancement remains disabled by default; no real LLM call is added in this plan.
- [ ] No production mutation capability is introduced.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run pytest -v` passes.
- [ ] `cd frontend; npm.cmd run build` passes.
