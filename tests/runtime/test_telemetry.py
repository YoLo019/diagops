from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from threading import Event, Lock
from threading import enumerate as enumerate_threads
from unittest.mock import AsyncMock, Mock

from opentelemetry import trace
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from backend.config.settings import (
    AppSettings,
    OpenTelemetrySettings,
    StorageSettings,
)
from backend.db.models import InvestigationRecord
from backend.db.repositories import InMemoryInvestigationRepository
from backend.diagnosis.agents_runtime import AgentsRcaRuntime, _SdkTurnResult
from backend.diagnosis.coordinator import DiagnosisCoordinator
from backend.diagnosis.orchestrator import DiagnosisOrchestrator
from backend.domain.events import IncidentEvent, IncidentSource, Severity
from backend.domain.multi_agent import InvestigationStrategy
from backend.domain.runtime import (
    RuntimeAttempt,
    RuntimeAttemptStatus,
    RuntimePhase,
    RuntimeRun,
    RuntimeRunKind,
    RuntimeRunReason,
)
from backend.providers.registry import build_mock_provider_registry
from backend.rca.analyzer import RcaAnalyzer
from backend.reports.generator import ReportGenerator
from backend.runtime.coordinator import RuntimeCoordinator
from backend.runtime.faults import DeterministicFaultInjector
from backend.runtime.phase_executor import DiagnosisPhaseExecutor
from backend.runtime.phases import BusinessMutation, PhaseOutput
from backend.runtime.store import InMemoryRuntimeStore
from backend.runtime.telemetry import RuntimeTelemetry, TraceReference
from backend.runtime.writer import RuntimeWriter
from backend.services.container import AppContainer


def _run() -> RuntimeRun:
    return RuntimeRun(
        id="run-telemetry",
        investigation_id="inv-telemetry",
        run_kind=RuntimeRunKind.LIVE,
        strategy=InvestigationStrategy.FIXED,
        run_reason=RuntimeRunReason.INITIAL,
        created_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


def _attempt(number: int) -> RuntimeAttempt:
    return RuntimeAttempt(
        id=f"attempt-{number}",
        run_id="run-telemetry",
        attempt_number=number,
        status=RuntimeAttemptStatus.RUNNING,
    )


def test_attempt_phase_agent_model_tool_hierarchy_and_allowlist() -> None:
    exporter = InMemorySpanExporter()
    telemetry = RuntimeTelemetry.for_exporter(exporter)

    with telemetry.attempt(_run(), _attempt(1)) as attempt_trace:
        with telemetry.span(
            "runtime.phase",
            {
                "phase": RuntimePhase.SPECIALIST_ANALYSIS.value,
                "status": "running",
                "prompt": "must-not-export",
            },
        ):
            with telemetry.span(
                "runtime.agent",
                {"agent_name": "LogAgent", "provider": "openai"},
            ):
                with telemetry.span(
                    "runtime.model",
                    {"model": "gpt-test", "input_tokens": 12},
                ):
                    pass
                with telemetry.span(
                    "runtime.tool",
                    {"tool_name": "read_logs", "evidence_body": "secret"},
                ):
                    pass

    spans = {span.name: span for span in exporter.get_finished_spans()}
    assert spans["runtime.phase"].parent.span_id == spans["runtime.attempt"].context.span_id
    assert spans["runtime.agent"].parent.span_id == spans["runtime.phase"].context.span_id
    assert spans["runtime.model"].parent.span_id == spans["runtime.agent"].context.span_id
    assert spans["runtime.tool"].parent.span_id == spans["runtime.agent"].context.span_id
    exported_keys = {key for span in spans.values() for key in span.attributes}
    assert "prompt" not in exported_keys
    assert "evidence_body" not in exported_keys
    assert exported_keys <= RuntimeTelemetry.ALLOWED_ATTRIBUTES
    assert len(attempt_trace.reference.trace_id) == 32
    assert len(attempt_trace.reference.root_span_id) == 16


def test_resumed_attempt_uses_new_trace_with_link_to_prior_attempt() -> None:
    exporter = InMemorySpanExporter()
    telemetry = RuntimeTelemetry.for_exporter(exporter)

    with telemetry.attempt(_run(), _attempt(1)) as first:
        prior = first.reference
    with telemetry.attempt(_run(), _attempt(2), previous=prior):
        pass

    roots = [
        span for span in exporter.get_finished_spans() if span.name == "runtime.attempt"
    ]
    assert len(roots) == 2
    assert roots[0].context.trace_id != roots[1].context.trace_id
    assert len(roots[1].links) == 1
    assert roots[1].links[0].context.trace_id == roots[0].context.trace_id
    assert roots[1].links[0].context.span_id == roots[0].context.span_id


def test_disabled_telemetry_is_noop() -> None:
    exporter = InMemorySpanExporter()
    telemetry = RuntimeTelemetry.disabled()

    with telemetry.attempt(_run(), _attempt(1)) as attempt_trace:
        with telemetry.span("runtime.phase", {"phase": "intake"}):
            pass

    assert attempt_trace.reference is None
    assert exporter.get_finished_spans() == ()


class RaisingExporter(SpanExporter):
    def export(self, spans):
        raise RuntimeError("collector includes password=unsafe")

    def shutdown(self):
        return None


def test_exporter_failure_isolated_from_runtime_execution() -> None:
    telemetry = RuntimeTelemetry.for_exporter(RaisingExporter())

    with telemetry.attempt(_run(), _attempt(1)):
        with telemetry.span("runtime.phase", {"phase": "intake"}):
            pass

    assert telemetry.force_flush(timeout_seconds=0.1) is True


def test_otel_unavailable_fault_falls_back_to_noop() -> None:
    telemetry = RuntimeTelemetry.from_settings(
        OpenTelemetrySettings(
            enabled=True,
            endpoint="http://127.0.0.1:4318/v1/traces",
        ),
        fault_injector=DeterministicFaultInjector({"otel_unavailable": 1}),
    )

    assert telemetry.enabled is False


def test_trace_reference_validates_fixed_length_internal_ids() -> None:
    reference = TraceReference(trace_id="a" * 32, root_span_id="b" * 16)

    assert reference.trace_id == "a" * 32
    assert "trace_id" not in _attempt(1).model_dump()


def test_coordinator_persists_internal_attempt_context_and_traces_phases() -> None:
    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-telemetry",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="telemetry",
                description="telemetry",
                started_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )
    )
    store = InMemoryRuntimeStore(repository)
    run = store.create_run(_run())
    exporter = InMemorySpanExporter()
    telemetry = RuntimeTelemetry.for_exporter(exporter)

    class Executor:
        fault_injector = None

        async def execute_phase(self, phase_input):
            return PhaseOutput(
                business_mutation=BusinessMutation(
                    investigation_id=run.investigation_id
                ),
                safe_payload={"status": "completed"},
                resume_state=phase_input.resume_state,
            )

    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=Executor(),
        telemetry=telemetry,
    )

    import asyncio

    asyncio.run(coordinator.execute(run.id, owner="worker-telemetry"))
    attempts = store.list_attempts(run.id)
    reference = store.get_attempt_trace_context(attempts[0].id)
    spans = exporter.get_finished_spans()

    assert reference is not None
    assert len([span for span in spans if span.name == "runtime.attempt"]) == 1
    assert len([span for span in spans if span.name == "runtime.phase"]) == 8


