from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from backend.domain.multi_agent import InvestigationStrategy, ModelProvider


class StorageSettings(BaseModel):
    url: str = "sqlite:///data/diagops.db"


class ProviderToggle(BaseModel):
    enabled: bool = False


class LogFileProviderSettings(ProviderToggle):
    paths: list[Path] = Field(
        default_factory=lambda: [Path("data/sample-logs/checkout-service.log")]
    )


class PrometheusProviderSettings(ProviderToggle):
    base_url: str = "http://127.0.0.1:9090"


class DeploymentFileProviderSettings(ProviderToggle):
    path: Path = Path("data/deployments/deployments.json")


class ServiceCatalogProviderSettings(ProviderToggle):
    path: Path = Path("config/services.yaml")


class ProviderSettings(BaseModel):
    mock: ProviderToggle = Field(default_factory=lambda: ProviderToggle(enabled=True))
    log_file: LogFileProviderSettings = Field(
        default_factory=lambda: LogFileProviderSettings(enabled=True)
    )
    prometheus: PrometheusProviderSettings = Field(
        default_factory=lambda: PrometheusProviderSettings(enabled=False)
    )
    deployment_file: DeploymentFileProviderSettings = Field(
        default_factory=lambda: DeploymentFileProviderSettings(enabled=True)
    )
    service_catalog: ServiceCatalogProviderSettings = Field(
        default_factory=lambda: ServiceCatalogProviderSettings(enabled=True)
    )


class AgentsSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = False
    provider: ModelProvider = ModelProvider.OPENAI
    model: str | None = None
    max_turns: int = Field(default=8, ge=1)
    timeout_seconds: int = Field(default=60, ge=1)
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED
    max_tool_calls_per_specialist: int = Field(default=3, ge=1, le=10)
    max_total_tool_calls: int = Field(default=8, ge=1, le=30)
    tool_timeout_seconds: int = Field(default=10, ge=1, le=60)


class BenchmarkSettings(BaseModel):
    results_path: Path = Path("output/benchmarks/openrca")


class OpenTelemetrySettings(BaseModel):
    enabled: bool = False
    endpoint: str | None = None


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = True
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    max_parallel_steps_per_run: int = Field(default=3, ge=1, le=16)
    lease_seconds: int = Field(default=30, ge=5, le=3600)
    heartbeat_seconds: int = Field(default=10, ge=1, le=300)
    opentelemetry: OpenTelemetrySettings = Field(default_factory=OpenTelemetrySettings)


class AppSettings(BaseModel):
    storage: StorageSettings = Field(default_factory=StorageSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    agents: AgentsSettings = Field(default_factory=AgentsSettings)
    benchmark: BenchmarkSettings = Field(default_factory=BenchmarkSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)


def load_settings() -> AppSettings:
    config_path = Path(_get_env("DIAGOPS_CONFIG", "config/diagops.yaml"))
    config_data: dict[str, Any] = {}
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            config_data = loaded

    settings = AppSettings.model_validate(config_data)
    _apply_environment_overrides(settings)
    return settings


def _apply_environment_overrides(settings: AppSettings) -> None:
    if database_url := _get_env("DIAGOPS_DATABASE_URL"):
        settings.storage.url = database_url

    if mock_enabled := _get_env("DIAGOPS_PROVIDER_MOCK_ENABLED"):
        settings.providers.mock.enabled = _parse_bool(mock_enabled)

    if agents_enabled := _get_env("DIAGOPS_AGENTS_ENABLED"):
        settings.agents.enabled = _parse_bool(agents_enabled)

    if agents_provider := _get_env("DIAGOPS_AGENTS_PROVIDER"):
        settings.agents.provider = agents_provider.strip().lower()

    if agents_model := _get_env("DIAGOPS_AGENTS_MODEL"):
        settings.agents.model = agents_model

    if agents_max_turns := _get_env("DIAGOPS_AGENTS_MAX_TURNS"):
        settings.agents.max_turns = int(agents_max_turns)

    if agents_timeout_seconds := _get_env("DIAGOPS_AGENTS_TIMEOUT_SECONDS"):
        settings.agents.timeout_seconds = int(agents_timeout_seconds)

    if agents_strategy := _get_env("DIAGOPS_AGENTS_STRATEGY"):
        settings.agents.strategy = agents_strategy.strip().lower()

    if value := _get_env("DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST"):
        settings.agents.max_tool_calls_per_specialist = int(value)

    if value := _get_env("DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS"):
        settings.agents.max_total_tool_calls = int(value)

    if value := _get_env("DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS"):
        settings.agents.tool_timeout_seconds = int(value)

    if value := _get_env("DIAGOPS_RUNTIME_ENABLED"):
        settings.runtime.enabled = _parse_bool(value)

    if value := _get_env("DIAGOPS_RUNTIME_MAX_CONCURRENT_RUNS"):
        settings.runtime.max_concurrent_runs = int(value)

    if value := _get_env("DIAGOPS_RUNTIME_MAX_PARALLEL_STEPS_PER_RUN"):
        settings.runtime.max_parallel_steps_per_run = int(value)

    if value := _get_env("DIAGOPS_RUNTIME_LEASE_SECONDS"):
        settings.runtime.lease_seconds = int(value)

    if value := _get_env("DIAGOPS_RUNTIME_HEARTBEAT_SECONDS"):
        settings.runtime.heartbeat_seconds = int(value)

    if value := _get_env("DIAGOPS_RUNTIME_OTEL_ENABLED"):
        settings.runtime.opentelemetry.enabled = _parse_bool(value)

    if value := _get_env("DIAGOPS_RUNTIME_OTEL_ENDPOINT"):
        settings.runtime.opentelemetry.endpoint = value


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")


def _get_env(name: str, default: str | None = None) -> str | None:
    from os import environ

    return environ.get(name, default)
