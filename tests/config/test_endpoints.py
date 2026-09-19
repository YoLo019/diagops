"""openai_compatible base_url canonicalization 的接受/拒绝向量（spec 7.8）。"""

import pytest

from backend.config.settings import (
    AgentsSettings,
    AppSettings,
    OpenAICompatibleSettings,
    canonicalize_endpoint,
    endpoint_id,
    load_settings,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://EXAMPLE.COM:443/v1/", "https://example.com/v1"),
        ("http://127.0.0.1:8000/v1/", "http://127.0.0.1:8000/v1"),
        ("https://example.com", "https://example.com"),
        ("https://example.com/", "https://example.com"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://[::1]:9000/v1", "http://[::1]:9000/v1"),
        ("https://example.com:8443/v1", "https://example.com:8443/v1"),
        ("https://bücher.example/v1", "https://xn--bcher-kva.example/v1"),
    ],
)
def test_canonicalization_accepted_vectors(raw, expected):
    assert canonicalize_endpoint(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://user:pass@example.com/v1",
        "https://user@example.com/v1",
        "https://example.com/v1?token=abc",
        "https://example.com/v1#frag",
        "https://example.com/%2e%2e/v1",
        "https://example.com//v1",
        "https://example.com/../v1",
        "https://example.com/./v1",
        "ftp://example.com/v1",
        "example.com/v1",
        "https:///v1",
        "http://example.com/v1",
        "http://192.168.1.10:8000/v1",
        "https://example.com:99999/v1",
        "https://example.com:notaport/v1",
    ],
)
def test_canonicalization_rejected_vectors(raw):
    with pytest.raises(ValueError):
        canonicalize_endpoint(raw)


def test_endpoint_id_is_sha256_of_canonical_url():
    first = endpoint_id(canonicalize_endpoint("HTTPS://EXAMPLE.COM:443/v1/"))
    second = endpoint_id("https://example.com/v1")

    assert first == second
    assert len(first) == 64 and all(char in "0123456789abcdef" for char in first)


def test_settings_validate_and_store_canonical_base_url():
    settings = OpenAICompatibleSettings(base_url="HTTPS://EXAMPLE.COM:443/v1/")

    assert settings.base_url == "https://example.com/v1"
    assert settings.api_mode == "chat_completions"

    with pytest.raises(ValueError):
        OpenAICompatibleSettings(base_url="https://user:pass@example.com/v1")


def test_environment_override_applies_canonicalization(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.setenv(
        "DIAGOPS_AGENTS_OPENAI_COMPATIBLE_BASE_URL", "HTTPS://EXAMPLE.COM:443/v1/"
    )

    settings = load_settings()

    assert settings.agents.openai_compatible.base_url == "https://example.com/v1"


def test_default_settings_keep_openai_compatible_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setenv("DIAGOPS_CONFIG", str(tmp_path / "missing.yaml"))
    monkeypatch.delenv("DIAGOPS_AGENTS_OPENAI_COMPATIBLE_BASE_URL", raising=False)
    monkeypatch.delenv("DIAGOPS_AGENTS_PROVIDER", raising=False)

    settings = load_settings()

    assert settings.agents.openai_compatible.base_url is None
    assert settings.agents.provider.value == "openai"


def test_api_key_never_enters_settings():
    settings = AppSettings()

    assert "key" not in settings.agents.openai_compatible.model_dump()
    assert "DIAGOPS_AGENTS_API_KEY" not in str(
        AgentsSettings().model_dump(mode="json")
    )
