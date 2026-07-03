# SRE RCA Agent MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建事件驱动的应用服务 RCA Agent 后端 MVP，让模拟事件或 Webhook 事件进入系统后，自动收集 mock 证据、生成根因假设、输出带证据链的诊断报告。

**Architecture:** 第一阶段采用项目自研 `DiagnosisOrchestrator`，不引入完整多 Agent 框架。后端以 FastAPI + Pydantic + SQLAlchemy + SQLite 为主，Provider 使用 mock 实现，RCA Analyzer 使用可测试的规则评分，Report Generator 使用模板生成 Markdown 报告。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic、SQLAlchemy 2.x、SQLite、pytest、uv；后续多 Agent 执行层优先 OpenAI Agents SDK，复杂状态流再考虑 LangGraph。

---

## 0. 实施边界

本计划只实现后端 MVP 闭环：

```text
事件输入 -> 调查会话 -> mock 证据采集 -> 规则 RCA -> Markdown 报告 -> API 查询
```

本计划不实现：

1. 前端 React 页面。
2. 真实 Prometheus / Loki / ELK 接入。
3. OpenAI Agents SDK 多 Agent 执行层。
4. 自动回滚、重启、扩容等生产操作。
5. 复杂权限系统。

前端和真实数据源在后续计划中实现。

## 1. 文件结构总览

执行完成后，仓库应包含：

```text
backend/
  __init__.py
  main.py
  api/
    __init__.py
    events.py
    health.py
    investigations.py
  db/
    __init__.py
    models.py
    repositories.py
    session.py
  diagnosis/
    __init__.py
    orchestrator.py
  domain/
    __init__.py
    events.py
    evidence.py
    hypotheses.py
    reports.py
  providers/
    __init__.py
    base.py
    mock_deploys.py
    mock_dependencies.py
    mock_logs.py
    mock_metrics.py
    registry.py
  rca/
    __init__.py
    analyzer.py
    scoring.py
  reports/
    __init__.py
    generator.py

data/
  incidents/
    deployment_regression.json
    traffic_spike.json
    dependency_timeout.json
    database_slowdown.json
    single_bad_instance.json

tests/
  api/
    test_events_api.py
    test_investigations_api.py
  diagnosis/
    test_orchestrator.py
  providers/
    test_mock_providers.py
  rca/
    test_analyzer.py
  reports/
    test_generator.py
  golden/
    test_golden_cases.py

pyproject.toml
README.md
```

---

## Task 1: 初始化 Python 项目骨架

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `backend/__init__.py`
- Create: `backend/main.py`
- Create: `backend/api/__init__.py`
- Create: `backend/api/health.py`
- Create: `tests/api/test_health.py`

- [ ] **Step 1: 写 FastAPI 健康检查失败测试**

Create `tests/api/test_health.py`:

```python
from fastapi.testclient import TestClient

from backend.main import app


def test_health_endpoint_returns_ok():
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
```

- [ ] **Step 2: 创建项目依赖配置**

Create `pyproject.toml`:

```toml
[project]
name = "diagops"
version = "0.1.0"
description = "Event-driven SRE RCA agent MVP"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115.0",
    "pydantic>=2.8.0",
    "sqlalchemy>=2.0.0",
    "uvicorn[standard]>=0.30.0",
]

[dependency-groups]
dev = [
    "httpx>=0.27.0",
    "pytest>=8.0.0",
    "pytest-cov>=5.0.0",
    "ruff>=0.6.0",
]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["."]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 3: 创建 README**

Create `README.md`:

```markdown
# DiagOps

DiagOps is an event-driven SRE RCA agent MVP.

The first version focuses on application service incidents:

- simulated incidents
- Webhook incident events
- mock evidence providers
- rule-based RCA analysis
- evidence-backed Markdown reports

## Local Development

```bash
uv sync
uv run uvicorn backend.main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```
```

- [ ] **Step 4: 创建 FastAPI app 和 health route**

Create `backend/__init__.py`:

```python
"""DiagOps backend package."""
```

Create `backend/api/__init__.py`:

```python
"""API route package."""
```

Create `backend/api/health.py`:

```python
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

Create `backend/main.py`:

```python
from fastapi import FastAPI

from backend.api.health import router as health_router

app = FastAPI(title="DiagOps", version="0.1.0")
app.include_router(health_router)
```

- [ ] **Step 5: 安装依赖并运行测试**

Run:

```bash
uv sync
uv run pytest tests/api/test_health.py -v
```

Expected:

```text
tests/api/test_health.py::test_health_endpoint_returns_ok PASSED
```

- [ ] **Step 6: 提交**

```bash
git add pyproject.toml README.md backend tests/api/test_health.py
git commit -m "chore: initialize fastapi backend"
```

---

## Task 2: 定义领域模型

**Files:**
- Create: `backend/domain/__init__.py`
- Create: `backend/domain/events.py`
- Create: `backend/domain/evidence.py`
- Create: `backend/domain/hypotheses.py`
- Create: `backend/domain/reports.py`
- Create: `tests/domain/test_domain_models.py`

- [ ] **Step 1: 写领域模型测试**

Create `tests/domain/test_domain_models.py`:

```python
from datetime import datetime

from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.domain.reports import IncidentReport


def test_incident_event_defaults_time_window():
    event = IncidentEvent(
        source=IncidentSource.SIMULATED,
        service="payment-service",
        environment="prod",
        severity=Severity.CRITICAL,
        title="5xx error rate increased",
        description="payment-service started returning 500 errors",
        started_at=datetime.fromisoformat("2026-07-03T14:03:00+08:00"),
    )

    assert event.time_window_minutes == 30
    assert event.signals == {}


def test_evidence_item_keeps_structured_payload():
    item = EvidenceItem(
        provider=EvidenceProvider.LOG,
        kind=EvidenceKind.LOG_PATTERN,
        timestamp=datetime.fromisoformat("2026-07-03T14:04:00+08:00"),
        summary="NullPointerException increased",
        payload={"exception": "NullPointerException", "count": 120},
    )

    assert item.payload["count"] == 120


def test_hypothesis_links_supporting_and_contradicting_evidence():
    hypothesis = Hypothesis(
        cause_type=CauseType.DEPLOYMENT_REGRESSION,
        summary="Deployment likely introduced the error",
        confidence=0.84,
        supporting_evidence_ids=["ev-log-1", "ev-deploy-1"],
        contradicting_evidence_ids=["ev-dependency-1"],
        next_actions=["Review recent deployment", "Consider rollback"],
    )

    assert hypothesis.confidence == 0.84
    assert hypothesis.supporting_evidence_ids == ["ev-log-1", "ev-deploy-1"]


def test_report_contains_markdown_and_hypotheses():
    report = IncidentReport(
        investigation_id="inv-1",
        summary="Likely deployment regression",
        timeline=[{"time": "14:00", "event": "deployment"}],
        hypotheses=[],
        markdown="# RCA Report",
    )

    assert report.markdown == "# RCA Report"
```

- [ ] **Step 2: 实现事件模型**

Create `backend/domain/__init__.py`:

```python
"""Domain models for RCA investigations."""
```

Create `backend/domain/events.py`:

```python
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class IncidentSource(StrEnum):
    SIMULATED = "simulated"
    WEBHOOK = "webhook"
    MANUAL = "manual"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class IncidentEvent(BaseModel):
    source: IncidentSource
    service: str
    environment: str
    severity: Severity
    title: str
    description: str
    started_at: datetime
    time_window_minutes: int = 30
    signals: dict[str, str] = Field(default_factory=dict)
```

- [ ] **Step 3: 实现证据模型**

Create `backend/domain/evidence.py`:

```python
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class EvidenceProvider(StrEnum):
    LOG = "log"
    METRIC = "metric"
    DEPLOY = "deploy"
    DEPENDENCY = "dependency"
    SERVICE_CATALOG = "service_catalog"


class EvidenceKind(StrEnum):
    LOG_PATTERN = "log_pattern"
    METRIC_TREND = "metric_trend"
    DEPLOYMENT = "deployment"
    DEPENDENCY_HEALTH = "dependency_health"
    SERVICE_METADATA = "service_metadata"


class EvidenceItem(BaseModel):
    id: str = Field(default_factory=lambda: f"ev-{uuid4().hex}")
    provider: EvidenceProvider
    kind: EvidenceKind
    timestamp: datetime
    summary: str
    payload: dict[str, object] = Field(default_factory=dict)
    confidence: float = 1.0
```

- [ ] **Step 4: 实现假设模型**

Create `backend/domain/hypotheses.py`:

```python
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field


class CauseType(StrEnum):
    DEPLOYMENT_REGRESSION = "deployment_regression"
    TRAFFIC_SPIKE = "traffic_spike"
    DOWNSTREAM_DEPENDENCY_FAILURE = "downstream_dependency_failure"
    DATABASE_SLOWDOWN = "database_slowdown"
    SINGLE_INSTANCE_ISSUE = "single_instance_issue"
    UNKNOWN = "unknown"


class Hypothesis(BaseModel):
    id: str = Field(default_factory=lambda: f"hyp-{uuid4().hex}")
    cause_type: CauseType
    summary: str
    confidence: float
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
```

- [ ] **Step 5: 实现报告模型**

Create `backend/domain/reports.py`:

```python
from pydantic import BaseModel, Field

from backend.domain.hypotheses import Hypothesis


class IncidentReport(BaseModel):
    investigation_id: str
    summary: str
    timeline: list[dict[str, str]] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    markdown: str
```

- [ ] **Step 6: 运行测试**

Run:

```bash
uv run pytest tests/domain/test_domain_models.py -v
```

Expected:

```text
4 passed
```

- [ ] **Step 7: 提交**

```bash
git add backend/domain tests/domain/test_domain_models.py
git commit -m "feat: add rca domain models"
```

---

## Task 3: 添加 mock 事件案例数据

**Files:**
- Create: `data/incidents/deployment_regression.json`
- Create: `data/incidents/traffic_spike.json`
- Create: `data/incidents/dependency_timeout.json`
- Create: `data/incidents/database_slowdown.json`
- Create: `data/incidents/single_bad_instance.json`
- Create: `backend/services/__init__.py`
- Create: `backend/services/incident_cases.py`
- Create: `tests/services/test_incident_cases.py`

- [ ] **Step 1: 写案例加载测试**

Create `tests/services/test_incident_cases.py`:

```python
from backend.domain.events import IncidentSource
from backend.services.incident_cases import list_case_ids, load_incident_case


def test_list_case_ids_returns_all_seed_cases():
    assert list_case_ids() == [
        "database_slowdown",
        "dependency_timeout",
        "deployment_regression",
        "single_bad_instance",
        "traffic_spike",
    ]


def test_load_incident_case_returns_incident_event():
    event = load_incident_case("deployment_regression")

    assert event.source == IncidentSource.SIMULATED
    assert event.service == "payment-service"
    assert event.signals["error_rate"] == "high"
```

- [ ] **Step 2: 创建五个事件 JSON**

Create `data/incidents/deployment_regression.json`:

```json
{
  "source": "simulated",
  "service": "payment-service",
  "environment": "prod",
  "severity": "critical",
  "title": "5xx error rate increased after deployment",
  "description": "payment-service started returning many 500 responses after v1.8.2 deployment",
  "started_at": "2026-07-03T14:03:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "error_rate": "high",
    "latency": "normal",
    "qps": "normal"
  }
}
```

Create `data/incidents/traffic_spike.json`:

```json
{
  "source": "simulated",
  "service": "checkout-service",
  "environment": "prod",
  "severity": "warning",
  "title": "Latency increased during QPS spike",
  "description": "checkout-service latency increased when traffic suddenly grew",
  "started_at": "2026-07-03T15:10:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "error_rate": "medium",
    "latency": "high",
    "qps": "high"
  }
}
```

Create `data/incidents/dependency_timeout.json`:

```json
{
  "source": "simulated",
  "service": "order-service",
  "environment": "prod",
  "severity": "critical",
  "title": "Order API timeout increased",
  "description": "order-service timeout increased when inventory-service became slow",
  "started_at": "2026-07-03T16:20:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "error_rate": "high",
    "latency": "high",
    "dependency": "inventory-service"
  }
}
```

Create `data/incidents/database_slowdown.json`:

```json
{
  "source": "simulated",
  "service": "report-service",
  "environment": "prod",
  "severity": "warning",
  "title": "Report queries became slow",
  "description": "report-service endpoints slowed down because database query latency increased",
  "started_at": "2026-07-03T17:30:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "latency": "high",
    "database": "slow"
  }
}
```

Create `data/incidents/single_bad_instance.json`:

```json
{
  "source": "simulated",
  "service": "profile-service",
  "environment": "prod",
  "severity": "warning",
  "title": "One instance has abnormal error rate",
  "description": "profile-service has one instance with high CPU and many errors",
  "started_at": "2026-07-03T18:40:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "error_rate": "medium",
    "instance": "abnormal",
    "cpu": "high"
  }
}
```

- [ ] **Step 3: 实现案例加载服务**

Create `backend/services/__init__.py`:

```python
"""Application services."""
```

Create `backend/services/incident_cases.py`:

```python
import json
from pathlib import Path

from backend.domain.events import IncidentEvent

INCIDENT_CASE_DIR = Path("data/incidents")


def list_case_ids() -> list[str]:
    return sorted(path.stem for path in INCIDENT_CASE_DIR.glob("*.json"))


def load_incident_case(case_id: str) -> IncidentEvent:
    case_path = INCIDENT_CASE_DIR / f"{case_id}.json"
    if not case_path.exists():
        raise ValueError(f"Unknown incident case: {case_id}")

    return IncidentEvent.model_validate(json.loads(case_path.read_text(encoding="utf-8")))
```

- [ ] **Step 4: 运行测试**

Run:

```bash
uv run pytest tests/services/test_incident_cases.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 5: 提交**

```bash
git add data/incidents backend/services tests/services/test_incident_cases.py
git commit -m "feat: add simulated incident cases"
```

---

## Task 4: 实现 Provider 接口和 mock Provider

**Files:**
- Create: `backend/providers/__init__.py`
- Create: `backend/providers/base.py`
- Create: `backend/providers/mock_logs.py`
- Create: `backend/providers/mock_metrics.py`
- Create: `backend/providers/mock_deploys.py`
- Create: `backend/providers/mock_dependencies.py`
- Create: `backend/providers/registry.py`
- Create: `tests/providers/test_mock_providers.py`

- [ ] **Step 1: 写 Provider 测试**

Create `tests/providers/test_mock_providers.py`:

```python
from backend.providers.registry import build_mock_provider_registry
from backend.services.incident_cases import load_incident_case


def test_mock_registry_collects_deployment_regression_evidence():
    event = load_incident_case("deployment_regression")
    registry = build_mock_provider_registry()

    evidence = registry.collect_all(event)

    summaries = [item.summary for item in evidence]
    assert any("NullPointerException" in summary for summary in summaries)
    assert any("v1.8.2" in summary for summary in summaries)
    assert any("QPS stayed within normal range" in summary for summary in summaries)


def test_mock_registry_collects_dependency_timeout_evidence():
    event = load_incident_case("dependency_timeout")
    registry = build_mock_provider_registry()

    evidence = registry.collect_all(event)

    assert any("inventory-service latency increased" in item.summary for item in evidence)
```

- [ ] **Step 2: 定义 Provider 协议**

Create `backend/providers/__init__.py`:

```python
"""Evidence provider implementations."""
```

Create `backend/providers/base.py`:

```python
from typing import Protocol

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem


class EvidenceProviderProtocol(Protocol):
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        """Collect evidence for an incident event."""
```

- [ ] **Step 3: 实现日志 mock Provider**

Create `backend/providers/mock_logs.py`:

```python
from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockLogProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        if event.service == "payment-service":
            return [
                EvidenceItem(
                    provider=EvidenceProvider.LOG,
                    kind=EvidenceKind.LOG_PATTERN,
                    timestamp=event.started_at + timedelta(minutes=1),
                    summary="New NullPointerException appears in /pay/confirm after deployment",
                    payload={"exception": "NullPointerException", "endpoint": "/pay/confirm", "count": 120},
                )
            ]

        if event.service == "order-service":
            return [
                EvidenceItem(
                    provider=EvidenceProvider.LOG,
                    kind=EvidenceKind.LOG_PATTERN,
                    timestamp=event.started_at,
                    summary="TimeoutException increased when calling inventory-service",
                    payload={"exception": "TimeoutException", "dependency": "inventory-service", "count": 90},
                )
            ]

        return []
```

- [ ] **Step 4: 实现指标 mock Provider**

Create `backend/providers/mock_metrics.py`:

```python
from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockMetricProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []

        if event.signals.get("qps") == "high":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at - timedelta(minutes=2),
                    summary="QPS increased sharply before latency and errors increased",
                    payload={"qps_change": "+260%", "latency_p95": "1800ms"},
                )
            )
        elif event.service == "payment-service":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at,
                    summary="QPS stayed within normal range while 5xx increased",
                    payload={"qps_change": "+3%", "error_rate": "12%"},
                )
            )

        if event.signals.get("cpu") == "high":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at,
                    summary="One instance has high CPU and abnormal error rate",
                    payload={"instance": "profile-service-3", "cpu": "94%", "error_rate": "18%"},
                )
            )

        if event.signals.get("database") == "slow":
            evidence.append(
                EvidenceItem(
                    provider=EvidenceProvider.METRIC,
                    kind=EvidenceKind.METRIC_TREND,
                    timestamp=event.started_at - timedelta(minutes=1),
                    summary="Database query latency increased before report-service latency increased",
                    payload={"db_p95": "2400ms", "endpoint": "/reports/daily"},
                )
            )

        return evidence
```

- [ ] **Step 5: 实现发布和依赖 mock Provider**

Create `backend/providers/mock_deploys.py`:

```python
from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockDeployProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        if event.service != "payment-service":
            return []

        return [
            EvidenceItem(
                provider=EvidenceProvider.DEPLOY,
                kind=EvidenceKind.DEPLOYMENT,
                timestamp=event.started_at - timedelta(minutes=3),
                summary="payment-service v1.8.2 was deployed three minutes before 5xx increased",
                payload={"version": "v1.8.2", "commit": "abc1234", "changed_module": "PayConfirmHandler"},
            )
        ]
```

Create `backend/providers/mock_dependencies.py`:

```python
from datetime import timedelta

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider


class MockDependencyProvider:
    def collect(self, event: IncidentEvent) -> list[EvidenceItem]:
        if event.service == "order-service":
            return [
                EvidenceItem(
                    provider=EvidenceProvider.DEPENDENCY,
                    kind=EvidenceKind.DEPENDENCY_HEALTH,
                    timestamp=event.started_at - timedelta(minutes=2),
                    summary="inventory-service latency increased before order-service timeout errors",
                    payload={"dependency": "inventory-service", "latency_p95": "3200ms", "error_rate": "8%"},
                )
            ]

        if event.service == "payment-service":
            return [
                EvidenceItem(
                    provider=EvidenceProvider.DEPENDENCY,
                    kind=EvidenceKind.DEPENDENCY_HEALTH,
                    timestamp=event.started_at + timedelta(minutes=2),
                    summary="payment dependency latency rose slightly after local errors started",
                    payload={"dependency": "payment-gateway", "latency_p95": "900ms"},
                )
            ]

        return []
```

- [ ] **Step 6: 实现 Provider Registry**

Create `backend/providers/registry.py`:

```python
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.providers.base import EvidenceProviderProtocol
from backend.providers.mock_dependencies import MockDependencyProvider
from backend.providers.mock_deploys import MockDeployProvider
from backend.providers.mock_logs import MockLogProvider
from backend.providers.mock_metrics import MockMetricProvider


class ProviderRegistry:
    def __init__(self, providers: list[EvidenceProviderProtocol]) -> None:
        self.providers = providers

    def collect_all(self, event: IncidentEvent) -> list[EvidenceItem]:
        evidence: list[EvidenceItem] = []
        for provider in self.providers:
            evidence.extend(provider.collect(event))
        return sorted(evidence, key=lambda item: item.timestamp)


def build_mock_provider_registry() -> ProviderRegistry:
    return ProviderRegistry(
        providers=[
            MockLogProvider(),
            MockMetricProvider(),
            MockDeployProvider(),
            MockDependencyProvider(),
        ]
    )
