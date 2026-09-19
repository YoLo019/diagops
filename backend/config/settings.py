import hashlib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.domain.multi_agent import InvestigationStrategy, ModelProvider
from backend.domain.runtime import (
    V11_DEFAULT_MAX_TOOL_CALLS_PER_SPECIALIST,
    V11_DEFAULT_MAX_TURNS,
    V11_DEFAULT_TOOL_BUDGET,
)

_LOCAL_CLEARTEXT_HOSTS = {"localhost", "127.0.0.1", "::1"}


def canonicalize_endpoint(value: str) -> str:
    """openai_compatible base_url 的唯一 canonicalization 算法（spec 7.8）。

    只接受绝对 http/https URL；拒绝 userinfo/query/fragment、百分号编码 path、
    重复斜杠与 ./.. 段；scheme 与 IDNA host 小写化；去除默认端口（http 80、
    https 443），保留非默认端口；去除 path 尾部斜杠。http 仅允许本机地址。
    """
    raw = value.strip()
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError("endpoint must use absolute http or https URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("endpoint must not contain userinfo")
    if parts.query or parts.fragment:
        raise ValueError("endpoint must not contain query or fragment")
    hostname = parts.hostname
    if not hostname:
        raise ValueError("endpoint requires a host")
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("endpoint port is invalid") from exc
    if ":" in hostname:
        # IPv6 字面量保持小写并加回括号。
        host = f"[{hostname.lower()}]"
        host_key = hostname.lower()
    else:
        try:
            host = hostname.encode("idna").decode("ascii").lower()
        except (UnicodeError, ValueError) as exc:
            raise ValueError("endpoint host is not valid IDNA") from exc
        host_key = host
    if scheme == "http" and host_key not in _LOCAL_CLEARTEXT_HOSTS:
        raise ValueError("cleartext http endpoints are only allowed for local hosts")
    if (scheme, port) in {("http", 80), ("https", 443)}:
        port = None
    path = parts.path
    if "%" in path:
        raise ValueError("endpoint path must not contain percent-encoded bytes")
    if "//" in path:
        raise ValueError("endpoint path must not contain duplicate slashes")
    segments = [segment for segment in path.split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("endpoint path must not contain dot segments")
    path = "/" + "/".join(segments) if segments else ""
    netloc = host if port is None else f"{host}:{port}"
    return f"{scheme}://{netloc}{path}"


def endpoint_id(canonical_url: str) -> str:
    """canonical UTF-8 URL 的 SHA-256 身份；不含任何凭证信息。"""
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


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


class LocalPackageProviderSettings(ProviderToggle):
    """离线 incident package 根目录；缺席 source 绝不回退 mock。"""

    path: Path = Path("data/incident-packages/default")


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
    local_package: LocalPackageProviderSettings = Field(
        default_factory=lambda: LocalPackageProviderSettings(enabled=False)
    )


class OpenAICompatibleSettings(BaseModel):
    """generic Chat Completions endpoint 配置；API key 只允许环境变量。"""

    model_config = ConfigDict(validate_assignment=True)

    base_url: str | None = None
    api_mode: Literal["chat_completions"] = "chat_completions"
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return canonicalize_endpoint(value)


class AgentsSettings(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    enabled: bool = False
    provider: ModelProvider = ModelProvider.OPENAI
    model: str | None = None
    max_turns: int = Field(default=V11_DEFAULT_MAX_TURNS, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)
    token_budget: int = Field(default=12000, ge=1, le=1_000_000)
    strategy: InvestigationStrategy = InvestigationStrategy.FIXED
    max_tool_calls_per_specialist: int = Field(
        default=V11_DEFAULT_MAX_TOOL_CALLS_PER_SPECIALIST, ge=1, le=10
    )
    max_total_tool_calls: int = Field(default=V11_DEFAULT_TOOL_BUDGET, ge=1, le=30)
    tool_timeout_seconds: int = Field(default=10, ge=1, le=60)
    openai_compatible: OpenAICompatibleSettings = Field(
        default_factory=OpenAICompatibleSettings
    )


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

    if value := _get_env("DIAGOPS_AGENTS_TOKEN_BUDGET"):
        settings.agents.token_budget = int(value)

    if agents_strategy := _get_env("DIAGOPS_AGENTS_STRATEGY"):
        settings.agents.strategy = agents_strategy.strip().lower()

    if value := _get_env("DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST"):
        settings.agents.max_tool_calls_per_specialist = int(value)

    if value := _get_env("DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS"):
        settings.agents.max_total_tool_calls = int(value)

    if value := _get_env("DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS"):
        settings.agents.tool_timeout_seconds = int(value)

    if value := _get_env("DIAGOPS_AGENTS_OPENAI_COMPATIBLE_BASE_URL"):
        settings.agents.openai_compatible.base_url = value

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
