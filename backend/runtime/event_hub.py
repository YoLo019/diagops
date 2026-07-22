from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from threading import RLock

from backend.domain.runtime import RuntimeEvent
from backend.runtime.faults import NoFaultInjector


class EventHubOverflow(RuntimeError):
    """单个订阅者积压超过边界；客户端应从最后 durable sequence 重连。"""


@dataclass(eq=False, slots=True)
class EventSubscription:
    run_id: str
    queue: asyncio.Queue[RuntimeEvent]
    loop: asyncio.AbstractEventLoop
    closed: bool = False
    _hub: EventHub | None = field(default=None, repr=False)

    async def get(self) -> RuntimeEvent:
        if self.closed and self.queue.empty():
            raise EventHubOverflow("runtime event subscriber overflowed")
        event = await self.queue.get()
        self.queue.task_done()
        return event


class EventHub:
    """提交后事件的有界进程内通知层；数据库始终是 catch-up 真相源。"""

    def __init__(self, *, max_queue_size: int = 256, fault_injector=None) -> None:
        if max_queue_size < 1:
            raise ValueError("event subscriber queue size must be positive")
        self._max_queue_size = max_queue_size
        self._subscribers: dict[str, set[EventSubscription]] = {}
        self._lock = RLock()
        self.fault_injector = fault_injector or NoFaultInjector()

    def subscribe(self, run_id: str) -> EventSubscription:
        subscription = EventSubscription(
            run_id=run_id,
            queue=asyncio.Queue(maxsize=self._max_queue_size),
            loop=asyncio.get_running_loop(),
            _hub=self,
        )
        with self._lock:
            self._subscribers.setdefault(run_id, set()).add(subscription)
        return subscription

    def unsubscribe(self, subscription: EventSubscription) -> None:
        with self._lock:
            subscription.closed = True
            subscribers = self._subscribers.get(subscription.run_id)
            if subscribers is None:
                return
            subscribers.discard(subscription)
            if not subscribers:
                self._subscribers.pop(subscription.run_id, None)

    def publish(self, events: list[RuntimeEvent]) -> None:
        """只投递轻量通知且从不等待慢消费者，因而不会阻塞 RuntimeWriter。"""
        for event in events:
            with self._lock:
                subscriptions = tuple(self._subscribers.get(event.run_id, ()))
            for subscription in subscriptions:
                if subscription.closed or subscription.loop.is_closed():
                    self.unsubscribe(subscription)
                    continue
                subscription.loop.call_soon_threadsafe(
                    self._deliver,
                    subscription,
                    event.model_copy(deep=True),
                )

    def _deliver(
        self,
        subscription: EventSubscription,
        event: RuntimeEvent,
    ) -> None:
        if subscription.closed:
            return
        try:
            subscription.queue.put_nowait(event)
        except asyncio.QueueFull:
            # 只关闭溢出的订阅者，其他 Run 和 Writer 均不受影响。
            self.unsubscribe(subscription)

    async def stream(
        self,
        store,
        run_id: str,
        *,
        after: int,
        heartbeat_seconds: float,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> AsyncIterator[RuntimeEvent | None]:
        """先订阅再查 durable events，并以 sequence 去重消除竞态窗口。"""
        if after > 0:
            self.fault_injector.hit("sse_reconnect")
        subscription = self.subscribe(run_id)
        last_sequence = after

        async def catch_up() -> AsyncIterator[RuntimeEvent]:
            nonlocal last_sequence
            while True:
                batch = store.list_events(
                    run_id,
                    after=last_sequence,
                    limit=500,
                )
                ordered = sorted(batch, key=lambda item: item.sequence)
                delivered = 0
                for event in ordered:
                    if event.sequence <= last_sequence:
                        continue
                    last_sequence = event.sequence
                    delivered += 1
                    yield event
                if len(batch) < 500 or delivered == 0:
                    return

        try:
            async for event in catch_up():
                yield event
            while True:
                if is_disconnected is not None and await is_disconnected():
                    return
                try:
                    await asyncio.wait_for(
                        subscription.get(),
                        timeout=heartbeat_seconds,
                    )
                except TimeoutError:
                    yield None
                    continue
                except EventHubOverflow:
                    return
                # live queue 只负责唤醒；重新查询 durable rows 可覆盖乱序通知和竞态。
                async for event in catch_up():
                    yield event
        finally:
            self.unsubscribe(subscription)
