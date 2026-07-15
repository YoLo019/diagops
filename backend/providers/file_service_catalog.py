from pathlib import Path
from typing import Any

import yaml

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.domain.tool_queries import ServiceCatalogQuery
from backend.providers.results import ProviderResult, ProviderStatus

MAX_CATALOG_BYTES = 2 * 1024 * 1024


class FileServiceCatalogProvider:
    provider = EvidenceProvider.SERVICE_CATALOG
    supported_tools = frozenset({"read_service_catalog"})

    def __init__(self, path: Path, *, max_bytes: int = MAX_CATALOG_BYTES) -> None:
        self.path = path
        self.max_bytes = max_bytes

    def collect(
        self, event: IncidentEvent, query: ServiceCatalogQuery | None = None
    ) -> ProviderResult:
        services = self._load_services()
        service = services.get(event.service)
        if service is None:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.SKIPPED,
                error_message=f"Service not found in catalog: {event.service}",
            )
        environments = _list_value(service, "environments")
        if environments and event.environment not in environments:
            return ProviderResult(
                provider=self.provider,
                status=ProviderStatus.SKIPPED,
                error_message="Service catalog environment is out of scope",
            )

        evidence = EvidenceItem(
            provider=self.provider,
            kind=EvidenceKind.SERVICE_METADATA,
            timestamp=event.started_at,
            summary=f"{event.service} service metadata loaded from catalog",
            payload={
                "service": event.service,
                "owner": _string_value(service, "owner"),
                "team": _string_value(service, "team"),
                "runtime": _string_value(service, "runtime"),
                "repository": _string_value(service, "repository"),
                "dependencies": (
                    _list_value(service, "dependencies")
                    if query is None or query.include_dependencies
                    else []
                ),
                "dashboards": _list_value(service, "dashboards"),
                "runbooks": _list_value(service, "runbooks"),
                "environment": event.environment,
                "environment_scope": "allowlist" if environments else "global",
                "time_filter": "not_applicable",
            },
            confidence=1.0,
        )
        return ProviderResult(provider=self.provider, evidence_items=[evidence])

    def _load_services(self) -> dict[str, dict[str, Any]]:
        if self.path.stat().st_size > self.max_bytes:
            raise ValueError("Service catalog exceeds size limit")
        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("Service catalog must be a YAML mapping")

        services = loaded.get("services")
        if not isinstance(services, dict):
            raise ValueError("Service catalog must contain a services mapping")

        normalized: dict[str, dict[str, Any]] = {}
        for service_name, service_data in services.items():
            if not isinstance(service_name, str) or not isinstance(service_data, dict):
                raise ValueError("Service catalog services must map names to metadata")
            normalized[service_name] = service_data
        return normalized


def _string_value(service: dict[str, Any], key: str) -> str:
    value = service.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Service catalog field must be a string: {key}")
    return value


def _list_value(service: dict[str, Any], key: str) -> list[str]:
    value = service.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"Service catalog field must be a list of strings: {key}")
    return value
