from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


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


class LlmSettings(BaseModel):
    enabled: bool = False


class ReActSettings(BaseModel):
    enabled: bool = False
    max_steps: int = 5


class AppSettings(BaseModel):
    storage: StorageSettings = Field(default_factory=StorageSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    react: ReActSettings = Field(default_factory=ReActSettings)


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

    if llm_enabled := _get_env("DIAGOPS_LLM_ENABLED"):
        settings.llm.enabled = _parse_bool(llm_enabled)

    if react_enabled := _get_env("DIAGOPS_REACT_ENABLED"):
        settings.react.enabled = _parse_bool(react_enabled)


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
