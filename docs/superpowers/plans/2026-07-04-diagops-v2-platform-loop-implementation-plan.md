# DiagOps V2 Platform Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade DiagOps from a one-shot RCA backend into an API-level platform loop with investigation state, failed-state persistence, provider status semantics, lightweight specialists, recommended actions, verification suggestions, and action/verification status APIs.

**Architecture:** Keep the project-owned RCA framework as the source of truth. Add small domain models and services around the existing FastAPI, Pydantic, provider, analyzer, report, and in-memory repository structure; do not introduce a full agent SDK or any production mutation capability in V2.

**Tech Stack:** Python, FastAPI, Pydantic, pytest, ruff, uv, in-memory repository.

---

## Scope Check

This plan implements the V2 API/backend loop only. It does not implement UI, SSH execution, real provider integrations, persistent SQLite/PostgreSQL storage, mobile approval, or automatic remediation.

The spec's UI section is explicitly out of V2 mandatory scope. All acceptance criteria in this plan are API, domain, repository, report, and test based.

## File Structure

Create:

- `backend/domain/actions.py` - recommended action and verification suggestion domain models.
- `backend/providers/results.py` - provider result status and aggregation model.
- `backend/diagnosis/context.py` - diagnosis context and specialist result models.
- `backend/diagnosis/coordinator.py` - lightweight coordinator and specialists that wrap provider collection.
- `backend/diagnosis/action_planner.py` - deterministic recommended action and verification suggestion generation.
- `tests/domain/test_action_models.py` - action and verification model tests.
- `tests/providers/test_provider_results.py` - provider result status tests.
- `tests/diagnosis/test_action_planner.py` - action planner tests.
- `tests/diagnosis/test_coordinator.py` - coordinator and specialist result tests.

Modify:

- `backend/domain/evidence.py` - add evidence status and related alert evidence types.
- `backend/domain/reports.py` - attach action and verification ids to reports.
- `backend/db/models.py` - expand investigation states, failure reason, actions, verifications, timestamps.
- `backend/db/repositories.py` - add update semantics and child object status mutation.
- `backend/providers/base.py` - provider protocol returns `ProviderResult`.
- `backend/providers/registry.py` - aggregate provider results and convert failures to evidence.
- `backend/providers/mock_logs.py` - return provider results.
- `backend/providers/mock_metrics.py` - return provider results.
- `backend/providers/mock_deploys.py` - return provider results.
- `backend/providers/mock_dependencies.py` - return provider results.
- `backend/providers/mock_service_catalog.py` - add service catalog mock provider.
- `backend/providers/mock_related_alerts.py` - add related alert mock provider.
- `backend/diagnosis/orchestrator.py` - persist pending/running/completed/failed and call coordinator/action planner/report.
- `backend/reports/generator.py` - validate all hypothesis/action evidence ids and render V2 sections.
- `backend/api/events.py` - return status-safe summaries and add manual endpoint.
- `backend/api/investigations.py` - return V2 detail and update action/verification status.
- `backend/services/container.py` - wire new providers and services.
- `tests/api/test_events_api.py` - adjust event status expectations and manual API tests.
- `tests/api/test_investigations_api.py` - action/verification status API tests.
- `tests/diagnosis/test_orchestrator.py` - status transition and failure persistence tests.
- `tests/golden/test_golden_cases.py` - V2 action and verification assertions.
- `tests/providers/test_mock_providers.py` - provider result tests.
- `tests/reports/test_generator.py` - V2 report section and validation tests.
- `README.md` - document V2 action and verification APIs.
- `docs/examples/webhook-event.json` - keep existing shape compatible.

---

### Task 1: Domain Models For Actions, Verification, Evidence Status, And Investigation State

**Files:**
- Create: `backend/domain/actions.py`
- Modify: `backend/domain/evidence.py`
- Modify: `backend/domain/reports.py`
- Modify: `backend/db/models.py`
- Test: `tests/domain/test_action_models.py`
- Test: `tests/domain/test_domain_models.py`

- [ ] **Step 1: Write action and verification model tests**

Create `tests/domain/test_action_models.py`:

```python
import pytest
from pydantic import ValidationError

from backend.domain.actions import (
    ActionRiskLevel,
    ActionStatus,
    ActionType,
    RecommendedAction,
    VerificationStatus,
    VerificationSuggestion,
)


def test_recommended_action_defaults_to_proposed_and_requires_evidence():
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Deployment evidence and new errors point to a regression.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=["ev-deploy", "ev-log"],
    )

    assert action.id.startswith("act-")
    assert action.status == ActionStatus.PROPOSED
    assert action.supporting_evidence_ids == ["ev-deploy", "ev-log"]


def test_recommended_action_rejects_high_risk_without_approval():
    with pytest.raises(ValidationError, match="medium and high risk actions require approval"):
        RecommendedAction(
            action_type=ActionType.RESTART_SUGGESTION,
            title="Restart service",
            description="Restart may affect traffic.",
            risk_level=ActionRiskLevel.HIGH,
            requires_approval=False,
            supporting_evidence_ids=["ev-log"],
        )


def test_recommended_action_rejects_empty_evidence_ids():
    with pytest.raises(ValidationError, match="supporting_evidence_ids must not be empty"):
        RecommendedAction(
            action_type=ActionType.MANUAL_FOLLOW_UP,
            title="Ask owner",
            description="Need a human to confirm business impact.",
            risk_level=ActionRiskLevel.LOW,
            requires_approval=False,
            supporting_evidence_ids=[],
        )


def test_verification_suggestion_defaults_to_pending():
    suggestion = VerificationSuggestion(
        title="Check 5xx recovery",
        description="Confirm error rate returned to normal.",
        expected_signal="5xx rate below 1%",
    )

    assert suggestion.id.startswith("ver-")
    assert suggestion.status == VerificationStatus.PENDING
    assert suggestion.result_note is None
```

- [ ] **Step 2: Extend domain model tests for evidence and investigation status**

Append to `tests/domain/test_domain_models.py`:

```python
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.evidence import EvidenceStatus


def test_evidence_item_defaults_to_success_status():
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp="2026-07-03T15:10:00+08:00",
        summary="error spike",
    )

    assert item.status == EvidenceStatus.SUCCESS
    assert item.error_message is None


def test_provider_error_evidence_can_store_error_message():
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.PROVIDER_ERROR,
        status=EvidenceStatus.FAILED,
        timestamp="2026-07-03T15:10:00+08:00",
        summary="log provider failed",
        error_message="timeout",
    )

    assert item.status == EvidenceStatus.FAILED
    assert item.error_message == "timeout"


def test_investigation_record_defaults_to_pending():
    event = IncidentEvent(
        source="webhook",
        service="checkout-service",
        environment="prod",
        severity="warning",
        title="Latency increased",
        description="checkout-service latency increased",
        started_at="2026-07-03T15:10:00+08:00",
    )
    record = InvestigationRecord(event=event)

    assert record.status == InvestigationStatus.PENDING
    assert record.failure_reason is None
    assert record.actions == []
    assert record.verification_suggestions == []
```

- [ ] **Step 3: Run failing domain tests**

Run:

```bash
uv run pytest tests/domain/test_action_models.py tests/domain/test_domain_models.py -v
```

Expected: FAIL because `backend.domain.actions`, `EvidenceStatus`, `EvidenceKind.PROVIDER_ERROR`, new `InvestigationStatus` values, and `InvestigationRecord` defaults do not exist yet.

- [ ] **Step 4: Add action and verification domain models**

Create `backend/domain/actions.py`:

```python
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


class ActionType(StrEnum):
    CHECK = "check"
    NOTIFY_OWNER = "notify_owner"
    ROLLBACK_SUGGESTION = "rollback_suggestion"
    SCALE_SUGGESTION = "scale_suggestion"
    RESTART_SUGGESTION = "restart_suggestion"
    CONFIG_CHECK = "config_check"
    DEPENDENCY_CHECK = "dependency_check"
    MANUAL_FOLLOW_UP = "manual_follow_up"


class ActionRiskLevel(StrEnum):
    READ_ONLY = "read_only"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"
    DONE = "done"


class VerificationStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class RecommendedAction(BaseModel):
    id: str = Field(default_factory=lambda: f"act-{uuid4().hex}")
    action_type: ActionType
    title: str
    description: str
    risk_level: ActionRiskLevel
    requires_approval: bool
    supporting_evidence_ids: list[str]
    status: ActionStatus = ActionStatus.PROPOSED
    note: str | None = None

    @model_validator(mode="after")
    def validate_action_contract(self) -> "RecommendedAction":
        if not self.supporting_evidence_ids:
            raise ValueError("supporting_evidence_ids must not be empty")
        if self.risk_level in {ActionRiskLevel.MEDIUM, ActionRiskLevel.HIGH} and not self.requires_approval:
            raise ValueError("medium and high risk actions require approval")
        return self


class VerificationSuggestion(BaseModel):
    id: str = Field(default_factory=lambda: f"ver-{uuid4().hex}")
    title: str
    description: str
    expected_signal: str
    status: VerificationStatus = VerificationStatus.PENDING
    result_note: str | None = None
```

- [ ] **Step 5: Extend evidence model**

Modify `backend/domain/evidence.py`:

```python
class EvidenceProvider(StrEnum):
    LOG = "log"
    METRIC = "metric"
    DEPLOY = "deploy"
    DEPENDENCY = "dependency"
    SERVICE_CATALOG = "service_catalog"
    RELATED_ALERT = "related_alert"


class EvidenceKind(StrEnum):
    LOG_PATTERN = "log_pattern"
    METRIC_TREND = "metric_trend"
    DEPLOYMENT = "deployment"
    DEPENDENCY_HEALTH = "dependency_health"
    SERVICE_METADATA = "service_metadata"
    RELATED_ALERT = "related_alert"
    PROVIDER_ERROR = "provider_error"


class EvidenceStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
```

Then add fields to `EvidenceItem`:

```python
    status: EvidenceStatus = EvidenceStatus.SUCCESS
    error_message: str | None = None
```

- [ ] **Step 6: Extend report and investigation models**

Modify `backend/domain/reports.py`:

```python
from backend.domain.actions import RecommendedAction, VerificationSuggestion
```

Add fields to `IncidentReport`:

```python
    action_ids: list[str] = Field(default_factory=list)
    verification_suggestion_ids: list[str] = Field(default_factory=list)
```

Modify `backend/db/models.py`:

```python
from backend.domain.actions import RecommendedAction, VerificationSuggestion
```

Replace `InvestigationStatus` with:

```python
class InvestigationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

Update `InvestigationRecord` fields:

```python
    status: InvestigationStatus = InvestigationStatus.PENDING
    failure_reason: str | None = None
    actions: list[RecommendedAction] = Field(default_factory=list)
    verification_suggestions: list[VerificationSuggestion] = Field(default_factory=list)
    completed_at: datetime | None = None
```

- [ ] **Step 7: Run domain tests**

Run:

```bash
uv run pytest tests/domain/test_action_models.py tests/domain/test_domain_models.py -v
```

Expected: PASS.

- [ ] **Step 8: Run all tests to catch compatibility failures**

Run:

```bash
uv run pytest -v
```

Expected: existing API/orchestrator tests may fail because status behavior and report/action fields are not wired yet. Record failures for later tasks; do not weaken assertions.

- [ ] **Step 9: Commit**

```bash
git add backend/domain/actions.py backend/domain/evidence.py backend/domain/reports.py backend/db/models.py tests/domain/test_action_models.py tests/domain/test_domain_models.py
git commit -m "feat: add v2 domain contracts"
```

---

### Task 2: Repository State Transitions And Child Status Updates

**Files:**
- Modify: `backend/db/repositories.py`
- Test: `tests/diagnosis/test_orchestrator.py`
- Test: `tests/api/test_investigations_api.py`

- [ ] **Step 1: Write repository behavior tests**

Append to `tests/diagnosis/test_orchestrator.py`:

```python
from backend.domain.actions import ActionRiskLevel, ActionType, RecommendedAction, VerificationSuggestion


def test_repository_updates_status_and_failure_reason():
    repository = InMemoryInvestigationRepository()
    event = load_incident_case("deployment_regression")
    record = repository.save(InvestigationRecord(event=event))

    updated = repository.update_status(
        record.id,
        InvestigationStatus.FAILED,
        failure_reason="report generation failed",
    )

    assert updated.status == InvestigationStatus.FAILED
    assert updated.failure_reason == "report generation failed"
    assert updated.updated_at >= record.created_at


def test_repository_updates_action_and_verification_status():
    repository = InMemoryInvestigationRepository()
    event = load_incident_case("deployment_regression")
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Deployment regression likely.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=["ev-1"],
    )
    verification = VerificationSuggestion(
        title="Check 5xx",
        description="Confirm error rate recovery.",
        expected_signal="5xx below 1%",
    )
    record = repository.save(
        InvestigationRecord(
            event=event,
            actions=[action],
            verification_suggestions=[verification],
        )
    )

    updated_action = repository.update_action_status(
        record.id,
        action.id,
        status="approved",
        note="owner approved",
    )
    updated_verification = repository.update_verification_status(
        record.id,
        verification.id,
        status="passed",
        result_note="5xx is normal",
    )

    assert updated_action.status == "approved"
    assert updated_action.note == "owner approved"
    assert updated_verification.status == "passed"
    assert updated_verification.result_note == "5xx is normal"
```

- [ ] **Step 2: Run failing repository tests**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py::test_repository_updates_status_and_failure_reason tests/diagnosis/test_orchestrator.py::test_repository_updates_action_and_verification_status -v
```

Expected: FAIL because repository update methods do not exist.

- [ ] **Step 3: Implement repository update methods**

Modify `backend/db/repositories.py`:

```python
from datetime import UTC, datetime

from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.domain.actions import ActionStatus, VerificationStatus
```

Add methods:

```python
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
```

- [ ] **Step 4: Run repository tests**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py::test_repository_updates_status_and_failure_reason tests/diagnosis/test_orchestrator.py::test_repository_updates_action_and_verification_status -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/db/repositories.py tests/diagnosis/test_orchestrator.py
git commit -m "feat: add investigation repository state updates"
```

---

### Task 3: ProviderResult Semantics And Mock Provider Conversion

**Files:**
- Create: `backend/providers/results.py`
- Modify: `backend/providers/base.py`
- Modify: `backend/providers/registry.py`
- Modify: `backend/providers/mock_logs.py`
- Modify: `backend/providers/mock_metrics.py`
- Modify: `backend/providers/mock_deploys.py`
- Modify: `backend/providers/mock_dependencies.py`
- Create: `backend/providers/mock_service_catalog.py`
- Create: `backend/providers/mock_related_alerts.py`
- Test: `tests/providers/test_provider_results.py`
- Test: `tests/providers/test_mock_providers.py`

- [ ] **Step 1: Write ProviderResult tests**

Create `tests/providers/test_provider_results.py`:

```python
from backend.domain.evidence import EvidenceKind, EvidenceProvider, EvidenceStatus
from backend.providers.results import ProviderResult, ProviderStatus


