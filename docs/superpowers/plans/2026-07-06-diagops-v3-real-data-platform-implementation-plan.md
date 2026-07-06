# DiagOps V3 Real Data Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade DiagOps from a V2 in-memory RCA platform loop into a V3 read-only real-data diagnosis platform with SQLite persistence, configurable providers, richer detail APIs, a first Web UI, optional read-only LLM analysis, and D-drive local data conventions.

**Architecture:** Keep the current FastAPI, Pydantic domain models, provider registry, coordinator, analyzer, action planner, and report generator as the core. Add a SQLAlchemy-backed repository beside the existing in-memory repository, introduce configuration-driven provider wiring, expose richer read APIs, and add a frontend that consumes the API without any production write actions beyond action/verification status updates.

**Tech Stack:** Python, FastAPI, Pydantic, SQLAlchemy 2.x, SQLite, pytest, ruff, uv, React, Vite, TypeScript, TanStack Query, Node.js from `D:\paiflow\nodejs`, D-drive caches and data directories.

---

## Scope Check

This plan implements the V3 read-only real-data platform described in `docs/superpowers/specs/2026-07-06-diagops-v3-real-data-platform-design.md`.

V3 includes SQLite persistence, configuration, local file providers, optional Prometheus HTTP provider, richer investigation APIs, a first Web UI, optional read-only LLM analysis model and service, README updates, and final smoke tests.

V3 does not implement SSH execution, auto-remediation, rollback execution, restart execution, scaling execution, Kubernetes control, a full CMDB, user login, permissions, vector databases, or mandatory PostgreSQL.

## Environment Rules

- Project root: `D:\agent\sre-agent`
- uv cache: `D:\agent\.uv-cache`
- SQLite database: `D:\agent\sre-agent\data\diagops.db`
- Sample logs: `D:\agent\sre-agent\data\sample-logs`
- Deployment records: `D:\agent\sre-agent\data\deployments`
- Service catalog config: `D:\agent\sre-agent\config\services.yaml`
- Frontend: `D:\agent\sre-agent\frontend`
- Node.js: `D:\paiflow\nodejs\node.exe`
- npm cache: `D:\agent\.npm-cache`

Before frontend dependency installation, run:

```powershell
npm config set cache D:\agent\.npm-cache --global
```

Expected:

```text
npm cache path points to D:\agent\.npm-cache
```

## File Structure

Create:

- `backend/config/settings.py` - typed application configuration and environment overrides.
- `backend/db/schema.py` - SQLAlchemy metadata and table definitions.
- `backend/db/sqlite_repository.py` - SQLite-backed investigation repository.
- `backend/db/serialization.py` - conversion helpers between domain models and database rows.
- `backend/db/session.py` - engine/session factory and schema initialization.
- `backend/providers/file_service_catalog.py` - service catalog file provider.
- `backend/providers/file_deployments.py` - deployment JSON provider.
- `backend/providers/file_logs.py` - local log file provider.
- `backend/providers/prometheus.py` - optional Prometheus HTTP provider with failure downgrade.
- `backend/domain/llm_analysis.py` - read-only LLM analysis domain model.
- `backend/diagnosis/llm_analyst.py` - optional evidence-grounded LLM analyst service.
- `backend/api/config.py` - provider configuration API.
- `config/diagops.yaml` - default local configuration.
- `config/services.yaml` - sample service catalog.
- `data/sample-logs/checkout-service.log` - sample local log file.
- `data/deployments/deployments.json` - sample deployment file.
- `frontend/` - Vite React TypeScript UI.
- `tests/config/test_settings.py`
- `tests/db/test_sqlite_repository.py`
- `tests/providers/test_file_service_catalog.py`
- `tests/providers/test_file_deployments.py`
- `tests/providers/test_file_logs.py`
- `tests/providers/test_prometheus_provider.py`
- `tests/api/test_v3_investigation_detail_api.py`
- `tests/diagnosis/test_llm_analyst.py`
- `tests/frontend/test_frontend_smoke.py`

