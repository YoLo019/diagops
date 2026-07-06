from pathlib import Path

from backend.config.settings import AppSettings, load_settings


def test_default_settings_use_d_drive_project_paths(monkeypatch):
    monkeypatch.delenv("DIAGOPS_CONFIG", raising=False)
    monkeypatch.delenv("DIAGOPS_DATABASE_URL", raising=False)
    monkeypatch.delenv("DIAGOPS_PROVIDER_MOCK_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_LLM_ENABLED", raising=False)

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


def test_environment_boolean_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv("DIAGOPS_PROVIDER_MOCK_ENABLED", "false")
    monkeypatch.setenv("DIAGOPS_LLM_ENABLED", "true")

    settings = load_settings()

    assert settings.providers.mock.enabled is False
    assert settings.llm.enabled is True


def test_missing_config_file_uses_safe_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("DIAGOPS_PROVIDER_MOCK_ENABLED", raising=False)
    monkeypatch.delenv("DIAGOPS_LLM_ENABLED", raising=False)

    settings = load_settings()

    assert isinstance(settings, AppSettings)
    assert settings.providers.prometheus.enabled is False
