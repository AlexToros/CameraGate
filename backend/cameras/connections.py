"""Bounded connect admission, favoring visible cameras without starving warmup."""
from collections import deque
from contextlib import contextmanager
from heapq import merge
import threading


class ConnectionGate:
    def __init__(self, limit=2):
        self.limit = limit
        self.running = self.foreground_runs = 0
        self.priority = set()
        self.waiting = [deque(), deque()]
        self.condition = threading.Condition()
        self.closed = False
        self._sequence = 0

    def prioritize(self, camera_ids):
        wanted = set(camera_ids)
        with self.condition:
            if wanted == self.priority:
                return
            self.priority = wanted
            # Repartition pending tickets once per changed wall, not per frame.
            tickets = merge(self.waiting[0], self.waiting[1])
            self.waiting = [deque(), deque()]
            for ticket in tickets:
                self.waiting[0 if ticket[1] is None or ticket[1] in wanted else 1].append(ticket)
            self.condition.notify_all()

    def _next(self):
        foreground, background = self.waiting
        # Reserve every eighth admission for background when both queues wait.
        return background if background and (not foreground or self.foreground_runs >= 7) else foreground

    @contextmanager
    def slot(self, camera_id):
        with self.condition:
            # A monotonically increasing ticket preserves FIFO across reprioritization.
            self._sequence += 1
            ticket = (self._sequence, camera_id)
            self.waiting[0 if camera_id is None or camera_id in self.priority else 1].append(ticket)
            self.condition.notify_all()
            self.condition.wait_for(lambda: self.closed or (
                self.running < self.limit and self._next() and self._next()[0] == ticket))
            if self.closed:
                raise ConnectionError('Connection gate closed')
            selected = self._next()
            selected.popleft()
            self.foreground_runs = min(7,self.foreground_runs+1) if selected is self.waiting[0] else 0
            self.running += 1
            self.condition.notify_all()
        try:
            yield
        finally:
            with self.condition:
                self.running -= 1
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class ControlSlot:
    """Reusable context for the independent recorder-event control connection."""
    def __init__(self, gate):
        self.gate = gate

    def __enter__(self):
        self.context = self.gate.slot(None)
        return self.context.__enter__()

    def __exit__(self, *args):
        return self.context.__exit__(*args)