```

- [ ] **Step 7: 运行测试**

Run:

```bash
uv run pytest tests/providers/test_mock_providers.py -v
```

Expected:

```text
2 passed
```

- [ ] **Step 8: 提交**

```bash
git add backend/providers tests/providers/test_mock_providers.py
git commit -m "feat: add mock evidence providers"
```

---

## Task 5: 实现规则 RCA Analyzer

**Files:**
- Create: `backend/rca/__init__.py`
- Create: `backend/rca/scoring.py`
- Create: `backend/rca/analyzer.py`
- Create: `tests/rca/test_analyzer.py`

- [ ] **Step 1: 写 Analyzer 测试**

Create `tests/rca/test_analyzer.py`:

```python
from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.services.incident_cases import load_incident_case


def _analyze_case(case_id: str):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    return RcaAnalyzer().analyze(event, evidence)


def test_deployment_regression_ranked_first():
    hypotheses = _analyze_case("deployment_regression")

    assert hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert hypotheses[0].confidence >= 0.8


def test_traffic_spike_ranked_first():
    hypotheses = _analyze_case("traffic_spike")

    assert hypotheses[0].cause_type == CauseType.TRAFFIC_SPIKE


def test_dependency_timeout_ranked_first():
    hypotheses = _analyze_case("dependency_timeout")

    assert hypotheses[0].cause_type == CauseType.DOWNSTREAM_DEPENDENCY_FAILURE


def test_database_slowdown_ranked_first():
    hypotheses = _analyze_case("database_slowdown")

    assert hypotheses[0].cause_type == CauseType.DATABASE_SLOWDOWN


def test_single_bad_instance_ranked_first():
    hypotheses = _analyze_case("single_bad_instance")

    assert hypotheses[0].cause_type == CauseType.SINGLE_INSTANCE_ISSUE
```

- [ ] **Step 2: 实现评分工具**

Create `backend/rca/__init__.py`:

```python
"""Root cause analysis package."""
```

Create `backend/rca/scoring.py`:

```python
from backend.domain.evidence import EvidenceItem


def contains_any(evidence: list[EvidenceItem], keywords: list[str]) -> list[EvidenceItem]:
    matches: list[EvidenceItem] = []
    lowered_keywords = [keyword.lower() for keyword in keywords]

    for item in evidence:
        text = f"{item.summary} {item.payload}".lower()
        if any(keyword in text for keyword in lowered_keywords):
            matches.append(item)

    return matches


def confidence_from_score(score: int) -> float:
    return min(0.95, round(score / 10, 2))
```

- [ ] **Step 3: 实现 RCA Analyzer**

Create `backend/rca/analyzer.py`:

```python
from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import CauseType, Hypothesis
from backend.rca.scoring import confidence_from_score, contains_any


class RcaAnalyzer:
    def analyze(self, event: IncidentEvent, evidence: list[EvidenceItem]) -> list[Hypothesis]:
        hypotheses = [
            self._deployment_regression(evidence),
            self._traffic_spike(event, evidence),
            self._dependency_failure(evidence),
            self._database_slowdown(event, evidence),
            self._single_instance_issue(event, evidence),
        ]

        ranked = [hypothesis for hypothesis in hypotheses if hypothesis.confidence > 0]
        ranked.sort(key=lambda hypothesis: hypothesis.confidence, reverse=True)

        if ranked:
            return ranked

        return [
            Hypothesis(
                cause_type=CauseType.UNKNOWN,
                summary="证据不足，暂时无法判断明确根因。",
                confidence=0.2,
                next_actions=["扩大时间窗口", "补充日志、指标、发布和依赖证据"],
            )
        ]

    def _deployment_regression(self, evidence: list[EvidenceItem]) -> Hypothesis:
        deploy_evidence = contains_any(evidence, ["deployed", "deployment", "v1.8.2"])
        error_evidence = contains_any(evidence, ["NullPointerException", "5xx"])
        normal_qps = contains_any(evidence, ["QPS stayed within normal range"])
        score = len(deploy_evidence) * 3 + len(error_evidence) * 3 + len(normal_qps) * 2

        return Hypothesis(
            cause_type=CauseType.DEPLOYMENT_REGRESSION,
            summary="最可能是近期发版引入的应用代码异常。",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=[item.id for item in deploy_evidence + error_evidence + normal_qps],
            next_actions=["检查最近发布版本的变更", "优先检查异常堆栈指向的模块", "如错误仍在持续，评估回滚"],
        )

    def _traffic_spike(self, event: IncidentEvent, evidence: list[EvidenceItem]) -> Hypothesis:
        qps_evidence = contains_any(evidence, ["QPS increased sharply"])
        score = 8 if event.signals.get("qps") == "high" else len(qps_evidence) * 4

        return Hypothesis(
            cause_type=CauseType.TRAFFIC_SPIKE,
            summary="最可能是流量突增导致延迟或错误升高。",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=[item.id for item in qps_evidence],
            next_actions=["确认入口流量来源", "检查限流和扩容策略", "观察下游依赖是否被流量放大影响"],
        )

    def _dependency_failure(self, evidence: list[EvidenceItem]) -> Hypothesis:
        dependency_evidence = contains_any(evidence, ["inventory-service latency increased", "dependency"])
        score = len(dependency_evidence) * 4

        return Hypothesis(
            cause_type=CauseType.DOWNSTREAM_DEPENDENCY_FAILURE,
            summary="最可能是下游依赖异常导致当前服务超时或错误。",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=[item.id for item in dependency_evidence],
            next_actions=["检查下游依赖健康状态", "确认超时、重试和熔断配置", "联系依赖服务负责人"],
        )

    def _database_slowdown(self, event: IncidentEvent, evidence: list[EvidenceItem]) -> Hypothesis:
        db_evidence = contains_any(evidence, ["Database query latency"])
        score = 8 if event.signals.get("database") == "slow" else len(db_evidence) * 4

        return Hypothesis(
            cause_type=CauseType.DATABASE_SLOWDOWN,
            summary="最可能是数据库查询变慢导致服务延迟升高。",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=[item.id for item in db_evidence],
            next_actions=["检查慢 SQL", "确认索引和执行计划", "观察数据库连接池和锁等待"],
        )

    def _single_instance_issue(self, event: IncidentEvent, evidence: list[EvidenceItem]) -> Hypothesis:
        instance_evidence = contains_any(evidence, ["One instance has high CPU", "abnormal error rate"])
        score = 8 if event.signals.get("instance") == "abnormal" else len(instance_evidence) * 4

        return Hypothesis(
            cause_type=CauseType.SINGLE_INSTANCE_ISSUE,
            summary="最可能是单个实例资源或运行状态异常。",
            confidence=confidence_from_score(score),
            supporting_evidence_ids=[item.id for item in instance_evidence],
            next_actions=["摘除异常实例", "检查该实例 CPU、内存、磁盘和进程状态", "对比正常实例日志"],
        )
