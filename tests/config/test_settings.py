from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config.settings import AppSettings, load_settings
from backend.domain.multi_agent import InvestigationStrategy, ModelProvider


def test_default_settings_use_d_drive_project_paths(monkeypatch):
    monkeypatch.delenv("DIAGOPS_CONFIG", raising=False)
    monkeypatch.delenv("DIAGOPS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DIAGOPS_PROVIDER_MOCK_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_LLM_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_REACT_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_PROVIDER", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_MODEL", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_MAX_TURNS", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_STRATEGY", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS", raising=False)

    settings = load_settings()

    assert settings.storage.url == "sqlite:///data/diagops.db"
    assert settings.providers.mock.enabled is True
    assert settings.providers.service_catalog.path == Path("config/services.yaml")
    assert settings.providers.deployment_file.path == Path("data/deployments/deployments.json")
    assert settings.providers.log_file.paths == [Path("data/sample-logs/checkout-service.log")]
    assert not hasattr(settings, "llm")
    assert not hasattr(settings, "react")
    assert settings.agents.enabled is False
    assert settings.agents.provider == ModelProvider.OPENAI
    assert settings.agents.model is None
    assert settings.agents.max_turns == 16
    assert settings.agents.timeout_seconds == 120
    assert settings.agents.strategy == InvestigationStrategy.FIXED
    assert settings.agents.max_tool_calls_per_specialist == 3
    assert settings.agents.max_total_tool_calls == 8
    assert settings.agents.tool_timeout_seconds == 10


def test_environment_database_url_override(monkeypatch, tmp_path):
    config_path = tmp_path / "diagops.yaml"
    config_path.write_text("storage:\n  url: sqlite:///old.db\n", encoding="utf-8")
    monkeypatch.setenv("DIAGOPS_CONFIG", str(config_path))
    monkeypatch.setenv("DIAGOPS_DATABASE_URL", "sqlite:///data/test.db")

    settings = load_settings()

    assert settings.storage.url == "sqlite:///data/test.db"


def test_environment_boolean_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DIAGOPS_PROVIDER_MOCK_ENABLED", "false")
    monkeypatch.setenv("DIAGOPS_LLM_ENABLED", "true")
    monkeypatch.setenv("DIAGOPS_REACT_ENABLED", "true")
    monkeypatch.setenv("DIAGOPS_AGENTS_ENABLED", "true")
    monkeypatch.setenv("DIAGOPS_AGENTS_MODEL", "test-model")
    monkeypatch.setenv("DIAGOPS_AGENTS_MAX_TURNS", "12")
    monkeypatch.setenv("DIAGOPS_AGENTS_TIMEOUT_SECONDS", "90")

    settings = load_settings()

    assert settings.providers.mock.enabled is False
    assert not hasattr(settings, "llm")
    assert not hasattr(settings, "react")
    assert settings.agents.enabled is True
    assert settings.agents.model == "test-model"
    assert settings.agents.max_turns == 12
    assert settings.agents.timeout_seconds == 90


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DIAGOPS_AGENTS_MAX_TURNS", "0"),
        ("DIAGOPS_AGENTS_TIMEOUT_SECONDS", "-1"),
    ],
)
def test_invalid_agents_environment_limits_are_rejected(monkeypatch, tmp_path, name, value):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        load_settings()


def test_missing_config_file_uses_safe_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("DIAGOPS_PROVIDER_MOCK_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_LLM_ENABLED", raising=False)

    settings = load_settings()

    assert isinstance(settings, AppSettings)
    assert settings.providers.prometheus.enabled is False