def test_provider_result_defaults_to_success():
    result = ProviderResult(provider=EvidenceProvider.LOG)

    assert result.status == ProviderStatus.SUCCESS
    assert result.evidence_items == []
    assert result.duration_ms >= 0


def test_failed_provider_result_creates_error_evidence():
    result = ProviderResult(
        provider=EvidenceProvider.LOG,
        status=ProviderStatus.FAILED,
        error_message="loki timeout",
        duration_ms=25,
    )

    evidence = result.to_error_evidence()

    assert evidence.provider == EvidenceProvider.LOG
    assert evidence.kind == EvidenceKind.PROVIDER_ERROR
    assert evidence.status == EvidenceStatus.FAILED
    assert evidence.error_message == "loki timeout"
    assert evidence.payload["provider_status"] == "failed"
```

- [ ] **Step 2: Extend mock provider tests**

Modify `tests/providers/test_mock_providers.py` so provider registry returns evidence and provider results:

```python
def test_mock_registry_collects_provider_results():
    event = load_incident_case("deployment_regression")
    registry = build_mock_provider_registry()

    results = registry.collect_results(event)

    assert {result.provider for result in results} >= {
        EvidenceProvider.LOG,
        EvidenceProvider.METRIC,
        EvidenceProvider.DEPLOY,
        EvidenceProvider.DEPENDENCY,
        EvidenceProvider.SERVICE_CATALOG,
        EvidenceProvider.RELATED_ALERT,
    }
    assert all(result.status == "success" for result in results)


def test_registry_converts_provider_failure_to_error_evidence():
    class FailingProvider:
        provider = EvidenceProvider.LOG

        def collect(self, event):
            raise RuntimeError("provider exploded")

    event = load_incident_case("deployment_regression")
    registry = ProviderRegistry([FailingProvider()])

    evidence = registry.collect_all(event)

    assert len(evidence) == 1
    assert evidence[0].kind == EvidenceKind.PROVIDER_ERROR
    assert evidence[0].status == EvidenceStatus.FAILED
    assert evidence[0].error_message == "provider exploded"
```

- [ ] **Step 3: Run failing provider tests**

Run:

```bash
uv run pytest tests/providers/test_provider_results.py tests/providers/test_mock_providers.py -v
```

Expected: FAIL because ProviderResult and new provider registry methods do not exist.

- [ ] **Step 4: Add ProviderResult model**

Create `backend/providers/results.py`:

```python
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider, EvidenceStatus


class ProviderStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class ProviderResult(BaseModel):
    provider: EvidenceProvider
    status: ProviderStatus = ProviderStatus.SUCCESS
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    error_message: str | None = None
    duration_ms: int = 0

    def to_error_evidence(self) -> EvidenceItem:
        status = EvidenceStatus(self.status)
        return EvidenceItem(
            provider=self.provider,
            kind=EvidenceKind.PROVIDER_ERROR,
            status=status,
            timestamp=datetime.now(UTC),
            summary=f"{self.provider} provider {self.status}",
            payload={
                "provider": self.provider,
                "provider_status": self.status,
                "duration_ms": self.duration_ms,
            },
            confidence=1.0,
            error_message=self.error_message,
        )
```

- [ ] **Step 5: Update provider protocol**

Modify `backend/providers/base.py`:

```python
from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.providers.results import ProviderResult


class EvidenceProviderProtocol(Protocol):
    provider: object

    def collect(self, event: IncidentEvent) -> ProviderResult:
        """Collect evidence for an incident event."""
```

- [ ] **Step 6: Update provider registry**

Modify `backend/providers/registry.py`:

```python
from time import perf_counter

from backend.domain.evidence import EvidenceItem
from backend.providers.results import ProviderResult, ProviderStatus
from backend.providers.mock_service_catalog import MockServiceCatalogProvider
from backend.providers.mock_related_alerts import MockRelatedAlertProvider
```

Update methods:

```python
    def collect_results(self, event: IncidentEvent) -> list[ProviderResult]:
        results: list[ProviderResult] = []
        for provider in self.providers:
            started = perf_counter()
            try:
                result = provider.collect(event)
            except Exception as exc:
                provider_name = getattr(provider, "provider")
                result = ProviderResult(
                    provider=provider_name,
                    status=ProviderStatus.FAILED,
                    error_message=str(exc),
                    duration_ms=int((perf_counter() - started) * 1000),
                )
            results.append(result)
        return results

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for result in self.collect_results(event):
            evidence.extend(result.evidence_items)
            if result.status in {ProviderStatus.FAILED, ProviderStatus.PARTIAL}:
                evidence.append(result.to_error_evidence())
        return sorted(evidence, key=lambda item: item.timestamp)
```

Add new mock providers to `build_mock_provider_registry()`:

```python
            MockServiceCatalogProvider(),
            MockRelatedAlertProvider(),
```

- [ ] **Step 7: Convert existing mock providers**

For each existing mock provider:

1. Add class attribute `provider = EvidenceProvider.<VALUE>`.
2. Keep existing evidence construction.
3. Return `ProviderResult(provider=self.provider, evidence_items=evidence)`.

Example for `MockLogProvider.collect`:

```python
from backend.providers.results import ProviderResult


class MockLogProvider:
    provider = EvidenceProvider.LOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence = []
        # existing evidence construction stays here
        return ProviderResult(provider=self.provider, evidence_items=evidence)
```

- [ ] **Step 8: Add service catalog mock provider**

Create `backend/providers/mock_service_catalog.py`:

```python
from datetime import UTC, datetime

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult


class MockServiceCatalogProvider:
    provider = EvidenceProvider.SERVICE_CATALOG

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence = [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.SERVICE_METADATA,
                timestamp=event.started_at,
                summary=f"{event.service} owner is platform-team",
                payload={
                    "service": event.service,
                    "owner": "platform-team",
                    "runtime": "python",
                    "environment": event.environment,
                    "dependencies": ["inventory-service", "payment-db"],
                },
                confidence=1.0,
            )
        ]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
```

- [ ] **Step 9: Add related alert mock provider**

Create `backend/providers/mock_related_alerts.py`:

```python
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult


class MockRelatedAlertProvider:
    provider = EvidenceProvider.RELATED_ALERT

    def collect(self, event: IncidentEvent) -> ProviderResult:
        evidence = [
            EvidenceItem(
                provider=self.provider,
                kind=EvidenceKind.RELATED_ALERT,
                timestamp=event.started_at,
                summary=f"No wider alert storm detected around {event.service}",
                payload={
                    "service": event.service,
                    "related_alert_count": 0,
                    "time_window_minutes": event.time_window_minutes,
                },
                confidence=0.8,
            )
        ]
        return ProviderResult(provider=self.provider, evidence_items=evidence)
```

- [ ] **Step 10: Run provider tests**

Run:

```bash
uv run pytest tests/providers/test_provider_results.py tests/providers/test_mock_providers.py -v
```

Expected: PASS.

- [ ] **Step 11: Run analyzer and report tests**

Run:

```bash
uv run pytest tests/rca/test_analyzer.py tests/reports/test_generator.py -v
```

Expected: PASS or failures only where V2 report validation will be fixed in later tasks.

- [ ] **Step 12: Commit**

```bash
git add backend/providers tests/providers backend/domain/evidence.py
git commit -m "feat: add provider result semantics"
```

---

### Task 4: Lightweight Coordinator And Specialist Results

**Files:**
- Create: `backend/diagnosis/context.py`
- Create: `backend/diagnosis/coordinator.py`
- Modify: `backend/diagnosis/__init__.py`
- Test: `tests/diagnosis/test_coordinator.py`

- [ ] **Step 1: Write coordinator tests**

Create `tests/diagnosis/test_coordinator.py`:

```python
from backend.diagnosis.context import SpecialistStatus
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.providers.registry import build_mock_provider_registry
from backend.services.incident_cases import load_incident_case