```

- [ ] **Step 4: 运行测试**

Run:

```bash
uv run pytest tests/rca/test_analyzer.py -v
```

Expected:

```text
5 passed
```

- [ ] **Step 5: 提交**

```bash
git add backend/rca tests/rca/test_analyzer.py
git commit -m "feat: add rule based rca analyzer"
```

---

## Task 6: 实现 Markdown 报告生成器

**Files:**
- Create: `backend/reports/__init__.py`
- Create: `backend/reports/generator.py`
- Create: `tests/reports/test_generator.py`

- [ ] **Step 1: 写报告生成测试**

Create `tests/reports/test_generator.py`:

```python
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


def test_report_generator_outputs_markdown_with_evidence():
    event = load_incident_case("deployment_regression")
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)

    report = ReportGenerator().generate("inv-1", event, evidence, hypotheses)

    assert report.investigation_id == "inv-1"
    assert "最可能根因" in report.markdown
    assert "NullPointerException" in report.markdown
    assert "payment-service v1.8.2" in report.markdown
    assert "事实与推断" in report.markdown
```

- [ ] **Step 2: 实现报告生成器**

Create `backend/reports/__init__.py`:

```python
"""Report generation package."""
```

Create `backend/reports/generator.py`:

```python
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
            lines.append(f"- `{item.timestamp.isoformat()}` [{item.provider}/{item.kind}] {item.summary}")

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
```

- [ ] **Step 3: 运行测试**

Run:

```bash
uv run pytest tests/reports/test_generator.py -v
```

Expected:

```text
1 passed
```

- [ ] **Step 4: 提交**

```bash
git add backend/reports tests/reports/test_generator.py
git commit -m "feat: add rca report generator"
```

---

## Task 7: 实现内存版调查仓储和诊断编排器

**Files:**
- Create: `backend/db/__init__.py`
- Create: `backend/db/models.py`
- Create: `backend/db/repositories.py`
- Create: `backend/diagnosis/__init__.py`
- Create: `backend/diagnosis/orchestrator.py`
- Create: `tests/diagnosis/test_orchestrator.py`

- [ ] **Step 1: 写编排器测试**

Create `tests/diagnosis/test_orchestrator.py`:

```python
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


def test_orchestrator_creates_investigation_with_report():
    repository = InMemoryInvestigationRepository()
    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=build_mock_provider_registry(),
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
    )

    investigation = orchestrator.run(load_incident_case("deployment_regression"))

    stored = repository.get(investigation.id)
    assert stored.id == investigation.id
    assert stored.report is not None
    assert stored.hypotheses[0].cause_type == CauseType.DEPLOYMENT_REGRESSION
    assert "最可能根因" in stored.report.markdown
```

- [ ] **Step 2: 实现调查记录模型**

Create `backend/db/__init__.py`:

```python
"""Persistence package."""
```

Create `backend/db/models.py`:

```python
from datetime import datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem
from backend.domain.hypotheses import Hypothesis
from backend.domain.reports import IncidentReport


class InvestigationStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


class InvestigationRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"inv-{uuid4().hex}")
    event: IncidentEvent
    status: InvestigationStatus
    evidence: list[EvidenceItem] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    report: IncidentReport | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
```

- [ ] **Step 3: 实现内存仓储**

Create `backend/db/repositories.py`:

```python
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
        return sorted(self._records.values(), key=lambda record: record.created_at, reverse=True)
```

- [ ] **Step 4: 实现 DiagnosisOrchestrator**

Create `backend/diagnosis/__init__.py`:

```python
"""Diagnosis orchestration package."""
```

Create `backend/diagnosis/orchestrator.py`:

```python
from backend.db.models import InvestigationRecord, InvestigationStatus
from backend.db.repositories import InMemoryInvestigationRepository
from backend.domain.events import IncidentEvent
from backend.providers.registry import ProviderRegistry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator


class DiagnosisOrchestrator:
    def __init__(
        self,
        repository: InMemoryInvestigationRepository,
        providers: ProviderRegistry,
        analyzer: RcaAnalyzer,
        report_generator: ReportGenerator,
    ) -> None:
        self.repository = repository
        self.providers = providers
        self.analyzer = analyzer
        self.report_generator = report_generator

    def run(self, event: IncidentEvent) -> InvestigationRecord:
        evidence = self.providers.collect_all(event)
        hypotheses = self.analyzer.analyze(event, evidence)

        record = InvestigationRecord(
            event=event,
            status=InvestigationStatus.COMPLETED,
            evidence=evidence,
            hypotheses=hypotheses,
        )
        report = self.report_generator.generate(record.id, event, evidence, hypotheses)
        record.report = report

        return self.repository.save(record)
```

- [ ] **Step 5: 运行测试**

Run:

```bash
uv run pytest tests/diagnosis/test_orchestrator.py -v
```

Expected:

```text
1 passed
```

- [ ] **Step 6: 提交**

```bash
git add backend/db backend/diagnosis tests/diagnosis/test_orchestrator.py
git commit -m "feat: add diagnosis orchestrator"
```

---

## Task 8: 实现事件和调查 API

**Files:**
- Create: `backend/api/events.py`
- Create: `backend/api/investigations.py`
- Modify: `backend/main.py`
- Create: `backend/services/container.py`
- Create: `tests/api/test_events_api.py`
- Create: `tests/api/test_investigations_api.py`

- [ ] **Step 1: 写 API 测试**

Create `tests/api/test_events_api.py`:

```python
from fastapi.testclient import TestClient

from backend.main import app


def test_create_simulated_event_returns_completed_investigation():
    client = TestClient(app)

    response = client.post("/events/simulated/deployment_regression")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["service"] == "payment-service"
    assert body["top_cause_type"] == "deployment_regression"


def test_create_webhook_event_returns_completed_investigation():
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
    assert response.json()["top_cause_type"] == "traffic_spike"
```

Create `tests/api/test_investigations_api.py`:

```python
from fastapi.testclient import TestClient

from backend.main import app