Modify:

- `backend/services/container.py` - load settings and wire SQLite repository/providers.
- `backend/providers/registry.py` - build providers from configuration.
- `backend/diagnosis/orchestrator.py` - persist provider and specialist results where available.
- `backend/db/models.py` - store provider_results, specialist_results, llm_analysis if needed.
- `backend/api/investigations.py` - add detail endpoints.
- `backend/main.py` - include config API and serve UI if implemented through FastAPI static files.
- `tests/conftest.py` - test database isolation if needed.
- `tests/golden/test_golden_cases.py` - run at least one golden case through SQLite repository.
- `README.md` - V3 startup, SQLite, provider config, UI usage.
- `.gitignore` - ignore generated SQLite database and frontend build output.

---

### Task 1: Configuration System And D-Drive Defaults

**Files:**
- Create: `backend/config/settings.py`
- Create: `config/diagops.yaml`
- Create: `config/services.yaml`
- Modify: `.gitignore`
- Test: `tests/config/test_settings.py`

- [ ] **Step 1: Write configuration tests**

Create `tests/config/test_settings.py`:

```python
from pathlib import Path

from backend.config.settings import AppSettings, load_settings


def test_default_settings_use_d_drive_project_paths(monkeypatch):
    monkeypatch.delenv("DIAGOPS_CONFIG", raising=False)
    settings = load_settings()

    assert settings.storage.url == "sqlite:///data/diagops.db"
    assert settings.providers.mock.enabled is True
    assert settings.providers.service_catalog.path == Path("config/services.yaml")
    assert settings.providers.deployment_file.path == Path("data/deployments/deployments.json")
    assert settings.providers.log_file.paths == [Path("data/sample-logs/checkout-service.log")]
    assert settings.llm.enabled is False


def test_environment_database_url_override(monkeypatch, tmp_path):
    config_path = tmp_path / "diagops.yaml"
    config_path.write_text("storage:\n  url: sqlite:///old.db\n", encoding="utf-8")
    monkeypatch.setenv("DIAGOPS_CONFIG", str(config_path))
    monkeypatch.setenv("DIAGOPS_DATABASE_URL", "sqlite:///data/test.db")

    settings = load_settings()

    assert settings.storage.url == "sqlite:///data/test.db"


def test_missing_config_file_uses_safe_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))

    settings = load_settings()

    assert isinstance(settings, AppSettings)
    assert settings.providers.prometheus.enabled is False
```

- [ ] **Step 2: Add config module**

Create `backend/config/settings.py` with Pydantic models:

```python
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class StorageSettings(BaseModel):
    url: str = "sqlite:///data/diagops.db"


class ProviderToggle(BaseModel):
    enabled: bool = False


class LogFileProviderSettings(ProviderToggle):
    paths: list[Path] = Field(default_factory=lambda: [Path("data/sample-logs/checkout-service.log")])


class PrometheusProviderSettings(ProviderToggle):
    base_url: str = "http://127.0.0.1:9090"


class DeploymentFileProviderSettings(ProviderToggle):
    path: Path = Path("data/deployments/deployments.json")


class ServiceCatalogProviderSettings(ProviderToggle):
    path: Path = Path("config/services.yaml")


class ProviderSettings(BaseModel):
    mock: ProviderToggle = ProviderToggle(enabled=True)
    log_file: LogFileProviderSettings = LogFileProviderSettings(enabled=True)
    prometheus: PrometheusProviderSettings = PrometheusProviderSettings(enabled=False)
    deployment_file: DeploymentFileProviderSettings = DeploymentFileProviderSettings(enabled=True)
    service_catalog: ServiceCatalogProviderSettings = ServiceCatalogProviderSettings(enabled=True)


class LlmSettings(BaseModel):
    enabled: bool = False


class AppSettings(BaseModel):
    storage: StorageSettings = StorageSettings()
    providers: ProviderSettings = ProviderSettings()
    llm: LlmSettings = LlmSettings()
```

Add `load_settings()` that:

1. Reads `DIAGOPS_CONFIG` or `config/diagops.yaml`.
2. Uses defaults when the file is missing.
3. Parses YAML when present.
4. Applies `DIAGOPS_DATABASE_URL`, `DIAGOPS_PROVIDER_MOCK_ENABLED`, and `DIAGOPS_LLM_ENABLED`.

- [ ] **Step 3: Add default config files**

Create `config/diagops.yaml`:

```yaml
storage:
  url: sqlite:///data/diagops.db

providers:
  mock:
    enabled: true
  log_file:
    enabled: true
    paths:
      - data/sample-logs/checkout-service.log
  prometheus:
    enabled: false
    base_url: http://127.0.0.1:9090
  deployment_file:
    enabled: true
    path: data/deployments/deployments.json
  service_catalog:
    enabled: true
    path: config/services.yaml

llm:
  enabled: false
```

Create `config/services.yaml`:

```yaml
services:
  checkout-service:
    owner: platform-team
    team: payments-platform
    runtime: python
    repository: https://example.invalid/checkout-service
    dependencies:
      - inventory-service
      - payment-db
    dashboards:
      - http://grafana.local/d/checkout
    runbooks:
      - http://runbooks.local/checkout
```

- [ ] **Step 4: Add dependency if needed**

If PyYAML is not already available, add it:

```powershell
uv add pyyaml
```

Expected: `pyproject.toml` and `uv.lock` update.

- [ ] **Step 5: Ignore generated local data**

Add to `.gitignore`:

```text
data/diagops.db
data/diagops.db-*
frontend/dist/
frontend/node_modules/
```

- [ ] **Step 6: Run tests**

```powershell
uv run pytest tests/config/test_settings.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add backend/config/settings.py config/diagops.yaml config/services.yaml tests/config/test_settings.py .gitignore pyproject.toml uv.lock
git commit -m "feat: add v3 configuration defaults"
```

---

### Task 2: SQLite Schema, Serialization, And Repository

**Files:**
- Create: `backend/db/schema.py`
- Create: `backend/db/session.py`
- Create: `backend/db/serialization.py`
- Create: `backend/db/sqlite_repository.py`
- Test: `tests/db/test_sqlite_repository.py`

- [ ] **Step 1: Write SQLite repository tests**

Create `tests/db/test_sqlite_repository.py` with tests for:

1. Schema initialization creates `schema_version`.
2. `save/get/list` round-trips a completed investigation.
3. A new repository instance can read the same SQLite file.
4. `update_action_status` persists action status and note.
5. `update_verification_status` persists verification status and result note.
6. Failed investigations persist `failure_reason`.

Use a temp database:

```python
def sqlite_url(tmp_path):
    return f"sqlite:///{tmp_path / 'diagops-test.db'}"
```

- [ ] **Step 2: Define SQLAlchemy tables**

Create `backend/db/schema.py`.

Required tables:

```text
schema_version
investigations
events
evidence_items
hypotheses
recommended_actions
verification_suggestions
reports
provider_results
specialist_results
llm_analyses
```

Use JSON columns from SQLAlchemy for SQLite-compatible JSON storage.

- [ ] **Step 3: Add session helpers**

Create `backend/db/session.py` with:

```python
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from backend.db.schema import metadata


def create_db_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, future=True, connect_args=connect_args)


def initialize_database(engine: Engine) -> None:
    metadata.create_all(engine)
```

Also insert schema version `3` if missing.

- [ ] **Step 4: Add serialization helpers**

Create `backend/db/serialization.py` with explicit conversion functions:

```text
record_to_rows(record)
rows_to_record(rows)
```

Avoid ad hoc string parsing. Use Pydantic `model_dump(mode="json")` for JSON payloads and model constructors for reading.

- [ ] **Step 5: Implement SQLite repository**

Create `backend/db/sqlite_repository.py` implementing the current repository methods:

```text
save(record)
get(investigation_id)
list()
update_status(investigation_id, status, failure_reason)
update_action_status(investigation_id, action_id, status, note)
update_verification_status(investigation_id, verification_id, status, result_note)
```

