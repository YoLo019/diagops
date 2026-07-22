from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from queue import Empty, Queue
from threading import Event, Lock, Thread, current_thread
from typing import Any
from urllib.parse import urlsplit

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
    SpanExportResult,
)
from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState
from pydantic import BaseModel, ConfigDict, Field

from backend.domain.runtime import RuntimeAttempt, RuntimeRun
from backend.runtime.faults import NoFaultInjector
from backend.safety.redaction import redact_text

logger = logging.getLogger(__name__)


class TraceReference(BaseModel):
    """仅供 Runtime Store 内部恢复链路使用，不属于公共 API 或 Event。"""

    model_config = ConfigDict(extra="forbid")

    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    root_span_id: str = Field(pattern=r"^[0-9a-f]{16}$")


class _SafeExporter(SpanExporter):
    """隔离 collector/exporter 异常，日志只保留固定安全类别。"""

    def __init__(self, delegate: SpanExporter) -> None:
        self._delegate = delegate

    def export(self, spans: Sequence[Any]) -> SpanExportResult:
        try:
            return self._delegate.export(spans)
        except Exception:
            logger.warning("runtime telemetry export failed category=otel_export_failure")
            return SpanExportResult.FAILURE

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        try:
            result = self._delegate.force_flush(timeout_millis)
        except Exception:
            logger.warning("runtime telemetry flush failed category=otel_flush_failure")
            return False
        return True if result is None else bool(result)

    def shutdown(self) -> None:
        try:
            self._delegate.shutdown()
        except Exception:
            logger.warning("runtime telemetry shutdown failed category=otel_shutdown_failure")


@dataclass(frozen=True, slots=True)
class AttemptTrace:
    reference: TraceReference | None


@dataclass(slots=True)
class _LifecycleTask:
    operation: Callable[[], object]
    failure_category: str
    stop_worker: bool = False
    completed: Event = field(default_factory=Event)
    outcome: bool = False


