from __future__ import annotations

import inspect

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, model_validator

from backend.domain.multi_agent import (
    ExecutionContractVersion,
    InvestigationStrategy,
    ModelProvider,
)
from backend.domain.runtime import (
    ReplayReport,
    RuntimeAttempt,
    RuntimeCheckpoint,
    RuntimeEvent,
    RuntimeEventType,
    RuntimeRun,
    RuntimeRunDiff,
    RuntimeRunReason,
    RuntimeRunStatus,
)
from backend.runtime.store import (
    RuntimeConflict,
    RuntimeContractError,
    RuntimeIntegrityError,
    RuntimeNotFound,
    RuntimePersistenceError,
)
from backend.services.container import get_container

router = APIRouter(tags=["runtime-runs"])


class RuntimeRunCreateRequest(BaseModel):
    # server-owned 字段（execution identity、authority、endpoint、capability
    # artifact）不接受请求覆盖；多余字段一律 422。
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    strategy: InvestigationStrategy
    run_reason: RuntimeRunReason
    parent_run_id: str | None = None
    model_provider: ModelProvider | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    execution_contract_version: ExecutionContractVersion = (
        ExecutionContractVersion.V10_LEGACY
    )

    @model_validator(mode="after")
    def validate_live_run_contract(self):
        if self.execution_contract_version == ExecutionContractVersion.V11:
            return self
        RuntimeRun(
            investigation_id="request-validation",
            run_kind="live",
            strategy=self.strategy,
            run_reason=self.run_reason,
            parent_run_id=self.parent_run_id,
            model_provider=self.model_provider,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
        )
        return self


class RuntimeRunDetail(RuntimeRun):
    attempts: list[RuntimeAttempt]
    checkpoints: list[RuntimeCheckpoint]


def _runtime_container():
    container = get_container()
    if not container.settings.runtime.enabled:
        raise HTTPException(status_code=503, detail="Runtime is disabled")
    return container


def _run_or_http(run_id: str) -> RuntimeRun:
    container = _runtime_container()
    try:
        return container.runtime_store.get_run(run_id)
    except RuntimeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _map_runtime_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RuntimeNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RuntimeContractError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (RuntimeConflict, RuntimeIntegrityError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RuntimePersistenceError):
        return HTTPException(status_code=503, detail="Runtime persistence unavailable")
    return HTTPException(status_code=503, detail="Runtime service unavailable")


def resolve_last_event_id(header_value: str | None, query_value: int) -> int:
    raw = header_value if header_value is not None else str(query_value)
    try:
        sequence = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Last-Event-ID must be an integer") from exc
    if sequence < 0:
        raise HTTPException(status_code=422, detail="Last-Event-ID must be non-negative")
    return sequence


def format_sse_event(event: RuntimeEvent) -> str:
    event_name = (
        event.event_type.value
        if isinstance(event.event_type, RuntimeEventType) and event.schema_version == 1
        else "runtime.opaque"
    )
    return (
        f"id: {event.sequence}\n"
        f"event: {event_name}\n"
        f"data: {event.model_dump_json()}\n\n"
    )


def heartbeat_frame() -> str:
    return ": heartbeat\n\n"