Preserve current exception behavior:

```text
ValueError("Unknown investigation: <id>")
ValueError("Unknown action: <id>")
ValueError("Unknown verification suggestion: <id>")
```

- [ ] **Step 6: Run tests**

```powershell
uv run pytest tests/db/test_sqlite_repository.py -v
uv run pytest tests/diagnosis/test_orchestrator.py tests/api/test_events_api.py tests/api/test_investigations_api.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add backend/db/schema.py backend/db/session.py backend/db/serialization.py backend/db/sqlite_repository.py tests/db/test_sqlite_repository.py
git commit -m "feat: add sqlite investigation repository"
```

---

### Task 3: Wire Persistent Storage Into The Application Container

**Files:**
- Modify: `backend/services/container.py`
- Modify: `backend/db/repositories.py` if a shared protocol is useful
- Test: `tests/api/test_persistence_api.py`

- [ ] **Step 1: Write persistence API tests**

Create `tests/api/test_persistence_api.py`.

Test that:

1. A container using a temp SQLite database creates an investigation.
2. A fresh container instance using the same database can list and get the investigation.
3. Action and verification status updates survive a fresh container instance.

- [ ] **Step 2: Update container wiring**

Modify `backend/services/container.py` so `AppContainer` accepts optional settings:

```python
class AppContainer:
    def __init__(self, settings: AppSettings | None = None) -> None:
        self.settings = settings or load_settings()
```

Use `SQLiteInvestigationRepository` when `settings.storage.url` starts with `sqlite`.

Keep test reset support:

```python
def reset_container(settings: AppSettings | None = None) -> AppContainer:
    global _container
    _container = AppContainer(settings=settings)
    return _container
```

- [ ] **Step 3: Preserve existing behavior**

Existing API tests should continue to pass. If tests need isolation, configure them to use temp SQLite settings or keep in-memory repository through explicit test container setup.

- [ ] **Step 4: Run tests**

```powershell
uv run pytest tests/api/test_persistence_api.py -v
uv run pytest tests/api tests/diagnosis -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/services/container.py backend/db/repositories.py tests/api/test_persistence_api.py
git commit -m "feat: wire sqlite persistence into container"
```

---

### Task 4: Persist Provider And Specialist Results

**Files:**
- Modify: `backend/db/models.py`
- Modify: `backend/diagnosis/orchestrator.py`
- Modify: `backend/db/sqlite_repository.py`
- Modify: `backend/db/serialization.py`
- Test: `tests/db/test_sqlite_repository.py`
- Test: `tests/diagnosis/test_orchestrator.py`

- [ ] **Step 1: Add model fields**

Extend `InvestigationRecord` with:

```python
provider_results: list[ProviderResult] = Field(default_factory=list)
specialist_results: list[SpecialistResult] = Field(default_factory=list)
```

- [ ] **Step 2: Add tests**

Add tests that:

1. Orchestrator completed records include provider and specialist results.
2. SQLite repository round-trips provider and specialist results.
3. Failed provider results are persisted.

- [ ] **Step 3: Update orchestrator**

After coordinator collection:

```python
record.provider_results = context.provider_results
record.specialist_results = context.specialist_results
```

Save the record before later steps that can fail.

- [ ] **Step 4: Update SQLite serialization**

Serialize provider and specialist results into their tables and restore them when reading records.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/db/test_sqlite_repository.py tests/diagnosis/test_orchestrator.py -v
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/db/models.py backend/diagnosis/orchestrator.py backend/db/sqlite_repository.py backend/db/serialization.py tests/db/test_sqlite_repository.py tests/diagnosis/test_orchestrator.py
git commit -m "feat: persist provider and specialist results"
```

---

### Task 5: File-Based Service Catalog Provider

**Files:**
- Create: `backend/providers/file_service_catalog.py`
- Modify: `backend/providers/registry.py`
- Test: `tests/providers/test_file_service_catalog.py`

- [ ] **Step 1: Write provider tests**

Create tests for:

1. Loading `config/services.yaml`.
2. Matching `checkout-service`.
3. Returning `SERVICE_METADATA` evidence with owner, team, repository, dependencies, dashboards, runbooks.
4. Missing service returns a skipped or low-confidence metadata evidence instead of failing the investigation.

- [ ] **Step 2: Implement provider**

Create `FileServiceCatalogProvider`.

Constructor:

```python
def __init__(self, path: Path) -> None:
    self.path = path