def test_get_investigation_after_simulated_event():
    client = TestClient(app)
    created = client.post("/events/simulated/deployment_regression").json()

    response = client.get(f"/investigations/{created['id']}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == created["id"]
    assert body["report"]["markdown"].startswith("# payment-service RCA")


def test_list_investigations_includes_created_record():
    client = TestClient(app)
    created = client.post("/events/simulated/dependency_timeout").json()

    response = client.get("/investigations")

    assert response.status_code == 200
    assert any(item["id"] == created["id"] for item in response.json())
```

- [ ] **Step 2: 实现应用容器**

Create `backend/services/container.py`:

```python
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator


class AppContainer:
    def __init__(self) -> None:
        self.repository = InMemoryInvestigationRepository()
        self.orchestrator = DiagnosisOrchestrator(
            repository=self.repository,
            providers=build_mock_provider_registry(),
            analyzer=RcaAnalyzer(),
            report_generator=ReportGenerator(),
        )


container = AppContainer()
```

- [ ] **Step 3: 实现事件 API**

Create `backend/api/events.py`:

```python
from pydantic import BaseModel
from fastapi import APIRouter

from backend.domain.events import IncidentEvent
from backend.services.container import container
from backend.services.incident_cases import load_incident_case

router = APIRouter(prefix="/events", tags=["events"])


class InvestigationSummary(BaseModel):
    id: str
    status: str
    service: str
    title: str
    top_cause_type: str
    confidence: float


def to_summary(record) -> InvestigationSummary:
    top = record.hypotheses[0]
    return InvestigationSummary(
        id=record.id,
        status=record.status,
        service=record.event.service,
        title=record.event.title,
        top_cause_type=top.cause_type,
        confidence=top.confidence,
    )


@router.post("", response_model=InvestigationSummary)
def create_event(event: IncidentEvent) -> InvestigationSummary:
    record = container.orchestrator.run(event)
    return to_summary(record)


@router.post("/simulated/{case_id}", response_model=InvestigationSummary)
def create_simulated_event(case_id: str) -> InvestigationSummary:
    event = load_incident_case(case_id)
    record = container.orchestrator.run(event)
    return to_summary(record)
```

- [ ] **Step 4: 实现调查 API**

Create `backend/api/investigations.py`:

```python
from fastapi import APIRouter

from backend.db.models import InvestigationRecord
from backend.services.container import container

router = APIRouter(prefix="/investigations", tags=["investigations"])


@router.get("", response_model=list[InvestigationRecord])
def list_investigations() -> list[InvestigationRecord]:
    return container.repository.list()


@router.get("/{investigation_id}", response_model=InvestigationRecord)
def get_investigation(investigation_id: str) -> InvestigationRecord:
    return container.repository.get(investigation_id)
```

- [ ] **Step 5: 注册 API 路由**

Modify `backend/main.py`:

```python
from fastapi import FastAPI

from backend.api.events import router as events_router
from backend.api.health import router as health_router
from backend.api.investigations import router as investigations_router

app = FastAPI(title="DiagOps", version="0.1.0")
app.include_router(health_router)
app.include_router(events_router)
app.include_router(investigations_router)
```

- [ ] **Step 6: 运行 API 测试**

Run:

```bash
uv run pytest tests/api -v
```

Expected:

```text
5 passed
```

- [ ] **Step 7: 提交**

```bash
git add backend/api backend/main.py backend/services/container.py tests/api
git commit -m "feat: add event and investigation api"
```

---

## Task 9: 添加 golden case 评估测试

**Files:**
- Create: `tests/golden/test_golden_cases.py`

- [ ] **Step 1: 写 golden case 测试**

Create `tests/golden/test_golden_cases.py`:

```python
import pytest

from backend.domain.hypotheses import CauseType
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.services.incident_cases import load_incident_case


@pytest.mark.parametrize(
    ("case_id", "expected_cause", "required_evidence"),
    [
        ("deployment_regression", CauseType.DEPLOYMENT_REGRESSION, "NullPointerException"),
        ("traffic_spike", CauseType.TRAFFIC_SPIKE, "QPS increased sharply"),
        ("dependency_timeout", CauseType.DOWNSTREAM_DEPENDENCY_FAILURE, "inventory-service latency increased"),
        ("database_slowdown", CauseType.DATABASE_SLOWDOWN, "Database query latency"),
        ("single_bad_instance", CauseType.SINGLE_INSTANCE_ISSUE, "One instance has high CPU"),
    ],
)
def test_golden_case_primary_cause_and_report_evidence(case_id, expected_cause, required_evidence):
    event = load_incident_case(case_id)
    evidence = build_mock_provider_registry().collect_all(event)
    hypotheses = RcaAnalyzer().analyze(event, evidence)
    report = ReportGenerator().generate(f"inv-{case_id}", event, evidence, hypotheses)

    assert hypotheses[0].cause_type == expected_cause
    assert required_evidence in report.markdown
    assert "事实与推断" in report.markdown
```

- [ ] **Step 2: 运行 golden tests**

Run:

```bash
uv run pytest tests/golden/test_golden_cases.py -v
```

Expected:

```text
5 passed
```

- [ ] **Step 3: 运行全量测试**

Run:

```bash
uv run pytest -v
```

Expected:

```text
all tests passed
```

- [ ] **Step 4: 提交**

```bash
git add tests/golden/test_golden_cases.py
git commit -m "test: add rca golden cases"
```

---

## Task 10: 添加本地运行说明和 Webhook 示例

**Files:**
- Modify: `README.md`
- Create: `docs/examples/webhook-event.json`

- [ ] **Step 1: 创建 Webhook 示例**

Create `docs/examples/webhook-event.json`:

```json
{
  "source": "webhook",
  "service": "checkout-service",
  "environment": "prod",
  "severity": "warning",
  "title": "Latency increased during QPS spike",
  "description": "checkout-service latency increased when traffic suddenly grew",
  "started_at": "2026-07-03T15:10:00+08:00",
  "time_window_minutes": 30,
  "signals": {
    "error_rate": "medium",
    "latency": "high",
    "qps": "high"
  }
}
```

- [ ] **Step 2: 更新 README**

Modify `README.md`:

```markdown
# DiagOps

DiagOps is an event-driven SRE RCA agent MVP.

The first version focuses on application service incidents:

- simulated incidents
- Webhook incident events
- mock evidence providers
- rule-based RCA analysis
- evidence-backed Markdown reports

## Local Development

```bash
uv sync
uv run uvicorn backend.main:app --reload
```

Health check:

```bash
curl http://127.0.0.1:8000/health
```

## Trigger A Simulated Incident

```bash
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
```

## Send A Webhook Event

```bash
curl -X POST http://127.0.0.1:8000/events \
  -H "Content-Type: application/json" \
  -d @docs/examples/webhook-event.json
```

## List Investigations

```bash
curl http://127.0.0.1:8000/investigations
```

## MVP Boundaries

The MVP does not automatically modify production systems. It only collects evidence, ranks hypotheses, and generates an RCA report for engineer review.
```

- [ ] **Step 3: 运行全量测试**

Run:

```bash
uv run pytest -v
```

Expected:

```text
all tests passed
```

- [ ] **Step 4: 提交**

```bash
git add README.md docs/examples/webhook-event.json
git commit -m "docs: add local usage examples"
```

---

## Task 11: 最终验证与推送

**Files:**
- No new files.

- [ ] **Step 1: 运行格式检查**

Run:

```bash
uv run ruff check .
```

Expected:

```text
All checks passed!
```

- [ ] **Step 2: 运行全量测试**

Run:

```bash
uv run pytest -v
```

Expected:

```text
all tests passed
```

- [ ] **Step 3: 本地启动服务**

Run:

```bash
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Expected:

```text
Uvicorn running on http://127.0.0.1:8000
```

- [ ] **Step 4: 手动验证 API**

In a second terminal:

```bash
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
curl http://127.0.0.1:8000/investigations
```

Expected:

```text
health returns {"status":"ok"}
simulated event returns top_cause_type deployment_regression
investigations contains the created investigation
```

- [ ] **Step 5: 推送**

```bash
git status -sb
git push
```

Expected:

```text
main is pushed to origin/main
working tree is clean
```

---

## 后续计划

当前计划完成后，再写新的计划实现：

1. React + TypeScript 前端页面。
2. SQLite 持久化替换内存仓储。
3. OpenAI Agents SDK 多 Agent 执行层。
4. Prometheus / Loki / ELK 真实 Provider。
5. 证据链可视化和通知集成。

