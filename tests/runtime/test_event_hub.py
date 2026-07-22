import asyncio
from datetime import UTC, datetime

from backend.domain.runtime import (
    RuntimeActorType,
    RuntimeEvent,
    RuntimeEventType,
)
from backend.runtime.event_hub import EventHub


def _event(run_id: str, sequence: int) -> RuntimeEvent:
    return RuntimeEvent(
        id=f"event-{run_id}-{sequence}",
        run_id=run_id,
        attempt_id=f"attempt-{run_id}",
        sequence=sequence,
        event_type=RuntimeEventType.RUN_STARTED,
        actor_type=RuntimeActorType.RUNTIME,
        safe_payload={"status": "running"},
        occurred_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


class RaceStore:
    def __init__(self, hub: EventHub, events: list[RuntimeEvent]) -> None:
        self.hub = hub
        self.events = events
        self.queried = False

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 500):
        matching = [
            event
            for event in self.events
            if event.run_id == run_id and event.sequence > after
        ][:limit]
        if not self.queried:
            self.queried = True
            raced = _event(run_id, len(self.events) + 1)
            self.events.append(raced)
            self.hub.publish([raced])
            matching.append(raced)
        return matching


def test_catchup_subscribes_first_and_deduplicates_racing_event() -> None:
    async def scenario() -> None:
        hub = EventHub(max_queue_size=8)
        store = RaceStore(hub, [_event("run-a", 1)])
        stream = hub.stream(store, "run-a", after=0, heartbeat_seconds=1)

        first = await anext(stream)
        second = await anext(stream)
        await stream.aclose()

        assert [first.sequence, second.sequence] == [1, 2]

    asyncio.run(scenario())


def test_reconnect_after_old_sequence_returns_each_durable_event_once() -> None:
    async def scenario() -> None:
        hub = EventHub(max_queue_size=8)
        store = RaceStore(
            hub,
            [_event("run-a", 1), _event("run-a", 2), _event("run-a", 3)],
        )
        store.queried = True
        stream = hub.stream(store, "run-a", after=1, heartbeat_seconds=1)

        events = [await anext(stream), await anext(stream)]
        await stream.aclose()

        assert [event.sequence for event in events] == [2, 3]

    asyncio.run(scenario())


def test_live_notifications_are_isolated_by_run() -> None:
    async def scenario() -> None:
        hub = EventHub(max_queue_size=8)
        left = hub.subscribe("run-left")
        right = hub.subscribe("run-right")

        hub.publish([_event("run-left", 1), _event("run-right", 1)])
        await asyncio.sleep(0)

        assert (await left.get()).run_id == "run-left"
        assert (await right.get()).run_id == "run-right"
        hub.unsubscribe(left)
        hub.unsubscribe(right)

    asyncio.run(scenario())


def test_slow_subscriber_overflow_closes_only_that_subscriber() -> None:
    async def scenario() -> None:
        hub = EventHub(max_queue_size=1)
        slow = hub.subscribe("run-a")
        healthy = hub.subscribe("run-b")

        hub.publish([_event("run-a", 1), _event("run-a", 2)])
        hub.publish([_event("run-b", 1)])
        await asyncio.sleep(0)

        assert slow.closed is True
        assert healthy.closed is False
        assert (await healthy.get()).sequence == 1
        hub.unsubscribe(healthy)

    asyncio.run(scenario())

