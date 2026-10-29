"""Event-trace data types (Component 2).

A TraceEvent has the approved fields
    event_id, step, time, type, layer, actor, state, details.
`step` is the processing order; `event_id` identifies the scheduled event
that caused the entry (None for transfer_started and aborted) and must not
be used to order the trace.

Memory: each entry stores a compact, immutable StateSnapshot (tuples of
integers) instead of nested dicts, and consecutive entries with identical
state share one snapshot object. `state` and `to_dict()` rebuild plain dicts
on demand, so the JSON shape is unchanged. Because snapshots and details are
immutable, nothing done later - by the engine or by a caller - can change an
earlier entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

# Approved vocabulary. No other `type` values are produced.
TRACE_TYPES = (
    "transfer_started", "frame_sent", "frame_lost", "frame_received",
    "frame_delivered", "ack_sent", "ack_lost", "ack_received", "timeout",
    "send_cancelled", "window_advanced", "completed", "failed", "aborted",
)
LAYERS = ("simulation", "data_link", "channel", "application")
ACTORS = ("simulator", "sender", "receiver", "channel")


@dataclass(frozen=True)
class StateSnapshot:
    """Sender, receiver and timer state at one moment, as immutable values.

    retries[i] is the retransmission count of outstanding frame base + i.
    acked and buffered are None for GBN (which has neither).
    timers holds (timer key, deadline) pairs for running timers.
    """

    seq_space: int
    base: int
    next: int
    retries: tuple[int, ...]
    retransmit_queue: tuple[int, ...]
    acked: tuple[int, ...] | None
    expected: int
    buffered: tuple[int, ...] | None
    timers: tuple[tuple[str, int], ...]
    delivered: int

    def to_dict(self) -> dict[str, Any]:
        m = self.seq_space
        sender: dict[str, Any] = {
            "base": self.base,
            "next": self.next,
            "base_seq": self.base % m,
            "next_seq": self.next % m,
            "outstanding": list(range(self.base, self.next)),
            "retransmit_queue": list(self.retransmit_queue),
            "retries": {str(self.base + i): r for i, r in enumerate(self.retries)},
        }
        if self.acked is not None:
            sender["acked"] = list(self.acked)
        receiver: dict[str, Any] = {"expected": self.expected, "expected_seq": self.expected % m}
        if self.buffered is not None:
            receiver["buffered"] = list(self.buffered)
        return {
            "sender": sender,
            "receiver": receiver,
            "timers": dict(self.timers),
            "delivered": self.delivered,
        }


def _freeze(value: Any) -> Any:
    """Lists become tuples, so details cannot be changed after recording."""
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


@dataclass(frozen=True)
class TraceEvent:
    """One trace entry. `snapshot` is shared, immutable state; `state`
    returns it as a fresh dict each time."""

    event_id: int | None
    step: int
    time: int
    type: str
    layer: str
    actor: str
    snapshot: StateSnapshot
    details: Mapping[str, Any]

    @classmethod
    def create(cls, event_id: int | None, step: int, time: int, type_: str, layer: str, actor: str,
               snapshot: StateSnapshot, details: dict[str, Any]) -> TraceEvent:
        frozen = MappingProxyType({k: _freeze(v) for k, v in details.items()})
        return cls(event_id, step, time, type_, layer, actor, snapshot, frozen)

    @property
    def state(self) -> dict[str, Any]:
        return self.snapshot.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "step": self.step, "time": self.time,
            "type": self.type, "layer": self.layer, "actor": self.actor,
            "state": self.state,
            "details": {k: _thaw(v) for k, v in self.details.items()},
        }