```

Provider attr:

```python
provider = EvidenceProvider.SERVICE_CATALOG
```

Return `ProviderResult`.

- [ ] **Step 3: Wire registry builder**

Add a configuration-based builder:

```python
def build_provider_registry_from_settings(settings: AppSettings) -> ProviderRegistry:
    providers = []
```

Include file service catalog provider when enabled.

- [ ] **Step 4: Run tests**

```powershell
uv run pytest tests/providers/test_file_service_catalog.py tests/providers/test_mock_providers.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/providers/file_service_catalog.py backend/providers/registry.py tests/providers/test_file_service_catalog.py
git commit -m "feat: add file service catalog provider"
```

---

### Task 6: Deployment File Provider

**Files:**
- Create: `backend/providers/file_deployments.py`
- Create: `data/deployments/deployments.json`
- Modify: `backend/providers/registry.py`
- Test: `tests/providers/test_file_deployments.py`

- [ ] **Step 1: Add sample deployment data**

Create `data/deployments/deployments.json` with records for:

1. `checkout-service` deployment near the deployment regression case.
2. An unrelated old deployment outside the time window.
3. A different service deployment.

- [ ] **Step 2: Write provider tests**

Test that:

1. Matching deployment returns `DEPLOYMENT` evidence.
2. Out-of-window deployment is ignored.
3. Missing file creates failed `ProviderResult` and error evidence through registry.

- [ ] **Step 3: Implement provider**

Create `FileDeploymentProvider`:

```python
provider = EvidenceProvider.DEPLOY
```

Read JSON with structured parsing, parse timestamps with `datetime.fromisoformat`, and filter by event service, environment, and `time_window_minutes`.

- [ ] **Step 4: Wire registry builder**

Add deployment provider when `settings.providers.deployment_file.enabled` is true.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/providers/test_file_deployments.py tests/providers/test_mock_providers.py -v
uv run pytest tests/rca/test_analyzer.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/providers/file_deployments.py data/deployments/deployments.json backend/providers/registry.py tests/providers/test_file_deployments.py
git commit -m "feat: add deployment file provider"
```

---

### Task 7: Local Log File Provider

**Files:**
- Create: `backend/providers/file_logs.py`
- Create: `data/sample-logs/checkout-service.log`
- Modify: `backend/providers/registry.py`
- Test: `tests/providers/test_file_logs.py`

- [ ] **Step 1: Add sample log data**

Create `data/sample-logs/checkout-service.log` with timestamped lines containing:

1. `NullPointerException`.
2. HTTP 500.
3. A normal info line.
4. An unrelated service line.

- [ ] **Step 2: Write provider tests**

Test that:

1. Error patterns become `LOG_PATTERN` evidence.
2. Evidence payload contains `error_count`, `sample_lines`, and `patterns`.
3. Missing log file produces provider failure through registry.
4. Log provider does not include lines outside service/time filters when timestamps are present.

- [ ] **Step 3: Implement provider**

Create `FileLogProvider`:

```python
provider = EvidenceProvider.LOG
```

Rules:

1. Read configured paths.
2. Match event service string.
3. Match error keywords: `ERROR`, `Exception`, `Traceback`, `HTTP 500`, `5xx`.
4. Return one aggregated evidence item per file when matches exist.
5. Return success with empty evidence when no matches exist.

- [ ] **Step 4: Wire registry builder**

