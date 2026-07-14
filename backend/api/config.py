from pathlib import Path
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.diagnosis.deepseek_model import implementation_status
from backend.domain.multi_agent import ModelProvider
from backend.services.container import get_container
from backend.services.reliability_artifacts import latest_certification

router = APIRouter(prefix="/config", tags=["config"])


class ProviderConfig(BaseModel):
    name: str
    enabled: bool
    config: dict[str, str | list[str]] = Field(default_factory=dict)


class ProvidersConfigResponse(BaseModel):
    providers: list[ProviderConfig]


class AgentConfigResponse(BaseModel):
    provider: ModelProvider
    model: str | None
    implementation_status: Literal["implemented", "unsupported"]
    certification_status: Literal["certified", "failed", "not_run"]


def _path(value: Path) -> str:
    return value.as_posix()


@router.get("/providers", response_model=ProvidersConfigResponse)
def get_provider_config() -> ProvidersConfigResponse:
    settings = get_container().settings.providers
    return ProvidersConfigResponse(
        providers=[
            ProviderConfig(
                name="mock",
                enabled=settings.mock.enabled,
            ),
            ProviderConfig(
                name="log_file",
                enabled=settings.log_file.enabled,
                config={"paths": [_path(path) for path in settings.log_file.paths]},
            ),
            ProviderConfig(
                name="prometheus",
                enabled=settings.prometheus.enabled,
                config={"base_url": settings.prometheus.base_url},
            ),
            ProviderConfig(
                name="deployment_file",
                enabled=settings.deployment_file.enabled,
                config={"path": _path(settings.deployment_file.path)},
            ),
            ProviderConfig(
                name="service_catalog",
                enabled=settings.service_catalog.enabled,
                config={"path": _path(settings.service_catalog.path)},
            ),
        ]
    )


def build_agent_config() -> AgentConfigResponse:
    settings = get_container().settings.agents
    provider = settings.provider
    model = settings.model.strip() if settings.model and settings.model.strip() else None
    provider_implementation = (
        implementation_status()
        if provider == ModelProvider.DEEPSEEK
        else "implemented"
    )
    certification = (
        latest_certification(
            Path("output/reliability"),
            provider.value,
            model,
        )
        if model is not None
        else "not_run"
    )
    return AgentConfigResponse(
        provider=provider,
        model=model,
        implementation_status=provider_implementation,
        certification_status=certification,
    )


@router.get("/agents", response_model=AgentConfigResponse)
def get_agent_config() -> AgentConfigResponse:
    return build_agent_config()