@router.post(
    "/investigations/{investigation_id}/runtime-runs",
    response_model=RuntimeRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_runtime_run(
    investigation_id: str,
    request: RuntimeRunCreateRequest,
) -> RuntimeRun:
    container = _runtime_container()
    try:
        run = container.create_runtime_run(
            investigation_id,
            strategy=request.strategy,
            run_reason=request.run_reason,
            parent_run_id=request.parent_run_id,
            model_provider=request.model_provider,
            model_name=request.model_name,
            prompt_version=request.prompt_version,
            execution_contract_version=request.execution_contract_version,
        )
        await container.runtime_writer.start()
        await container.runtime_manager.start(run.id)
        return run
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (
        RuntimeConflict,
        RuntimeContractError,
        RuntimeIntegrityError,
        RuntimePersistenceError,
    ) as exc:
        raise _map_runtime_error(exc) from exc


@router.get(
    "/investigations/{investigation_id}/runtime-runs",
    response_model=list[RuntimeRun],
)
def list_runtime_runs(investigation_id: str) -> list[RuntimeRun]:
    container = _runtime_container()
    try:
        container.repository.get(investigation_id)
        return container.runtime_store.list_runs(investigation_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runtime-runs/{run_id}", response_model=RuntimeRunDetail)
def get_runtime_run(run_id: str) -> RuntimeRunDetail:
    container = _runtime_container()
    run = _run_or_http(run_id)
    return RuntimeRunDetail(
        **run.model_dump(),
        attempts=container.runtime_store.list_attempts(run_id),
        checkpoints=container.runtime_store.list_checkpoints(run_id),
    )


@router.get("/runtime-runs/{run_id}/events", response_model=list[RuntimeEvent])
def list_runtime_events(
    run_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[RuntimeEvent]:
    container = _runtime_container()
    _run_or_http(run_id)
    return container.runtime_store.list_events(run_id, after=after, limit=limit)


@router.get("/runtime-runs/{run_id}/events/stream")
async def stream_runtime_events(
    run_id: str,
    request: Request,
    last_event_id: int = Query(default=0, ge=0),
) -> StreamingResponse:
    container = _runtime_container()
    _run_or_http(run_id)
    after = resolve_last_event_id(request.headers.get("last-event-id"), last_event_id)

    async def generate():
        async for event in container.event_hub.stream(
            container.runtime_store,
            run_id,
            after=after,
            heartbeat_seconds=container.settings.runtime.heartbeat_seconds,
            is_disconnected=request.is_disconnected,
        ):
            yield heartbeat_frame() if event is None else format_sse_event(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runtime-runs/{run_id}/cancel", response_model=RuntimeRun)
async def cancel_runtime_run(run_id: str) -> RuntimeRun:
    container = _runtime_container()
    _run_or_http(run_id)
    try:
        return await container.runtime_manager.cancel(run_id)
    except (RuntimeConflict, RuntimeNotFound, RuntimePersistenceError) as exc:
        raise _map_runtime_error(exc) from exc


@router.post(
    "/runtime-runs/{run_id}/resume",
    response_model=RuntimeRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resume_runtime_run(run_id: str) -> RuntimeRun:
    container = _runtime_container()
    run = _run_or_http(run_id)
    if run.status != RuntimeRunStatus.INTERRUPTED:
        raise HTTPException(status_code=409, detail="only interrupted runs can resume")
    try:
        await container.runtime_writer.start()
        await container.runtime_manager.resume(run_id)
        return run
    except RuntimeConflict as exc:
        detail = str(exc)
        status_code = 422 if "checkpoint" in detail.lower() else 409
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except (RuntimeNotFound, RuntimePersistenceError) as exc:
        raise _map_runtime_error(exc) from exc


@router.post("/runtime-runs/{run_id}/replay", response_model=ReplayReport)
async def replay_runtime_run(run_id: str):
    container = _runtime_container()
    _run_or_http(run_id)
    try:
        result = container.replay_run(run_id)
        return await result if inspect.isawaitable(result) else result
    except (RuntimeNotFound, RuntimeConflict, RuntimeIntegrityError) as exc:
        raise _map_runtime_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Runtime replay unavailable") from exc


@router.get("/runtime-runs/{run_id}/diff", response_model=RuntimeRunDiff)
async def diff_runtime_runs(run_id: str, against_run_id: str):
    container = _runtime_container()
    left = _run_or_http(run_id)
    right = _run_or_http(against_run_id)
    if left.investigation_id != right.investigation_id:
        raise HTTPException(
            status_code=409,
            detail="runtime runs belong to different investigations",
        )
    try:
        result = container.diff_runs(run_id, against_run_id)
        return await result if inspect.isawaitable(result) else result
    except (RuntimeNotFound, RuntimeConflict, RuntimeIntegrityError) as exc:
        raise _map_runtime_error(exc) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Runtime diff unavailable") from exc