def test_coordinator_collects_specialist_results_and_evidence():
    event = load_incident_case("deployment_regression")
    coordinator = DiagnosisCoordinator(build_mock_provider_registry())

    result = coordinator.collect(event)

    assert result.event == event
    assert result.evidence
    assert result.provider_results
    assert result.specialist_results
    assert all(item.status == SpecialistStatus.COMPLETED for item in result.specialist_results)
    assert {item.agent_name for item in result.specialist_results} >= {
        "LogAnalyst",
        "MetricAnalyst",
        "DeployAnalyst",
        "DependencyAnalyst",
        "ServiceCatalogAnalyst",
        "RelatedAlertAnalyst",
    }


def test_coordinator_records_failed_provider_as_failed_specialist():
    class FailingProvider:
        provider = "log"

        def collect(self, event):
            raise RuntimeError("provider exploded")

    event = load_incident_case("deployment_regression")
    coordinator = DiagnosisCoordinator(build_mock_provider_registry())
    coordinator.providers.providers = [FailingProvider()]

    result = coordinator.collect(event)

    assert result.evidence[0].kind == "provider_error"
    assert result.specialist_results[0].status == SpecialistStatus.FAILED
    assert result.specialist_results[0].errors == ["provider exploded"]
```

- [ ] **Step 2: Run failing coordinator tests**

Run:

```bash
uv run pytest tests/diagnosis/test_coordinator.py -v
```

Expected: FAIL because coordinator and context models do not exist.

- [ ] **Step 3: Add diagnosis context models**

Create `backend/diagnosis/context.py`:

```python
from enum import StrEnum

from pydantic import BaseModel, Field

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.providers.results import ProviderResult, ProviderStatus


class SpecialistStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"


class SpecialistResult(BaseModel):
    agent_name: str
    status: SpecialistStatus
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    summary: str
    errors: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class DiagnosisContext(BaseModel):
    event: IncidentEvent
    evidence: list[EvidenceItem] = Field(default_factory=list)
    provider_results: list[ProviderResult] = Field(default_factory=list)
    specialist_results: list[SpecialistResult] = Field(default_factory=list)


def provider_status_to_specialist_status(status: ProviderStatus) -> SpecialistStatus:
    if status == ProviderStatus.SUCCESS:
        return SpecialistStatus.COMPLETED
    if status == ProviderStatus.PARTIAL:
        return SpecialistStatus.PARTIAL
    if status == ProviderStatus.SKIPPED:
        return SpecialistStatus.SKIPPED
    return SpecialistStatus.FAILED
```

- [ ] **Step 4: Add coordinator**

Create `backend/diagnosis/coordinator.py`:

```python
from backend.diagnosis.context import (
    DiagnosisContext,
    SpecialistResult,
    provider_status_to_specialist_status,
)
from backend.domain.events import IncidentEvent
from backend.providers.registry import ProviderRegistry


AGENT_NAMES_BY_PROVIDER = {
    "log": "LogAnalyst",
    "metric": "MetricAnalyst",
    "deploy": "DeployAnalyst",
    "dependency": "DependencyAnalyst",
    "service_catalog": "ServiceCatalogAnalyst",
    "related_alert": "RelatedAlertAnalyst",
}


class DiagnosisCoordinator:
    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def collect(self, event: IncidentEvent) -> DiagnosisContext:
        provider_results = self.providers.collect_results(event)
        evidence = self.providers.evidence_from_results(provider_results)
        specialist_results = [
            SpecialistResult(
                agent_name=AGENT_NAMES_BY_PROVIDER.get(str(result.provider), f"{result.provider}Analyst"),
                status=provider_status_to_specialist_status(result.status),
                evidence_items=result.evidence_items,
                summary=f"{result.provider} provider returned {len(result.evidence_items)} evidence item(s)",
                errors=[result.error_message] if result.error_message else [],
                duration_ms=result.duration_ms,
            )
            for result in provider_results
        ]

        return DiagnosisContext(
            event=event,
            evidence=evidence,
            provider_results=provider_results,
            specialist_results=specialist_results,
        )
```

- [ ] **Step 5: Add helper in provider registry**

Modify `backend/providers/registry.py` to avoid duplicate aggregation logic:

```python
    def evidence_from_results(self, results: list[ProviderResult]) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for result in results:
            evidence.extend(result.evidence_items)
            if result.status in {ProviderStatus.FAILED, ProviderStatus.PARTIAL}:
                evidence.append(result.to_error_evidence())
        return sorted(evidence, key=lambda item: item.timestamp)

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        return self.evidence_from_results(self.collect_results(event))
```

- [ ] **Step 6: Run coordinator tests**

Run:

```bash
uv run pytest tests/diagnosis/test_coordinator.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/diagnosis/context.py backend/diagnosis/coordinator.py backend/providers/registry.py tests/diagnosis/test_coordinator.py
git commit -m "feat: add lightweight diagnosis coordinator"
```

---

### Task 5: Action Planner And Verification Suggestions

**Files:**
- Create: `backend/diagnosis/action_planner.py`
- Test: `tests/diagnosis/test_action_planner.py`

- [ ] **Step 1: Write action planner tests**

Create `tests/diagnosis/test_action_planner.py`:

```python
from backend.diagnosis.action_planner import ActionPlanner
from backend.domain.actions import ActionRiskLevel, ActionType
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.services.incident_cases import load_incident_case


def plan_for_case(case_id: str):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)
    return ActionPlanner().plan(event, evidence, hypotheses)


def test_deployment_regression_generates_high_risk_rollback_suggestion():
    actions, verifications = plan_for_case("deployment_regression")

    top_action = actions[0]
    assert top_action.action_type == ActionType.ROLLBACK_SUGGESTION
    assert top_action.risk_level == ActionRiskLevel.HIGH
    assert top_action.requires_approval is True
    assert top_action.supporting_evidence_ids
    assert any("5xx" in item.expected_signal for item in verifications)


def test_traffic_spike_generates_scale_suggestion():
    actions, verifications = plan_for_case("traffic_spike")

    assert actions[0].action_type == ActionType.SCALE_SUGGESTION
    assert actions[0].risk_level == ActionRiskLevel.MEDIUM
    assert actions[0].requires_approval is True
    assert any("QPS" in item.expected_signal for item in verifications)


def test_unknown_generates_manual_follow_up_action():
    event = load_incident_case("deployment_regression")
    actions, verifications = ActionPlanner().plan(
        event,
        evidence=[],
        hypotheses=[
            Hypothesis(
                cause_type=CauseType.UNKNOWN,
                summary="No evidence available.",
                confidence=0.2,
                supporting_evidence_ids=[],
                contradicting_evidence_ids=[],
                next_actions=["Collect logs, metrics, deploy records, and dependency status."],
            ),
        ],
    )

    assert actions[0].action_type == ActionType.MANUAL_FOLLOW_UP
    assert actions[0].risk_level == ActionRiskLevel.LOW
    assert actions[0].requires_approval is False
    assert verifications[0].expected_signal == "new evidence is collected"