Add log file provider when enabled.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/providers/test_file_logs.py tests/providers/test_mock_providers.py -v
uv run pytest tests/rca/test_analyzer.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/providers/file_logs.py data/sample-logs/checkout-service.log backend/providers/registry.py tests/providers/test_file_logs.py
git commit -m "feat: add local log file provider"
```

---

### Task 8: Prometheus Provider With Failure Downgrade

**Files:**
- Create: `backend/providers/prometheus.py`
- Modify: `backend/providers/registry.py`
- Test: `tests/providers/test_prometheus_provider.py`

- [ ] **Step 1: Write tests using monkeypatch**

Test that:

1. Disabled Prometheus provider is not added by settings builder.
2. Successful mocked HTTP responses create `METRIC_TREND` evidence.
3. Connection failure returns failed provider result through registry and does not fail investigation.
4. Evidence payload contains query names and observed values.

- [ ] **Step 2: Implement provider**

Use standard library `urllib.request` to avoid new dependencies.

Provider:

```python
provider = EvidenceProvider.METRIC
```

Queries:

```text
qps
5xx_rate
p95_latency
cpu
memory
```

V3 may use simple query strings from config or hard-coded safe defaults.

- [ ] **Step 3: Wire registry builder**

Only add Prometheus provider when enabled.

- [ ] **Step 4: Run tests**

```powershell
uv run pytest tests/providers/test_prometheus_provider.py tests/providers/test_mock_providers.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add backend/providers/prometheus.py backend/providers/registry.py tests/providers/test_prometheus_provider.py
git commit -m "feat: add optional prometheus provider"
```

---

### Task 9: Investigation Detail API Expansion

**Files:**
- Modify: `backend/api/investigations.py`
- Create: `backend/api/config.py`
- Modify: `backend/main.py`
- Test: `tests/api/test_v3_investigation_detail_api.py`

- [ ] **Step 1: Write API tests**

Create tests for:

1. `GET /investigations/{id}/timeline`.
2. `GET /investigations/{id}/evidence`.
3. `GET /investigations/{id}/evidence?provider=log`.
4. `GET /investigations/{id}/provider-results`.
5. `GET /investigations/{id}/specialist-results`.
6. `GET /investigations/{id}/report`.
7. `GET /config/providers`.

- [ ] **Step 2: Implement endpoints**

Add routes:

```text
GET /investigations/{id}/timeline
GET /investigations/{id}/evidence
GET /investigations/{id}/provider-results
GET /investigations/{id}/specialist-results
GET /investigations/{id}/report
```

Return 404 for unknown investigation.

- [ ] **Step 3: Add config API**

Create `backend/api/config.py`:

```text
GET /config/providers
```

Return provider names, enabled flags, and safe non-secret paths or URLs. Do not return secrets.

- [ ] **Step 4: Wire router**

Include config router in `backend/main.py`.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/api/test_v3_investigation_detail_api.py tests/api/test_investigations_api.py -v
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/api/investigations.py backend/api/config.py backend/main.py tests/api/test_v3_investigation_detail_api.py
git commit -m "feat: add v3 investigation detail api"
```

---

### Task 10: Golden Cases Through SQLite And Real Providers

**Files:**
- Modify: `tests/golden/test_golden_cases.py`
- Create: `tests/golden/test_v3_persistent_golden.py`

- [ ] **Step 1: Add persistent golden test**

Create `tests/golden/test_v3_persistent_golden.py`.

Test:

1. Use temp SQLite database.
2. Build container with file providers and mock provider enabled.
3. Run `deployment_regression`.
4. Create a new SQLite repository instance.
5. Read the same investigation.
6. Assert status completed, report exists, actions exist, verifications exist, provider results exist, specialist results exist.

- [ ] **Step 2: Preserve existing golden tests**

Do not remove existing primary-cause and evidence assertions.

- [ ] **Step 3: Run tests**

```powershell
uv run pytest tests/golden/test_golden_cases.py tests/golden/test_v3_persistent_golden.py -v
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add tests/golden/test_golden_cases.py tests/golden/test_v3_persistent_golden.py
git commit -m "test: add v3 persistent golden coverage"
```