def test_agents_provider_defaults_to_openai(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("DIAGOPS_AGENTS_PROVIDER", raising=False)

    assert load_settings().agents.provider == ModelProvider.OPENAI


def test_agents_provider_environment_selects_deepseek(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DIAGOPS_AGENTS_PROVIDER", "deepseek")

    assert load_settings().agents.provider == ModelProvider.DEEPSEEK


def test_invalid_agents_provider_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DIAGOPS_AGENTS_PROVIDER", "compatible")

    with pytest.raises(ValidationError):
        load_settings()


def test_adaptive_agent_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DIAGOPS_AGENTS_STRATEGY", "adaptive")
    monkeypatch.setenv("DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST", "5")
    monkeypatch.setenv("DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS", "12")
    monkeypatch.setenv("DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS", "20")

    settings = load_settings()

    assert settings.agents.strategy == InvestigationStrategy.ADAPTIVE
    assert settings.agents.max_tool_calls_per_specialist == 5
    assert settings.agents.max_total_tool_calls == 12
    assert settings.agents.tool_timeout_seconds == 20


@pytest.mark.parametrize(
    "name, value",
    [
        ("DIAGOPS_AGENTS_STRATEGY", "automatic"),
        ("DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST", "0"),
        ("DIAGOPS_AGENTS_MAX_TOOL_CALLS_PER_SPECIALIST", "11"),
        ("DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS", "0"),
        ("DIAGOPS_AGENTS_MAX_TOTAL_TOOL_CALLS", "31"),
        ("DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS", "0"),
        ("DIAGOPS_AGENTS_TOOL_TIMEOUT_SECONDS", "61"),
    ],
)
def test_invalid_adaptive_agent_environment_is_rejected(
    monkeypatch, tmp_path, name, value
):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        load_settings()


def test_runtime_settings_defaults_are_bounded_and_otel_is_off(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))

    settings = load_settings()

    assert settings.runtime.enabled is True
    assert settings.runtime.max_concurrent_runs == 4
    assert settings.runtime.max_parallel_steps_per_run == 3
    assert settings.runtime.lease_seconds == 30
    assert settings.runtime.heartbeat_seconds == 10
    assert settings.runtime.opentelemetry.enabled is False
    assert settings.runtime.opentelemetry.endpoint is None


def test_runtime_environment_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DIAGOPS_RUNTIME_ENABLED", "false")
    monkeypatch.setenv("DIAGOPS_RUNTIME_MAX_CONCURRENT_RUNS", "8")
    monkeypatch.setenv("DIAGOPS_RUNTIME_MAX_PARALLEL_STEPS_PER_RUN", "6")
    monkeypatch.setenv("DIAGOPS_RUNTIME_LEASE_SECONDS", "60")
    monkeypatch.setenv("DIAGOPS_RUNTIME_HEARTBEAT_SECONDS", "15")
    monkeypatch.setenv("DIAGOPS_RUNTIME_OTEL_ENABLED", "true")
    monkeypatch.setenv("DIAGOPS_RUNTIME_OTEL_ENDPOINT", "http://localhost:4318/v1/traces")

    settings = load_settings()

    assert settings.runtime.enabled is False
    assert settings.runtime.max_concurrent_runs == 8
    assert settings.runtime.max_parallel_steps_per_run == 6
    assert settings.runtime.lease_seconds == 60
    assert settings.runtime.heartbeat_seconds == 15
    assert settings.runtime.opentelemetry.enabled is True
    assert settings.runtime.opentelemetry.endpoint == "http://localhost:4318/v1/traces"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DIAGOPS_RUNTIME_MAX_CONCURRENT_RUNS", "0"),
        ("DIAGOPS_RUNTIME_MAX_CONCURRENT_RUNS", "33"),
        ("DIAGOPS_RUNTIME_MAX_PARALLEL_STEPS_PER_RUN", "0"),
        ("DIAGOPS_RUNTIME_MAX_PARALLEL_STEPS_PER_RUN", "17"),
        ("DIAGOPS_RUNTIME_LEASE_SECONDS", "4"),
        ("DIAGOPS_RUNTIME_LEASE_SECONDS", "3601"),
        ("DIAGOPS_RUNTIME_HEARTBEAT_SECONDS", "0"),
        ("DIAGOPS_RUNTIME_HEARTBEAT_SECONDS", "301"),
    ],
)
def test_invalid_runtime_environment_limits_are_rejected(
    monkeypatch, tmp_path, name, value
):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        load_settings()