def test_real_agents_callbacks_create_lifetime_spans_with_actual_parentage(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    repository = InMemoryInvestigationRepository()
    repository.save(
        InvestigationRecord(
            id="inv-real-telemetry",
            event=IncidentEvent(
                source=IncidentSource.MANUAL,
                service="checkout-service",
                environment="prod",
                severity=Severity.WARNING,
                title="real telemetry",
                description="real telemetry",
                started_at=datetime(2026, 7, 18, tzinfo=UTC),
            ),
        )
    )
    providers = build_mock_provider_registry()

    async def turn(**_kwargs):
        await asyncio.sleep(0.001)
        return _SdkTurnResult([], None, [])

    orchestrator = DiagnosisOrchestrator(
        repository=repository,
        providers=providers,
        analyzer=RcaAnalyzer(),
        report_generator=ReportGenerator(),
        coordinator=DiagnosisCoordinator(providers),
        agents_runtime=AgentsRcaRuntime(model="fake-model", turn=turn),
        default_strategy=InvestigationStrategy.ADAPTIVE,
    )
    store = InMemoryRuntimeStore(repository)
    run = store.create_run(
        RuntimeRun(
            id="run-real-telemetry",
            investigation_id="inv-real-telemetry",
            run_kind=RuntimeRunKind.LIVE,
            strategy=InvestigationStrategy.ADAPTIVE,
            run_reason=RuntimeRunReason.INITIAL,
            model_name="fake-model",
            prompt_version="v9",
        )
    )
    exporter = InMemorySpanExporter()
    telemetry = RuntimeTelemetry.for_exporter(exporter)
    coordinator = RuntimeCoordinator(
        store=store,
        writer=RuntimeWriter(store),
        phase_executor=DiagnosisPhaseExecutor(orchestrator, telemetry=telemetry),
        telemetry=telemetry,
    )

    asyncio.run(coordinator.execute(run.id, owner="worker-real-telemetry"))

    spans = exporter.get_finished_spans()
    by_id = {span.context.span_id: span for span in spans}
    agents = [span for span in spans if span.name == "runtime.agent"]
    models = [span for span in spans if span.name == "runtime.model"]
    assert agents
    assert models
    assert {span.attributes["agent_name"] for span in agents} <= {
        "CoordinatorAgent",
        "LogAgent",
        "MetricAgent",
        "DeploymentAgent",
    }
    assert "phase_executor" not in {
        span.attributes.get("agent_name") for span in agents
    }
    assert all(by_id[span.parent.span_id].name == "runtime.phase" for span in agents)
    assert all(by_id[span.parent.span_id].name == "runtime.agent" for span in models)
    assert all(span.end_time > span.start_time for span in [*agents, *models])


def test_telemetry_shutdown_is_idempotent_and_isolates_provider_failure() -> None:
    class Provider:
        shutdown_calls = 0

        def get_tracer(self, *args, **kwargs):
            return trace.NoOpTracerProvider().get_tracer(*args, **kwargs)

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            raise RuntimeError("collector unavailable")

    provider = Provider()
    telemetry = RuntimeTelemetry(enabled=True, provider=provider)

    telemetry.shutdown()
    telemetry.shutdown()

    assert provider.shutdown_calls == 1


def test_telemetry_lifecycle_timeout_stays_serial_and_reclaims_worker() -> None:
    class BlockingProvider:
        active = 0
        max_active = 0
        shutdown_calls = 0

        def __init__(self) -> None:
            self.lock = Lock()
            self.flush_started = Event()
            self.release_flush = Event()
            self.shutdown_completed = Event()

        def get_tracer(self, *args, **kwargs):
            return trace.NoOpTracerProvider().get_tracer(*args, **kwargs)

        def force_flush(self, timeout_millis: int) -> bool:
            del timeout_millis
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.flush_started.set()
            self.release_flush.wait(1)
            with self.lock:
                self.active -= 1
            return True

        def shutdown(self) -> None:
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.shutdown_calls += 1
                self.active -= 1
            self.shutdown_completed.set()

    provider = BlockingProvider()
    telemetry = RuntimeTelemetry(enabled=True, provider=provider)

    assert telemetry.force_flush(timeout_seconds=0.01) is False
    assert provider.flush_started.is_set()
    assert telemetry.shutdown(timeout_seconds=0.01) is False
    assert telemetry.shutdown(timeout_seconds=0.01) is False
    assert provider.max_active == 1
    assert provider.shutdown_calls == 0

    provider.release_flush.set()
    assert provider.shutdown_completed.wait(1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(
        thread.name == "diagops-otel-lifecycle" for thread in enumerate_threads()
    ):
        time.sleep(0.01)

    assert provider.shutdown_calls == 1
    assert provider.max_active == 1
    assert not any(
        thread.name == "diagops-otel-lifecycle" for thread in enumerate_threads()
    )


def test_container_shutdown_is_bounded_idempotent_and_disposes_engine(
    tmp_path,
    monkeypatch,
) -> None:
    container = AppContainer(
        AppSettings(storage=StorageSettings(url=f"sqlite:///{tmp_path / 'app.db'}"))
    )
    manager_shutdown = AsyncMock()
    writer_shutdown = AsyncMock()
    dispose = Mock()

    class SlowProvider:
        flush_calls = 0
        shutdown_calls = 0

        def get_tracer(self, *args, **kwargs):
            return trace.NoOpTracerProvider().get_tracer(*args, **kwargs)

        def force_flush(self, timeout_millis: int) -> bool:
            self.flush_calls += 1
            time.sleep(0.2)
            return True

        def shutdown(self) -> None:
            self.shutdown_calls += 1
            time.sleep(0.2)

    provider = SlowProvider()
    container.runtime_manager.shutdown = manager_shutdown
    container.runtime_writer.shutdown = writer_shutdown
    container.telemetry = RuntimeTelemetry(enabled=True, provider=provider)
    monkeypatch.setattr(container.engine, "dispose", dispose)
    monkeypatch.setattr(
        "backend.services.container._TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    async def shutdown_twice() -> None:
        await container.shutdown()
        await container.shutdown()

    started = time.perf_counter()
    asyncio.run(shutdown_twice())
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    manager_shutdown.assert_awaited_once()
    writer_shutdown.assert_awaited_once()
    assert provider.flush_calls == 1
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and provider.shutdown_calls == 0:
        time.sleep(0.01)
    assert provider.shutdown_calls == 1
    while time.monotonic() < deadline and any(
        thread.name == "diagops-otel-lifecycle" for thread in enumerate_threads()
    ):
        time.sleep(0.01)
    assert not any(
        thread.name == "diagops-otel-lifecycle" for thread in enumerate_threads()
    )
    dispose.assert_called_once()
