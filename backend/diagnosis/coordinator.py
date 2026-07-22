from collections.abc import Callable

from backend.diagnosis.context import (
    DiagnosisContext,
    SpecialistResult,
    provider_status_to_specialist_status,
)
from backend.domain.events import IncidentEvent
from backend.providers.registry import ProviderRegistry
from backend.runtime.concurrency import RunStepGate

AGENT_NAMES_BY_PROVIDER = {
    "log": "LogAnalyst",
    "metric": "MetricAnalyst",
    "deploy": "DeployAnalyst",
    "dependency": "DependencyAnalyst",
    "service_catalog": "ServiceCatalogAnalyst",
    "related_alert": "RelatedAlertAnalyst",
}


class DiagnosisCoordinator:
    def __init__(self, providers: ProviderRegistry) -> None:
        self.providers = providers

    def collect(self, event: IncidentEvent) -> DiagnosisContext:
        provider_results = self.providers.collect_results(event)
        return self._context(event, provider_results)

    async def collect_async(
        self,
        event: IncidentEvent,
        *,
        max_parallel_steps: int = 3,
        check_execution: Callable[[], None] | None = None,
        parallel_limit: RunStepGate | None = None,
    ) -> DiagnosisContext:
        provider_results = await self.providers.collect_results_async(
            event,
            max_parallel_steps=max_parallel_steps,
            check_execution=check_execution,
            parallel_limit=parallel_limit,
        )
        return self._context(event, provider_results)

    def _context(self, event, provider_results) -> DiagnosisContext:
        evidence = self.providers.evidence_from_results(provider_results)
        specialist_results = [
            SpecialistResult(
                agent_name=AGENT_NAMES_BY_PROVIDER.get(
                    str(result.provider), f"{result.provider}Analyst"
                ),
                status=provider_status_to_specialist_status(result.status),
                evidence_items=result.evidence_items,
                summary=(
                    f"{result.provider} provider returned "
                    f"{len(result.evidence_items)} evidence item(s)"
                ),
                errors=[result.error_message] if result.error_message else [],
                duration_ms=result.duration_ms,
            )
            for result in provider_results
        ]

        return DiagnosisContext(
            event=event,
            evidence=evidence,
            provider_results=provider_results,
            specialist_results=specialist_results,
        )
