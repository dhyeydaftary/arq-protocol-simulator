"""Scheduled simulation events (Phase 1, Component 2, milestone M1).

These are the entries of the engine's event queue: things that are due to
happen at a future tick. They are NOT the event trace. The trace (what the
frontend shows, with its established schema and vocabulary) is defined in a
later milestone, once its exact schema has been documented and approved.

Ordering rule (confirmed): the queue is ordered by
    (time, priority, insertion counter)
and at the same tick arrivals come first, then timeouts, then transmission
slots. An ACK arriving exactly at a timer's deadline is therefore processed
before that timeout.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass
from enum import Enum, IntEnum


class Priority(IntEnum):
    """Tie-break between events at the same tick. Lower runs first."""

    ARRIVAL = 0
    TIMEOUT = 1
    TRANSMISSION_SLOT = 2


class EventKind(Enum):
    """What a scheduled event is. Each kind has a fixed priority."""

    FRAME_ARRIVAL = "frame_arrival"          # a frame reaches the receiver
    ACK_ARRIVAL = "ack_arrival"              # an ACK reaches the sender
    TIMEOUT = "timeout"                      # a retransmission timer expires
    TRANSMISSION_SLOT = "transmission_slot"  # the sender may start one transmission

    @property
    def priority(self) -> Priority:
        return _PRIORITY[self]


# Frame and ACK arrivals share one priority; between themselves they are
# ordered by insertion counter (they happen at different ends of the link).
_PRIORITY = {
    EventKind.FRAME_ARRIVAL: Priority.ARRIVAL,
    EventKind.ACK_ARRIVAL: Priority.ARRIVAL,
    EventKind.TIMEOUT: Priority.TIMEOUT,
    EventKind.TRANSMISSION_SLOT: Priority.TRANSMISSION_SLOT,
}


@dataclass(frozen=True)
class Event:
    """One scheduled event.

    time:     integer tick at which it is due.
    priority: taken from `kind`; never chosen separately.
    event_id: the queue's insertion counter (0, 1, 2, ...). It reflects the
              order in which events were SCHEDULED, not the order in which
              they are processed, so it must not be used as a chronological
              order of what happened.
    kind:     what the event is.
    payload:  kind-specific data (e.g. a TimeoutPayload); None if not needed.
    """

    time: int
    priority: Priority
    event_id: int
    kind: EventKind
    payload: object = None

    @property
    def sort_key(self) -> tuple[int, int, int]:
        """The queue order: (time, priority, insertion counter)."""
        return (self.time, int(self.priority), self.event_id)


@dataclass(frozen=True)
class TimeoutPayload:
    """Payload of a TIMEOUT event.

    timer_key: which timer (e.g. "base" for Go-Back-N, a wire sequence number
               for Selective Repeat).
    token:     the timer's generation token when the timeout was scheduled.
               The timeout is valid only if that timer is still running with
               this same token; otherwise it is stale and must be ignored.
    """

    timer_key: Hashable
    token: int
