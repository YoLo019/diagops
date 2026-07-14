import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.domain.events import IncidentEvent
from backend.domain.evidence import EvidenceItem, EvidenceKind, EvidenceProvider
from backend.providers.results import ProviderResult

MAX_DEPLOYMENT_BYTES = 2 * 1024 * 1024
MAX_DEPLOYMENTS = 10_000


class FileDeploymentProvider:
    provider = EvidenceProvider.DEPLOY

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = MAX_DEPLOYMENT_BYTES,
        max_deployments: int = MAX_DEPLOYMENTS,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.max_deployments = max_deployments

    def collect(self, event: IncidentEvent) -> ProviderResult:
        deployments = self._load_deployments()
        window = timedelta(minutes=event.time_window_minutes)
        evidence: list[EvidenceItem] = []

        for deployment in deployments:
            deployed_at = deployment["deployed_at"]
            if deployment["service"] != event.service:
                continue
            if deployment["environment"] != event.environment:
                continue
            if abs(deployed_at - event.started_at) > window:
                continue

            summary = deployment["summary"] or (
                f"{event.service} {deployment['version']} was deployed near incident start"
            )
            evidence.append(
                EvidenceItem(
                    provider=self.provider,
                    kind=EvidenceKind.DEPLOYMENT,
                    timestamp=deployed_at,
                    summary=summary,
                    payload={
                        "service": deployment["service"],
                        "environment": deployment["environment"],
                        "version": deployment["version"],
                        "deployed_at": deployed_at.isoformat(),
                        "operator": deployment["operator"],
                        "commit": deployment["commit"],
                        "summary": summary,
                    },
                    confidence=1.0,
                )
            )

        return ProviderResult(provider=self.provider, evidence_items=evidence)

    def _load_deployments(self) -> list[dict[str, Any]]:
        if self.path.stat().st_size > self.max_bytes:
            raise ValueError("Deployment file exceeds size limit")
        loaded = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(loaded, list):
            raise ValueError("Deployment file must contain a JSON list")

        deployments: list[dict[str, Any]] = []
        for item in loaded[: self.max_deployments]:
            if not isinstance(item, dict):
                raise ValueError("Deployment records must be JSON objects")
            deployments.append(_deployment_record(item))
        return deployments


def _deployment_record(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "service": _string_value(item, "service"),
        "environment": _string_value(item, "environment"),
        "version": _string_value(item, "version"),
        "deployed_at": datetime.fromisoformat(_string_value(item, "deployed_at")),
        "operator": _string_value(item, "operator"),
        "commit": _string_value(item, "commit"),
        "summary": _string_value(item, "summary"),
    }


def _string_value(item: dict[str, Any], key: str) -> str:
    value = item.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"Deployment field must be a string: {key}")
    return value
