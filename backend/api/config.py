from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, Field

from backend.services.container import get_container

router = APIRouter(prefix="/config", tags=["config"])


class ProviderConfig(BaseModel):
    name: str
    enabled: bool
    config: dict[str, str | list[str]] = Field(default_factory=dict)


class ProvidersConfigResponse(BaseModel):
    providers: list[ProviderConfig]


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
