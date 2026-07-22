import asyncio
from datetime import UTC, datetime

from backend.api.runtime_runs import router
from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeEvent,
    RuntimeEventType,
)
from backend.services.container import reset_container


def test_runtime_sse_route_declares_reconnect_and_proxy_headers() -> None:
    container = reset_container()
    from tests.api.test_runtime_runs_api import _stored_run

    _stored_run("inv-sse", run_id="run-sse")
    route = next(
        route
        for route in router.routes
        if getattr(route, "path", None) == "/runtime-runs/{run_id}/events/stream"
    )

    class Request:
        headers = {"last-event-id": "0"}

        async def is_disconnected(self) -> bool:
            return True

    response = asyncio.run(
        route.endpoint(run_id="run-sse", request=Request(), last_event_id=0)
    )

    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["content-type"].startswith("text/event-stream")
    assert container.runtime_store.get_run("run-sse").status == "completed"


def test_sse_business_frame_is_safe_json_and_heartbeat_has_no_id() -> None:
    from backend.api.runtime_runs import format_sse_event, heartbeat_frame

    event = RuntimeEvent(
        run_id="run-safe",
        attempt_id="attempt-safe",
        sequence=3,
        event_type=RuntimeEventType.RUN_STARTED,
        actor_type=RuntimeActorType.RUNTIME,
        safe_payload={"message": "line one\nline two"},
        occurred_at=datetime(2026, 7, 18, tzinfo=UTC),
    )

    frame = format_sse_event(event)

    assert frame.startswith("id: 3\nevent: run.started\ndata: {")
    assert "line one\\nline two" in frame
    assert frame.endswith("\n\n")
    assert heartbeat_frame() == ": heartbeat\n\n"
    assert "id:" not in heartbeat_frame()


def test_last_event_id_header_takes_precedence_over_browser_query() -> None:
    from backend.api.runtime_runs import resolve_last_event_id

    assert resolve_last_event_id("7", 2) == 7
    assert resolve_last_event_id(None, 2) == 2


def test_disconnect_does_not_cancel_runtime_run() -> None:
    container = reset_container()
    from tests.api.test_runtime_runs_api import _stored_run

    _stored_run("inv-disconnect", run_id="run-disconnect")

    async def disconnect() -> bool:
        await asyncio.sleep(0)
        return True

    async def consume() -> None:
        stream = container.event_hub.stream(
            container.runtime_store,
            "run-disconnect",
            after=0,
            heartbeat_seconds=1,
            is_disconnected=disconnect,
        )
        try:
            await anext(stream)
        except StopAsyncIteration:
            pass

    asyncio.run(consume())

    assert container.runtime_store.get_run("run-disconnect").status == "completed"
