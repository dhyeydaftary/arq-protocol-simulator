"""Run metrics, derived from the event trace (Component 2, M5).

The trace is the single source of truth: every count below is a count of
trace entries, so metrics always agree with what the trace shows. Only
events_processed comes from the engine, because some processed events (a
transmission slot with nothing left to send) leave no trace entry.

Goodput convention: frames delivered divided by the completion time, where
the transfer starts at tick 0, so the completion time is the completion tick.
It is only defined for completed runs. There is no channel-utilization
metric, because channel utilization is not modelled.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Metrics:
    status: str
    end_tick: int
    completion_tick: int | None        # end_tick if completed, else None
    last_delivery_tick: int | None
    events_processed: int
    frame_transmissions: int           # frames that began transmission, including retransmissions
    retransmissions: int
    frames_lost: int                   # lost frame transmissions
    frames_received: int
    duplicate_frames: int              # SR duplicates of already-accepted frames
    discarded_frames: int              # GBN frames other than the expected one
    buffered_frames: int               # SR out-of-order frames accepted into the buffer
    frames_delivered: int
    acks_sent: int
    acks_lost: int
    acks_received: int
    duplicate_acks: int
    timeouts: int                      # valid timeouts
    stale_timeouts: int
    sends_cancelled: int
    goodput: float | None              # frames_delivered / completion_tick

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_metrics(trace: Iterable[Any], *, status: str, end_tick: int, events_processed: int) -> Metrics:
    """Count trace entries (TraceEvent objects or their dict form)."""
    # Only type, time and details are needed; avoid rebuilding state dicts.
    entries = [e if isinstance(e, dict) else {"type": e.type, "time": e.time, "details": e.details}
               for e in trace]

    def count(type_: str, **match: Any) -> int:
        return sum(1 for e in entries
                   if e["type"] == type_ and all(e["details"].get(k) == v for k, v in match.items()))

    deliveries = [e["time"] for e in entries if e["type"] == "frame_delivered"]
    frames_delivered = len(deliveries)
    completion_tick = end_tick if status == "completed" else None
    goodput = frames_delivered / completion_tick if completion_tick else None

    return Metrics(
        status=status,
        end_tick=end_tick,
        completion_tick=completion_tick,
        last_delivery_tick=max(deliveries) if deliveries else None,
        events_processed=events_processed,
        frame_transmissions=count("frame_sent"),
        retransmissions=count("frame_sent", retransmission=True),
        frames_lost=count("frame_lost"),
        frames_received=count("frame_received"),
        duplicate_frames=count("frame_received", outcome="duplicate"),
        discarded_frames=count("frame_received", outcome="discarded"),
        buffered_frames=count("frame_received", outcome="buffered"),
        frames_delivered=frames_delivered,
        acks_sent=count("ack_sent"),
        acks_lost=count("ack_lost"),
        acks_received=count("ack_received"),
        duplicate_acks=count("ack_received", duplicate=True),
        timeouts=count("timeout", valid=True),
        stale_timeouts=count("timeout", valid=False),
        sends_cancelled=count("send_cancelled"),
        goodput=goodput,
    )