---

### Task 11: Read-Only LLM Analyst Domain And Stub

**Files:**
- Create: `backend/domain/llm_analysis.py`
- Create: `backend/diagnosis/llm_analyst.py`
- Modify: `backend/db/models.py`
- Modify: `backend/diagnosis/orchestrator.py`
- Test: `tests/diagnosis/test_llm_analyst.py`

- [ ] **Step 1: Write LLM analyst tests**

Test that:

1. Disabled analyst returns no analysis.
2. Stub analyst references only existing evidence IDs.
3. Invalid referenced evidence ID raises validation error.
4. Orchestrator does not call analyst when config disabled.

- [ ] **Step 2: Add domain model**

Create `LLMAnalysis` with:

```text
id
investigation_id
summary
missing_evidence
risk_notes
suggested_questions
referenced_evidence_ids
created_at
```

- [ ] **Step 3: Add read-only stub service**

Create `ReadOnlyLlmAnalyst`.

Default behavior when disabled:

```python
return None
```

Stub behavior when enabled without an external LLM key:

```text
Generate deterministic evidence-grounded missing evidence suggestions.
```

Do not call any network API in V3 unless a later task explicitly configures it.

- [ ] **Step 4: Wire model storage**

Add `llm_analysis: LLMAnalysis | None = None` to `InvestigationRecord` if needed.

Persist it in SQLite if present.

- [ ] **Step 5: Run tests**

```powershell
uv run pytest tests/diagnosis/test_llm_analyst.py -v
uv run pytest tests/diagnosis/test_orchestrator.py tests/db/test_sqlite_repository.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add backend/domain/llm_analysis.py backend/diagnosis/llm_analyst.py backend/db/models.py backend/diagnosis/orchestrator.py tests/diagnosis/test_llm_analyst.py
git commit -m "feat: add read-only llm analyst stub"
```

---

### Task 12: Web UI First Version

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/api.ts`
- Create: `frontend/src/styles.css`
- Create: `frontend/tsconfig.json`
- Create: `frontend/vite.config.ts`
- Create: `tests/frontend/test_frontend_smoke.py`
- Modify: `README.md`

- [ ] **Step 1: Set npm cache to D drive**

```powershell
npm config set cache D:\agent\.npm-cache --global
npm config get cache
```

Expected:

```text
D:\agent\.npm-cache
```

- [ ] **Step 2: Scaffold frontend**

Create a Vite React TypeScript app under `frontend`.

Install dependencies from `frontend`:

```powershell
npm create vite@latest . -- --template react-ts
npm install
npm install @tanstack/react-query
```

Expected:

```text
frontend/node_modules exists under D:\agent\sre-agent\frontend
```

- [ ] **Step 3: Implement UI**

Minimum UI:

1. Investigation list.
2. Manual investigation form.
3. Investigation detail.
4. Evidence list.
5. Hypotheses.
6. Recommended actions with status update.
7. Verification suggestions with result update.
8. Markdown report display.

Do not add controls that execute remediation.

- [ ] **Step 4: Add frontend smoke test**

Create `tests/frontend/test_frontend_smoke.py` that checks:

1. `frontend/package.json` exists.
2. `frontend/src/App.tsx` contains investigation list/detail UI strings.
3. No UI text claims that rollback, restart, scale, or config changes are executed automatically.

- [ ] **Step 5: Build frontend**

```powershell
cd frontend
npm run build
```

Expected:

```text
frontend/dist exists
```

- [ ] **Step 6: Run tests**

```powershell
uv run pytest tests/frontend/test_frontend_smoke.py -v
uv run pytest -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add frontend tests/frontend/test_frontend_smoke.py README.md package-lock.json
git commit -m "feat: add v3 investigation web ui"
```

If npm creates `frontend/package-lock.json`, stage that file instead of root `package-lock.json`.

---

### Task 13: README, Local Data, And V3 Operational Docs

**Files:**
- Modify: `README.md`
- Create: `docs/examples/v3-manual-investigation.json`
- Create: `docs/examples/v3-provider-config.yaml`

- [ ] **Step 1: Update README**

Add sections:

1. V3 overview.
2. SQLite database path.
3. D-drive cache and data path rules.
4. Provider configuration.
5. Starting backend.
6. Starting frontend.
7. Running with sample log and deployment data.
8. Read-only safety boundary.

- [ ] **Step 2: Add examples**

Create `docs/examples/v3-manual-investigation.json`:

```json
{
  "text": "checkout-service has many 500s after 14:00",
  "service": "checkout-service",
  "environment": "prod"
}
```

Create `docs/examples/v3-provider-config.yaml` mirroring `config/diagops.yaml`.

- [ ] **Step 3: Run docs-related smoke tests**

```powershell
uv run pytest tests/api/test_events_api.py tests/api/test_investigations_api.py tests/golden/test_v3_persistent_golden.py -v
uv run ruff check .
```

Expected: PASS.

- [ ] **Step 4: Commit**

```powershell
git add README.md docs/examples/v3-manual-investigation.json docs/examples/v3-provider-config.yaml
git commit -m "docs: document v3 local platform usage"
```

---

### Task 14: Final Verification And Push

**Files:**
- No new feature files.

- [ ] **Step 1: Check dependency locations**

```powershell
uv cache dir
npm config get cache
Get-Command node
```

Expected:

```text
D:\agent\.uv-cache
D:\agent\.npm-cache
D:\paiflow\nodejs\node.exe
```

- [ ] **Step 2: Run backend lint and tests**

```powershell
uv run ruff check .
uv run pytest -v
```

Expected: PASS. The existing Starlette/TestClient deprecation warning is acceptable.

- [ ] **Step 3: Build frontend**

```powershell
cd frontend
npm run build
```

Expected: PASS.

- [ ] **Step 4: Start backend**

```powershell
uv run uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

