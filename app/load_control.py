"""One process: bounded FIFO API queue and one shared inference lock."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import time


class Busy(Exception):
    pass


class QueueTimeout(Exception):
    pass


@dataclass(eq=False)
class Ticket:
    count: int
    ready: asyncio.Future
    task_type: str = "sentiment"
    created: float = 0


class LoadControl:
    def __init__(self, cabinet_busy=lambda: False, max_waiting=5, max_messages=2000, concurrency=1, priorities=None, aging_seconds=10):
        self.gpu = asyncio.Semaphore(concurrency)
        self.priorities = priorities or {}
        self.aging_seconds = aging_seconds
        self.cabinet_busy = cabinet_busy
        self.max_waiting, self.max_messages = max_waiting, max_messages
        self.active = None
        self.waiting = []

    @property
    def api_pending(self):
        return self.active is not None or bool(self.waiting)

    def release(self, ticket):
        if ticket in self.waiting:
            self.waiting.remove(ticket)
        elif self.active is ticket:
            self.active = None
        if self.active is None:
            while self.waiting:
                # Lower priority value wins; aging prevents starvation.
                now = time.monotonic()
                candidate = min(self.waiting, key=lambda t:
                    self.priorities.get(t.task_type, 0) - (now-t.created)/self.aging_seconds)
                self.waiting.remove(candidate)
                if not candidate.ready.cancelled():
                    self.active = candidate
                    candidate.ready.set_result(None)
                    break

    @asynccontextmanager
    async def slot(self, count, timeout, task_type="sentiment"):
        # No await between checking cabinet state and reserving a ticket.
        if self.cabinet_busy():
            raise Busy()
        ticket = Ticket(count, asyncio.get_running_loop().create_future(), task_type, time.monotonic())
        if self.active is None:
            self.active = ticket
            ticket.ready.set_result(None)
        else:
            if len(self.waiting) >= self.max_waiting or sum(t.count for t in self.waiting) + count > self.max_messages:
                raise Busy()
            self.waiting.append(ticket)
        started = time.monotonic()
        try:
            try:
                await asyncio.wait_for(asyncio.shield(ticket.ready), timeout)
            except TimeoutError as exc:
                raise QueueTimeout() from exc
            yield time.monotonic() - started
        finally:
            ticket.ready.cancel()
            self.release(ticket)