```

- [ ] **Step 2: Run failing action planner tests**

Run:

```bash
uv run pytest tests/diagnosis/test_action_planner.py -v
```

Expected: FAIL because `ActionPlanner` does not exist.

- [ ] **Step 3: Implement action planner**

Create `backend/diagnosis/action_planner.py`:

```python
from backend.domain.actions import (
    ActionRiskLevel,
    ActionType,
    RecommendedAction,
    VerificationSuggestion,
)
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import CauseType, Hypothesis


class ActionPlanner:
    def plan(
        self,
        event: IncidentEvent,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
    ) -> tuple[list[RecommendedAction], list[VerificationSuggestion]]:
        if not hypotheses:
            return self._manual_follow_up("No hypotheses were generated")

        top = hypotheses[0]
        supporting_ids = top.supporting_evidence_ids or [item.id for item in evidence[:1]]
        if not supporting_ids:
            return self._manual_follow_up("No evidence was available")

        if top.cause_type == CauseType.DEPLOYMENT_REGRESSION:
            return [
                RecommendedAction(
                    action_type=ActionType.ROLLBACK_SUGGESTION,
                    title=f"Evaluate rollback for {event.service}",
                    description="Recent deployment evidence and new errors point to a possible regression. V2 does not execute rollback.",
                    risk_level=ActionRiskLevel.HIGH,
                    requires_approval=True,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications("5xx rate below 1%", "new exception group stops appearing")

        if top.cause_type == CauseType.TRAFFIC_SPIKE:
            return [
                RecommendedAction(
                    action_type=ActionType.SCALE_SUGGESTION,
                    title=f"Evaluate capacity response for {event.service}",
                    description="Traffic and latency evidence suggest overload. V2 only records this as a recommendation.",
                    risk_level=ActionRiskLevel.MEDIUM,
                    requires_approval=True,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications("QPS returns to expected range", "P95 latency recovers")

        if top.cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE:
            return [
                RecommendedAction(
                    action_type=ActionType.DEPENDENCY_CHECK,
                    title="Contact dependency owner",
                    description="Dependency health evidence points to a downstream issue.",
                    risk_level=ActionRiskLevel.LOW,
                    requires_approval=False,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications("dependency latency recovers", "local timeout errors decrease")

        if top.cause_type == CauseType.DATABASE_SLOWDOWN:
            return [
                RecommendedAction(
                    action_type=ActionType.CONFIG_CHECK,
                    title="Review database performance signals",
                    description="Database latency evidence suggests a database slowdown.",
                    risk_level=ActionRiskLevel.LOW,
                    requires_approval=False,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications("database P95 latency recovers", "application latency recovers")

        if top.cause_type == CauseType.SINGLE_INSTANCE_ISSUE:
            return [
                RecommendedAction(
                    action_type=ActionType.CHECK,
                    title="Inspect abnormal instance",
                    description="Instance-level evidence suggests one bad instance.",
                    risk_level=ActionRiskLevel.READ_ONLY,
                    requires_approval=False,
                    supporting_evidence_ids=supporting_ids,
                )
            ], self._default_verifications("bad instance error rate recovers", "load balancer stops routing errors")

        return self._manual_follow_up(top.summary, supporting_ids)

    def _manual_follow_up(
        self,
        reason: str,
        supporting_ids: list[str] | None = None,
    ) -> tuple[list[RecommendedAction], list[VerificationSuggestion]]:
        return [
            RecommendedAction(
                action_type=ActionType.MANUAL_FOLLOW_UP,
                title="Collect more evidence",
                description=reason,
                risk_level=ActionRiskLevel.LOW,
                requires_approval=False,
                supporting_evidence_ids=supporting_ids or ["ev-missing-evidence"],
            )
        ], [
            VerificationSuggestion(
                title="Collect additional evidence",
                description="Gather logs, metrics, deploy records, and dependency status before acting.",
                expected_signal="new evidence is collected",
            )
        ]

    def _default_verifications(self, *signals: str) -> list[VerificationSuggestion]:
        return [
            VerificationSuggestion(
                title=f"Verify {signal}",
                description=f"Confirm that {signal}.",
                expected_signal=signal,
            )
            for signal in signals
        ]
```

If the manual follow-up fallback uses `ev-missing-evidence`, add a synthetic missing evidence item in orchestrator before persisting actions. This is wired in Task 6.

- [ ] **Step 4: Run action planner tests**

Run:

```bash
uv run pytest tests/diagnosis/test_action_planner.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/diagnosis/action_planner.py tests/diagnosis/test_action_planner.py
git commit -m "feat: add v2 action planner"
```

---

### Task 6: Investigation Runner Semantics In Orchestrator

**Files:**
- Modify: `backend/diagnosis/orchestrator.py`
- Modify: `backend/services/container.py`
- Test: `tests/diagnosis/test_orchestrator.py`

- [ ] **Step 1: Write orchestrator state tests**

Append to `tests/diagnosis/test_orchestrator.py`:

```python
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.reports.generator import ReportGenerator


def build_v2_orchestrator(repository=None, providers=None, analyzer=None, report_generator=None):
    repository = repository or InMemoryInvestigationRepository()
    providers = providers or build_mock_provider_registry()
    return DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=analyzer or RcaAnalyzer(),
        report_generator=report_generator or ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
        action_planner=ActionPlanner(),
    )


def test_orchestrator_persists_completed_record_with_actions_and_verifications():
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert saved.status == InvestigationStatus.COMPLETED
    assert saved.evidence
    assert saved.hypotheses
    assert saved.report is not None
    assert saved.actions
    assert saved.verification_suggestions
    assert saved.completed_at is not None


def test_orchestrator_persists_failed_record_when_report_generation_fails():
    class BrokenReportGenerator:
        def generate(self, *args, **kwargs):
            raise RuntimeError("markdown exploded")

    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(
        repository=repository,
        report_generator=BrokenReportGenerator(),
    )
    event = load_incident_case("deployment_regression")

    record = orchestrator.run(event)
    saved = repository.get(record.id)

    assert record.status == InvestigationStatus.FAILED
    assert saved.status == InvestigationStatus.FAILED
    assert saved.failure_reason == "markdown exploded"
    assert saved.evidence
    assert saved.hypotheses
```

- [ ] **Step 2: Run failing orchestrator tests**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py -v
```

Expected: FAIL because orchestrator constructor and state semantics are not updated yet.

- [ ] **Step 3: Update orchestrator constructor**

Modify `backend/diagnosis/orchestrator.py` imports:

```python
from datetime import UTC, datetime

from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider, EvidenceStatus
```

Update `__init__`:

```python
        coordinator: DiagnosisCoordinator | None = None,
        action_planner: ActionPlanner | None = None,
```

Inside constructor:

```python
        self.coordinator = coordinator or DiagnosisCoordinator(providers)
        self.action_planner = action_planner or ActionPlanner()
```

- [ ] **Step 4: Implement stateful run**

Replace `DiagnosisOrchestrator.run` with:

```python
    def run(self, event: IncidentEvent) -> InvestigationRecord:
        record = self.repository.save(
            InvestigationRecord(event=event, status=InvestigationStatus.PENDING)
        )
        self.repository.update_status(record.id, InvestigationStatus.RUNNING)

        try:
            context = self.coordinator.collect(event)
            evidence = context.evidence
            hypotheses = self.analyzer.analyze(event, evidence)
            actions, verifications = self.action_planner.plan(event, evidence, hypotheses)
            evidence = self._ensure_action_evidence(evidence, actions)

            record.evidence = evidence
            record.hypotheses = hypotheses
            record.actions = actions
            record.verification_suggestions = verifications
            report = self.report_generator.generate(
                record.id,
                event,
                evidence,
                hypotheses,
                actions=actions,
                verification_suggestions=verifications,
            )
            record.report = report
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            return self.repository.update_status(record.id, InvestigationStatus.COMPLETED)
        except Exception as exc:
            record.failure_reason = str(exc)
            record.updated_at = datetime.now(UTC)
            self.repository.save(record)
            return self.repository.update_status(
                record.id,
                InvestigationStatus.FAILED,
                failure_reason=str(exc),
            )
```

Add helper:

```python
    def _ensure_action_evidence(
        self,
        evidence: list[EvidenceItem],
        actions,
    ) -> list[EvidenceItem]:
        evidence_ids = {item.id for item in evidence}
        needs_missing = any(
            evidence_id not in evidence_ids
            for action in actions
            for evidence_id in action.supporting_evidence_ids
        )
        if not needs_missing:
            return evidence

        return [
            *evidence,
            EvidenceItem(
                id="ev-missing-evidence",
                provider=EvidenceProvider.SERVICE_CATALOG,
                kind=EvidenceKind.PROVIDER_ERROR,
                status=EvidenceStatus.FAILED,
                timestamp=datetime.now(UTC),
                summary="Additional evidence is required before recommending action.",
                payload={"reason": "action planner had no concrete evidence"},
                confidence=1.0,
                error_message="missing evidence",
            ),
        ]
```

- [ ] **Step 5: Update container wiring**

Modify `backend/services/container.py`:

```python
from backend.diagnosis.action_planner import ActionPlanner
from backend.diagnosis.coordinator import DiagnosisCoordinator
```

When constructing the orchestrator, pass:

```python
coordinator=DiagnosisCoordinator(providers),
action_planner=ActionPlanner(),
```

- [ ] **Step 6: Run orchestrator tests**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py -v
```

Expected: PASS after report generator accepts `actions` and `verification_suggestions` keyword arguments in Task 7. If it fails on the new report generator signature, proceed to Task 7 before committing Task 6. If possible, commit Tasks 6 and 7 separately after both pass.

- [ ] **Step 7: Commit if tests pass**

```bash
git add backend/diagnosis/orchestrator.py backend/services/container.py tests/diagnosis/test_orchestrator.py
git commit -m "feat: persist investigation lifecycle states"
```

---

### Task 7: V2 Report Generator Sections And Reference Validation

**Files:**
- Modify: `backend/reports/generator.py`
- Test: `tests/reports/test_generator.py`
- Test: `tests/golden/test_golden_cases.py`

- [ ] **Step 1: Write report validation tests**

Append to `tests/reports/test_generator.py`:

```python
from backend.domain.actions import ActionRiskLevel, ActionType, RecommendedAction, VerificationSuggestion


def test_report_generator_rejects_missing_action_evidence_id():
    event, evidence, hypotheses = build_report_inputs()
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Regression likely.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=["ev-not-found"],
    )

    with pytest.raises(ValueError, match="missing evidence id: ev-not-found"):
        ReportGenerator().generate(
            "inv-1",
            event,
            evidence,
            hypotheses,
            actions=[action],
            verification_suggestions=[],
        )


def test_report_generator_validates_all_hypotheses_evidence_ids():
    event, evidence, hypotheses = build_report_inputs()
    hypotheses[1].supporting_evidence_ids = ["ev-not-found"]

    with pytest.raises(ValueError, match="missing evidence id: ev-not-found"):
        ReportGenerator().generate("inv-1", event, evidence, hypotheses)


def test_report_generator_renders_v2_sections():
    event, evidence, hypotheses = build_report_inputs()
    action = RecommendedAction(
        action_type=ActionType.ROLLBACK_SUGGESTION,
        title="Evaluate rollback",
        description="Regression likely.",
        risk_level=ActionRiskLevel.HIGH,
        requires_approval=True,
        supporting_evidence_ids=[hypotheses[0].supporting_evidence_ids[0]],
    )
    verification = VerificationSuggestion(
        title="Check 5xx recovery",
        description="Confirm errors recover.",
        expected_signal="5xx rate below 1%",
    )

    report = ReportGenerator().generate(
        "inv-1",
        event,
        evidence,
        hypotheses,
        actions=[action],
        verification_suggestions=[verification],
    )

    assert "## 建议动作" in report.markdown
    assert "## 需要审批的动作" in report.markdown
    assert "## 验证建议" in report.markdown
    assert "V2 未执行该动作" in report.markdown
    assert action.id in report.action_ids
    assert verification.id in report.verification_suggestion_ids
```

- [ ] **Step 2: Extend golden tests**

In `tests/golden/test_golden_cases.py`, after report generation, assert:

```python
    assert report.action_ids
    assert report.verification_suggestion_ids
    assert "## 建议动作" in report.markdown
    assert "## 验证建议" in report.markdown
```

If current golden tests create the report without actions, update them to call `ActionPlanner().plan(event, evidence, hypotheses)` and pass actions/verifications to the report generator.

- [ ] **Step 3: Run failing report tests**

Run:

```bash
uv run pytest tests/reports/test_generator.py tests/golden/test_golden_cases.py -v
```

Expected: FAIL because report generator signature and V2 sections are not implemented.

- [ ] **Step 4: Update report generator signature**

Modify `backend/reports/generator.py` imports:

```python
from backend.domain.actions import RecommendedAction, VerificationSuggestion
```

Update `generate` signature:

```python
        actions: list[RecommendedAction] | None = None,
        verification_suggestions: list[VerificationSuggestion] | None = None,
```

Inside `generate`:

```python
        actions = actions or []
        verification_suggestions = verification_suggestions or []
        self._validate_evidence_ids(sorted_evidence, hypotheses, actions)
```

Return:

```python
            action_ids=[action.id for action in actions],
            verification_suggestion_ids=[item.id for item in verification_suggestions],
```

- [ ] **Step 5: Validate all evidence references**

Replace `_validate_top_evidence_ids` with:

```python
    def _validate_evidence_ids(
        self,
        evidence: list[EvidenceItem],
        hypotheses: list[Hypothesis],
        actions: list[RecommendedAction],
    ) -> None:
        evidence_ids = {item.id for item in evidence}
        referenced_ids: list[str] = []
        for hypothesis in hypotheses:
            referenced_ids.extend(hypothesis.supporting_evidence_ids)
            referenced_ids.extend(hypothesis.contradicting_evidence_ids)
        for action in actions:
            referenced_ids.extend(action.supporting_evidence_ids)

        for evidence_id in referenced_ids:
            if evidence_id not in evidence_ids:
                raise ValueError(f"missing evidence id: {evidence_id}")
```

- [ ] **Step 6: Render V2 sections**

Update `_render_markdown` signature to accept actions and verification suggestions. Add sections after existing recommended action output:

```python
        lines.extend(["", "## 建议动作", ""])
        if actions:
            for action in actions:
                approval_text = "需要人工审批" if action.requires_approval else "不需要审批"
                lines.append(f"- `{action.action_type}` {action.title}")
                lines.append(f"  - 风险等级：`{action.risk_level}`")
                lines.append(f"  - 审批要求：{approval_text}")
                lines.append(f"  - 状态：`{action.status}`")
                lines.append(f"  - 说明：{action.description}")
                lines.append(f"  - 证据：{', '.join(action.supporting_evidence_ids)}")
                lines.append("  - 执行状态：V2 未执行该动作")
        else:
            lines.append("- 建议动作生成失败或暂无建议动作")

        approval_actions = [action for action in actions if action.requires_approval]
        lines.extend(["", "## 需要审批的动作", ""])
        if approval_actions:
            for action in approval_actions:
                lines.append(f"- `{action.id}` {action.title} ({action.risk_level})")
        else:
            lines.append("- 当前没有需要审批的动作")

        lines.extend(["", "## 验证建议", ""])
        if verification_suggestions:
            for suggestion in verification_suggestions:
                lines.append(f"- `{suggestion.status}` {suggestion.title}: {suggestion.expected_signal}")
        else:
            lines.append("- 当前没有验证建议")
```

Do not claim any action was executed.

- [ ] **Step 7: Run report and golden tests**

Run:

```bash
uv run pytest tests/reports/test_generator.py tests/golden/test_golden_cases.py -v
```

Expected: PASS.

- [ ] **Step 8: Run orchestrator tests from Task 6**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py -v
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add backend/reports/generator.py tests/reports/test_generator.py tests/golden/test_golden_cases.py
git commit -m "feat: render v2 report loop sections"
```

---

### Task 8: Investigation API For Actions, Verifications, And Manual Input

**Files:**
- Modify: `backend/api/events.py`
- Modify: `backend/api/investigations.py`
- Test: `tests/api/test_events_api.py`
- Test: `tests/api/test_investigations_api.py`

- [ ] **Step 1: Write API tests for V2 summaries and manual input**

Append to `tests/api/test_events_api.py`:

```python
def test_create_event_summary_includes_v2_counts():
    client = TestClient(app)

    response = client.post(
        "/events",
        json={
            "source": "webhook",
            "service": "checkout-service",
            "environment": "prod",
            "severity": "warning",
            "title": "Latency increased",
            "description": "checkout-service latency increased during QPS spike",
            "started_at": "2026-07-03T15:10:00+08:00",
            "time_window_minutes": 30,
            "signals": {"qps": "high", "latency": "high"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["action_count"] >= 1
    assert body["verification_count"] >= 1


def test_manual_investigation_requires_service_and_environment():
    client = TestClient(app)

    response = client.post(
        "/investigations/manual",
        json={"text": "checkout-service has many 500s"},
    )

    assert response.status_code == 422


def test_manual_investigation_creates_completed_record():
    client = TestClient(app)

    response = client.post(
        "/investigations/manual",
        json={
            "text": "checkout-service has many 500s after 14:00",
            "service": "checkout-service",
            "environment": "prod",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "checkout-service"
    assert body["status"] == "completed"
```

- [ ] **Step 2: Write API tests for action and verification status updates**

Append to `tests/api/test_investigations_api.py`:

```python
def test_update_action_status_only_changes_state():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    detail = client.get(f"/investigations/{created['id']}").json()
    action_id = detail["actions"][0]["id"]

    response = client.patch(
        f"/investigations/{created['id']}/actions/{action_id}",
        json={"status": "approved", "note": "owner approved"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["note"] == "owner approved"


def test_update_unknown_action_returns_404():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()

    response = client.patch(
        f"/investigations/{created['id']}/actions/act-not-found",
        json={"status": "approved"},
    )

    assert response.status_code == 404


def test_update_verification_status_records_result_note():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()
    detail = client.get(f"/investigations/{created['id']}").json()
    verification_id = detail["verification_suggestions"][0]["id"]

    response = client.patch(
        f"/investigations/{created['id']}/verifications/{verification_id}",
        json={"status": "passed", "result_note": "5xx recovered"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "passed"
    assert body["result_note"] == "5xx recovered"
```

- [ ] **Step 3: Run failing API tests**

Run:

```bash
uv run pytest tests/api/test_events_api.py tests/api/test_investigations_api.py -v
```

Expected: FAIL because V2 summary fields, manual route, and patch routes do not exist.

- [ ] **Step 4: Extend event summary**

Modify `backend/api/events.py` `InvestigationSummary`:

```python
    action_count: int = 0
    verification_count: int = 0
    failure_reason: str | None = None
```

Update `to_summary` to handle failed records without hypotheses:

```python
    top = record.hypotheses[0] if record.hypotheses else None
    return InvestigationSummary(
        id=record.id,
        status=record.status,
        service=record.event.service,
        title=record.event.title,
        top_cause_type=top.cause_type if top else "unknown",
        confidence=top.confidence if top else 0.0,
        action_count=len(record.actions),
        verification_count=len(record.verification_suggestions),
        failure_reason=record.failure_reason,
    )
```

- [ ] **Step 5: Add manual route request model and route**

In `backend/api/investigations.py`, add:

```python
from datetime import UTC, datetime
from pydantic import BaseModel

from backend.api.events import InvestigationSummary, to_summary
from backend.domain.events import IncidentEvent, IncidentSource, Severity
```

Add request model:

```python
class ManualInvestigationRequest(BaseModel):
    text: str
    service: str
    environment: str
```

Add route before `/{investigation_id}`:

```python
@router.post("/manual", response_model=InvestigationSummary)
def create_manual_investigation(request: ManualInvestigationRequest) -> InvestigationSummary:
    event = IncidentEvent(
        source=IncidentSource.MANUAL,
        service=request.service,
        environment=request.environment,
        severity=Severity.WARNING,
        title=request.text[:80],
        description=request.text,
        started_at=datetime.now(UTC),
    )
    container = get_container()
    record = container.orchestrator.run(event)
    return to_summary(record)
```

- [ ] **Step 6: Add patch request models and routes**

In `backend/api/investigations.py`, add:

```python
from backend.domain.actions import ActionStatus, VerificationStatus
```

Request models:

```python
class UpdateActionStatusRequest(BaseModel):
    status: ActionStatus
    note: str | None = None


class UpdateVerificationStatusRequest(BaseModel):
    status: VerificationStatus
    result_note: str | None = None
```

Routes:

```python
@router.patch("/{investigation_id}/actions/{action_id}")
def update_action_status(
    investigation_id: str,
    action_id: str,
    request: UpdateActionStatusRequest,
):
    container = get_container()
    try:
        return container.repository.update_action_status(
            investigation_id,
            action_id,
            status=request.status,
            note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{investigation_id}/verifications/{verification_id}")
def update_verification_status(
    investigation_id: str,
    verification_id: str,
    request: UpdateVerificationStatusRequest,
):
    container = get_container()
    try:
        return container.repository.update_verification_status(
            investigation_id,
            verification_id,
            status=request.status,
            result_note=request.result_note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
```

- [ ] **Step 7: Run API tests**

Run:

```bash
uv run pytest tests/api/test_events_api.py tests/api/test_investigations_api.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add backend/api/events.py backend/api/investigations.py tests/api/test_events_api.py tests/api/test_investigations_api.py
git commit -m "feat: add v2 investigation action api"
```

---

### Task 9: Platform Observability Logging

**Files:**
- Create or modify: `backend/utils/logger.py` if no logging helper exists
- Modify: `backend/diagnosis/orchestrator.py`
- Modify: `backend/providers/registry.py`
- Test: `tests/diagnosis/test_orchestrator.py`
- Test: `tests/providers/test_mock_providers.py`

- [ ] **Step 1: Write logging behavior tests with caplog**

Append to `tests/diagnosis/test_orchestrator.py`:

```python
import logging


def test_orchestrator_logs_investigation_lifecycle(caplog):
    repository = InMemoryInvestigationRepository()
    orchestrator = build_v2_orchestrator(repository=repository)
    event = load_incident_case("deployment_regression")

    with caplog.at_level(logging.INFO):
        record = orchestrator.run(event)

    messages = "\n".join(item.message for item in caplog.records)
    assert record.id in messages
    assert "investigation started" in messages
    assert "investigation completed" in messages
```

Append to `tests/providers/test_mock_providers.py`:

```python
import logging


def test_provider_registry_logs_provider_status(caplog):
    event = load_incident_case("deployment_regression")
    registry = build_mock_provider_registry()

    with caplog.at_level(logging.INFO):
        registry.collect_results(event)

    messages = "\n".join(item.message for item in caplog.records)
    assert "provider completed" in messages
    assert "duration_ms" in messages
```

- [ ] **Step 2: Run failing logging tests**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py::test_orchestrator_logs_investigation_lifecycle tests/providers/test_mock_providers.py::test_provider_registry_logs_provider_status -v
```

Expected: FAIL because logging is not implemented.

- [ ] **Step 3: Add logging**

Use standard library logging. At top of `backend/diagnosis/orchestrator.py`:

```python
import logging

logger = logging.getLogger(__name__)
```

Add logs:

```python
        logger.info("investigation started id=%s service=%s", record.id, event.service)
```

After completed:

```python
            logger.info(
                "investigation completed id=%s evidence_count=%s action_count=%s verification_count=%s",
                record.id,
                len(record.evidence),
                len(record.actions),
                len(record.verification_suggestions),
            )
```

On failure:

```python
            logger.exception("investigation failed id=%s reason=%s", record.id, exc)
```

In `backend/providers/registry.py`:

```python
import logging

logger = logging.getLogger(__name__)
```

After each provider result:

```python
            logger.info(
                "provider completed provider=%s status=%s duration_ms=%s evidence_count=%s",
                result.provider,
                result.status,
                result.duration_ms,
                len(result.evidence_items),
            )
```

- [ ] **Step 4: Run logging tests**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py::test_orchestrator_logs_investigation_lifecycle tests/providers/test_mock_providers.py::test_provider_registry_logs_provider_status -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/diagnosis/orchestrator.py backend/providers/registry.py tests/diagnosis/test_orchestrator.py tests/providers/test_mock_providers.py
git commit -m "feat: add platform loop observability logs"
```

---

### Task 10: V2 Golden Cases, Docs, And Example Compatibility

**Files:**
- Modify: `tests/golden/test_golden_cases.py`
- Modify: `README.md`
- Modify: `docs/examples/webhook-event.json` only if schema changes require it

- [ ] **Step 1: Strengthen golden tests for V2 contracts**

In `tests/golden/test_golden_cases.py`, add assertions for each case:

```python
    assert record.status == "completed"
    assert record.actions
    assert record.verification_suggestions
    assert all(action.supporting_evidence_ids for action in record.actions)
    evidence_ids = {item.id for item in record.evidence}
    for action in record.actions:
        assert set(action.supporting_evidence_ids) <= evidence_ids
        if action.risk_level in {"medium", "high"}:
            assert action.requires_approval is True
    assert "V2 未执行该动作" in record.report.markdown
```

If golden tests currently use report generator directly, switch them to run through `DiagnosisOrchestrator` so V2 actions and verifications are included.

- [ ] **Step 2: Run failing/passing golden tests**

Run:

```bash
uv run pytest tests/golden/test_golden_cases.py -v
```

Expected: PASS after Tasks 1-9 are complete.

- [ ] **Step 3: Update README with V2 endpoints**

Add sections to `README.md`:

```markdown
## V2 Platform Loop

DiagOps V2 keeps production systems read-only. It creates an investigation, collects evidence, ranks RCA hypotheses, generates recommended actions, records approval status, and provides verification suggestions.

## Manual Investigation

```bash
curl -X POST http://127.0.0.1:8000/investigations/manual \
  -H "Content-Type: application/json" \
  -d '{"text":"checkout-service has many 500s after 14:00","service":"checkout-service","environment":"prod"}'
```

## Update A Recommended Action

```bash
curl -X PATCH http://127.0.0.1:8000/investigations/<investigation_id>/actions/<action_id> \
  -H "Content-Type: application/json" \
  -d '{"status":"approved","note":"owner approved"}'
```

Approval only changes state in V2. It does not execute rollback, restart, scaling, or configuration changes.

## Record A Verification Result

```bash
curl -X PATCH http://127.0.0.1:8000/investigations/<investigation_id>/verifications/<verification_id> \
  -H "Content-Type: application/json" \
  -d '{"status":"passed","result_note":"5xx rate recovered"}'
```
```

- [ ] **Step 4: Run README-related smoke tests**

Run:

```bash
uv run pytest tests/api/test_events_api.py tests/api/test_investigations_api.py tests/golden/test_golden_cases.py -v
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/golden/test_golden_cases.py README.md docs/examples/webhook-event.json
git commit -m "docs: document v2 platform loop api"
```

---

### Task 11: Final Verification And Push

**Files:**
- No new files.

- [ ] **Step 1: Run ruff**

Run:

```bash
uv run ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 2: Run full test suite**

Run:

```bash
uv run pytest -v
```

Expected:

```text
all tests pass
```

The known Starlette/TestClient deprecation warning is acceptable if tests pass.

- [ ] **Step 3: Start local API**

Run:

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Expected:

```text
Uvicorn running on http://127.0.0.1:8000
```

- [ ] **Step 4: Manual API verification in another terminal**

Run:

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
curl http://127.0.0.1:8000/investigations
```

Expected:

1. Health returns `{"status":"ok"}`.
2. Simulated event returns `status=completed`, `top_cause_type=deployment_regression`, `action_count>=1`, and `verification_count>=1`.
3. Investigation list contains the created investigation.

- [ ] **Step 5: Verify action update manually**

Get an action id from the investigation detail, then run:

```bash
curl -X PATCH http://127.0.0.1:8000/investigations/<investigation_id>/actions/<action_id> \
  -H "Content-Type: application/json" \
  -d '{"status":"approved","note":"manual smoke test"}'
```

Expected:

```text
status is approved and no remediation command is executed
```

- [ ] **Step 6: Verify verification update manually**

Get a verification id from the investigation detail, then run:

```bash
curl -X PATCH http://127.0.0.1:8000/investigations/<investigation_id>/verifications/<verification_id> \
  -H "Content-Type: application/json" \
  -d '{"status":"passed","result_note":"manual smoke test passed"}'
```

Expected:

```text
status is passed and result_note is saved
```

- [ ] **Step 7: Check git status**

Run:

```bash
git status -sb
```

Expected:

```text
working tree is clean
```

- [ ] **Step 8: Push feature branch**

Run:

```bash
git push
```

Expected:

```text
codex/mvp-backend pushed to origin/codex/mvp-backend
```

---

## Self-Review Checklist

Spec coverage:

1. Investigation state and failed persistence: Tasks 1, 2, 6, 8.
2. Recommended actions and approval status: Tasks 1, 5, 7, 8, 10.
3. Verification suggestions: Tasks 1, 5, 7, 8, 10.
4. Provider result status and provider-error evidence: Task 3.
5. Lightweight coordinator and specialists: Task 4.
6. Report V2 sections and evidence validation: Task 7.
7. Manual input and API updates: Task 8.
8. Platform observability: Task 9.
9. Testing and golden cases: Tasks 1-11.
10. Documentation and examples: Task 10.

Plan constraints:

1. No SSH command execution.
2. No rollback/restart/scale/config mutation.
3. No UI implementation.
4. No full multi-agent SDK.
5. No persistence migration beyond the in-memory repository.

Execution rule:

Do not proceed to the next task if task-specific tests fail. Reviewer agents should reject changes that weaken evidence references, silently drop failed investigations, or imply that V2 executes recommended actions.