class RuntimeTelemetry:
    """Runtime 专用低基数 tracing facade，禁止调用方直接设置任意属性。"""

    ALLOWED_ATTRIBUTES = frozenset(
        {
            "investigation_id",
            "run_id",
            "attempt_id",
            "phase",
            "agent_name",
            "tool_name",
            "provider",
            "model",
            "status",
            "duration_ms",
            "input_tokens",
            "output_tokens",
            "cost",
            "evidence_count",
            "failure_category",
        }
    )

    def __init__(
        self,
        *,
        enabled: bool,
        provider: TracerProvider | None = None,
    ) -> None:
        self.enabled = enabled and provider is not None
        self._provider = provider
        self._lifecycle_lock = Lock()
        self._lifecycle_queue: Queue[_LifecycleTask] = Queue()
        self._lifecycle_worker: Thread | None = None
        self._flush_task: _LifecycleTask | None = None
        self._shutdown_task: _LifecycleTask | None = None
        self._shutdown = False
        provider_for_tracer = provider or trace.NoOpTracerProvider()
        self._tracer = provider_for_tracer.get_tracer("diagops-runtime")

    @classmethod
    def disabled(cls) -> RuntimeTelemetry:
        return cls(enabled=False)

    @classmethod
    def for_exporter(cls, exporter: SpanExporter) -> RuntimeTelemetry:
        provider = TracerProvider(
            resource=Resource.create({SERVICE_NAME: "diagops-runtime"})
        )
        provider.add_span_processor(SimpleSpanProcessor(_SafeExporter(exporter)))
        return cls(enabled=True, provider=provider)

    @classmethod
    def from_settings(
        cls,
        settings,
        *,
        fault_injector=None,
    ) -> RuntimeTelemetry:
        if not settings.enabled:
            return cls.disabled()
        if not cls._valid_endpoint(settings.endpoint):
            logger.warning("runtime telemetry disabled category=otel_invalid_endpoint")
            return cls.disabled()
        injector = fault_injector or NoFaultInjector()
        try:
            injector.hit("otel_unavailable")
            exporter = _SafeExporter(OTLPSpanExporter(endpoint=settings.endpoint))
            provider = TracerProvider(
                resource=Resource.create({SERVICE_NAME: "diagops-runtime"})
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            return cls(enabled=True, provider=provider)
        except Exception:
            logger.warning("runtime telemetry disabled category=otel_creation_failure")
            return cls.disabled()

    @staticmethod
    def _valid_endpoint(endpoint: str | None) -> bool:
        if endpoint is None or len(endpoint) > 2048:
            return False
        parsed = urlsplit(endpoint)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname is not None
            and parsed.username is None
            and parsed.password is None
        )

    @classmethod
    def safe_attributes(cls, attributes: Mapping[str, Any]) -> dict[str, Any]:
        safe: dict[str, Any] = {}
        for key, value in attributes.items():
            if key not in cls.ALLOWED_ATTRIBUTES or value is None:
                continue
            if isinstance(value, str):
                safe[key] = redact_text(value)[:160]
            elif isinstance(value, (bool, int, float)):
                safe[key] = value
            elif hasattr(value, "value") and isinstance(value.value, str):
                safe[key] = redact_text(value.value)[:160]
        return safe

    @contextmanager
    def attempt(
        self,
        run: RuntimeRun,
        attempt: RuntimeAttempt,
        *,
        previous: TraceReference | None = None,
    ) -> Iterator[AttemptTrace]:
        if not self.enabled:
            yield AttemptTrace(reference=None)
            return
        links: list[Link] = []
        if previous is not None:
            links.append(
                Link(
                    SpanContext(
                        trace_id=int(previous.trace_id, 16),
                        span_id=int(previous.root_span_id, 16),
                        is_remote=True,
                        trace_flags=TraceFlags.SAMPLED,
                        trace_state=TraceState(),
                    )
                )
            )
        attributes = self.safe_attributes(
            {
                "investigation_id": run.investigation_id,
                "run_id": run.id,
                "attempt_id": attempt.id,
                "status": attempt.status.value,
            }
        )
        with self._tracer.start_as_current_span(
            "runtime.attempt",
            context=Context(),
            links=links,
            attributes=attributes,
        ) as span:
            context = span.get_span_context()
            reference = TraceReference(
                trace_id=f"{context.trace_id:032x}",
                root_span_id=f"{context.span_id:016x}",
            )
            yield AttemptTrace(reference=reference)

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Mapping[str, Any],
    ) -> Iterator[Any]:
        if not self.enabled:
            yield trace.INVALID_SPAN
            return
        with self._tracer.start_as_current_span(
            name,
            attributes=self.safe_attributes(attributes),
        ) as span:
            yield span

    def start_span(
        self,
        name: str,
        attributes: Mapping[str, Any],
        *,
        parent=None,
    ):
        """启动由 Runtime callback 显式收尾的真实生命周期 span。"""
        if not self.enabled:
            return trace.INVALID_SPAN
        context = trace.set_span_in_context(parent) if parent is not None else None
        return self._tracer.start_span(
            name,
            context=context,
            attributes=self.safe_attributes(attributes),
        )

    def force_flush(self, *, timeout_seconds: float = 5) -> bool:
        if self._provider is None:
            return True
        return self._bounded_lifecycle_call(
            "flush",
            lambda: self._provider.force_flush(
                timeout_millis=max(1, int(timeout_seconds * 1000))
            ),
            timeout_seconds=timeout_seconds,
            failure_category="otel_flush_failure",
            timeout_category="otel_flush_timeout",
        )

    def shutdown(self, *, timeout_seconds: float = 5) -> bool:
        if self._provider is None:
            return True
        return self._bounded_lifecycle_call(
            "shutdown",
            self._provider.shutdown,
            timeout_seconds=timeout_seconds,
            failure_category="otel_shutdown_failure",
            timeout_category="otel_shutdown_timeout",
        )

    def _bounded_lifecycle_call(
        self,
        kind: str,
        operation: Callable[[], object],
        *,
        timeout_seconds: float,
        failure_category: str,
        timeout_category: str,
    ) -> bool:
        with self._lifecycle_lock:
            task = self._flush_task if kind == "flush" else self._shutdown_task
            if task is None:
                task = _LifecycleTask(
                    operation=operation,
                    failure_category=failure_category,
                    stop_worker=kind == "shutdown",
                )
                if kind == "flush":
                    if self._shutdown:
                        task.outcome = True
                        task.completed.set()
                    else:
                        self._flush_task = task
                else:
                    self._shutdown = True
                    self._shutdown_task = task
                if not task.completed.is_set():
                    self._ensure_lifecycle_worker()
                    self._lifecycle_queue.put(task)
        if not task.completed.wait(max(0, timeout_seconds)):
            logger.warning("runtime telemetry timed out category=%s", timeout_category)
            return False
        return task.outcome

    def _ensure_lifecycle_worker(self) -> None:
        if self._lifecycle_worker is not None:
            return
        self._lifecycle_worker = Thread(
            target=self._run_lifecycle_worker,
            daemon=True,
            name="diagops-otel-lifecycle",
        )
        self._lifecycle_worker.start()

    def _run_lifecycle_worker(self) -> None:
        while True:
            try:
                task = self._lifecycle_queue.get(timeout=0.05)
            except Empty:
                with self._lifecycle_lock:
                    if self._lifecycle_queue.empty():
                        if self._lifecycle_worker is current_thread():
                            self._lifecycle_worker = None
                        return
                continue
            try:
                result = task.operation()
                task.outcome = True if result is None else bool(result)
            except Exception:
                logger.warning(
                    "runtime telemetry failed category=%s",
                    task.failure_category,
                )
                task.outcome = False
            finally:
                task.completed.set()
                self._lifecycle_queue.task_done()
            if task.stop_worker:
                with self._lifecycle_lock:
                    if self._lifecycle_worker is current_thread():
                        self._lifecycle_worker = None
                    return