Expected:

```text
Uvicorn running on http://127.0.0.1:8000
```

- [ ] **Step 5: Manual API smoke**

In another terminal:

```powershell
curl http://127.0.0.1:8000/health
curl -X POST http://127.0.0.1:8000/events/simulated/deployment_regression
curl http://127.0.0.1:8000/investigations
curl http://127.0.0.1:8000/config/providers
```

Expected:

1. Health returns `{"status":"ok"}`.
2. Simulated event returns `status=completed`, `action_count>=1`, `verification_count>=1`.
3. Investigation list contains the created investigation.
4. Provider config endpoint returns enabled provider names without secrets.

- [ ] **Step 6: Restart persistence smoke**

1. Stop backend.
2. Start backend again.
3. Query the previous investigation id.

Expected:

```text
The investigation still exists after restart.
```

- [ ] **Step 7: UI smoke**

Start frontend:

```powershell
cd frontend
npm run dev -- --host 127.0.0.1
```

Open the shown local URL and confirm:

1. Investigation list loads.
2. Detail page opens.
3. Action status update works.
4. Verification status update works.
5. UI does not present auto-remediation execution.

- [ ] **Step 8: Check git status**

```powershell
git status -sb
```

Expected:

```text
working tree clean
```

- [ ] **Step 9: Push branch**

```powershell
git push origin codex/mvp-backend
```

Expected:

```text
codex/mvp-backend pushed
```

---

## Self-Review Checklist

- [ ] Every task has concrete files.
- [ ] Every task has verification commands.
- [ ] No production write actions are introduced.
- [ ] SQLite is the default persistence layer.
- [ ] PostgreSQL remains a future-compatible option, not a V3 requirement.
- [ ] D-drive cache and data paths are documented.
- [ ] Existing V2 APIs remain backward compatible.
- [ ] Provider failures downgrade to provider error evidence.
- [ ] LLM Analyst is disabled by default and read-only.
- [ ] UI does not expose rollback, restart, scale, or config execution.
- [ ] Final verification includes backend tests, frontend build, API smoke, persistence smoke, and git push.
