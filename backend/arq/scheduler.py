"""Deterministic event queue and retransmission timers (Component 2, M1).

EventQueue  - events come out in (time, priority, insertion counter) order.
              Nothing here depends on wall-clock time, randomness or Python's
              built-in hash function, so the same calls always give the same
              order.
Timers      - running timers and their generation tokens. A timeout is valid
              only if its timer is still running with the same token (README,
              decision D9).

Neither class knows what events or timers mean; the engine decides that.
"""

from __future__ import annotations

import heapq
from collections.abc import Hashable

from .errors import SimulationInvariantError
from .events import Event, EventKind


def _is_int(value: object) -> bool:
    """True for real integers (bool is rejected, as in config.py)."""
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Event queue
# ---------------------------------------------------------------------------

class EventQueue:
    """Priority queue of scheduled events.

    `now` is the time of the most recently popped event (0 before any pop).
    An event may not be scheduled to run before the event currently being
    processed: its (time, priority) must be at least the (time, priority) of
    the last popped event. So at the current tick, a timeout may schedule a
    transmission slot, but nothing may schedule an arrival "in the past" of
    the current timeout. Violations raise SimulationInvariantError.
    """

    def __init__(self) -> None:
        self._heap: list[tuple[int, int, int, Event]] = []
        self._next_id = 0
        self._now = 0
        self._current_priority = 0  # priority of the last popped event

    @property
    def now(self) -> int:
        return self._now

    def __len__(self) -> int:
        return len(self._heap)

    def schedule(self, time: int, kind: EventKind, payload: object = None) -> Event:
        """Add an event and return it. Its event_id is the insertion counter."""
        if not isinstance(kind, EventKind):
            raise SimulationInvariantError(f"unknown event kind {kind!r}")
        if not _is_int(time):
            raise SimulationInvariantError(f"event time must be an integer tick, got {time!r}")
        priority = kind.priority
        if (time, int(priority)) < (self._now, self._current_priority):
            raise SimulationInvariantError(
                f"cannot schedule {kind.value} at tick {time} (priority {int(priority)}): "
                f"the queue is already at tick {self._now} (priority {self._current_priority})"
            )
        event = Event(time, priority, self._next_id, kind, payload)
        self._next_id += 1
        heapq.heappush(self._heap, (*event.sort_key, event))
        return event

    def pop(self) -> Event:
        """Remove and return the next event, advancing `now` to its time."""
        if not self._heap:
            raise SimulationInvariantError("pop from an empty event queue")
        *_, event = heapq.heappop(self._heap)
        self._now = event.time
        self._current_priority = int(event.priority)
        return event


# ---------------------------------------------------------------------------
# Retransmission timers
# ---------------------------------------------------------------------------

class Timers:
    """Running timers, each identified by a key, with generation tokens.

    Every start (or restart) takes the next value of ONE counter shared by
    all timers, so tokens are globally increasing and never reused - even
    when a key (such as a wire sequence number) is reused after wrap-around.
    Stopping a timer forgets its token. Old TIMEOUT events are not removed
    from the queue; they are recognised as stale by `is_valid`.
    """

    def __init__(self) -> None:
        self._running: dict[Hashable, tuple[int, int]] = {}  # key -> (token, deadline)
        self._last_token = 0

    def start(self, key: Hashable, deadline: int) -> int:
        """Start, or restart, the timer `key`; return its new token."""
        if not _is_int(deadline):
            raise SimulationInvariantError(f"timer deadline must be an integer tick, got {deadline!r}")
        self._last_token += 1
        self._running[key] = (self._last_token, deadline)
        return self._last_token

    def stop(self, key: Hashable) -> bool:
        """Stop the timer `key`. Return True if it was running."""
        return self._running.pop(key, None) is not None

    def is_running(self, key: Hashable) -> bool:
        return key in self._running

    def token(self, key: Hashable) -> int | None:
        """Current token of a running timer, or None if it is not running."""
        entry = self._running.get(key)
        return entry[0] if entry else None

    def deadline(self, key: Hashable) -> int | None:
        """Deadline of a running timer, or None if it is not running."""
        entry = self._running.get(key)
        return entry[1] if entry else None

    def is_valid(self, key: Hashable, token: int) -> bool:
        """True if a timeout carrying (key, token) is current, not stale."""
        entry = self._running.get(key)
        return entry is not None and entry[0] == token
