"""Event-driven simulation engine (Phase 1, Component 2, milestones M4-M5).

run_simulation(config) runs one transfer and returns a SimulationResult with
its outcome, its event trace and its metrics. Everything is deterministic:
the same config always gives the same result.

The engine owns the EventQueue and Timers, and coordinates the Channel and
the protocol's sender and receiver:

  TRANSMISSION_SLOT  the sender may begin one frame (retransmissions first);
                     the channel decides loss; an arrival is scheduled only
                     if the frame is not lost.
  FRAME_ARRIVAL      the receiver accepts, buffers or discards the frame,
                     delivers what it can and may send an ACK.
  ACK_ARRIVAL        the sender processes the ACK; completion is declared once
                     acknowledgements cover every frame.
  TIMEOUT            stale timeouts (token mismatch) are traced and ignored;
                     a valid one either fails the run (retries exhausted) or
                     queues retransmissions.

Timer commands from the sender are applied here: "start" (also restart) sets
deadline = now + timeout and schedules a TIMEOUT carrying the new token.

Outcomes: "completed", "failed_retry_limit", "aborted_safety_limit". The
safety limit counts every popped event, stale timeouts included; when the
next event would exceed it, the run stops before processing that event.
Impossible internal states raise SimulationInvariantError instead.

Trace entries (TraceEvent) are numbered by `step`, the order in which they
were recorded. `event_id` identifies the scheduled event that caused an
entry; it reflects scheduling order and must not be used to order the trace.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .channel import Channel, Transmission
from .config import SimulationConfig
from .errors import SimulationInvariantError
from .events import Event, EventKind, TimeoutPayload
from .metrics import Metrics, compute_metrics
from .protocols import create_protocol
from .protocols.base import START, STOP, TimerCommand
from .scheduler import EventQueue, Timers
from .trace import TRACE_TYPES, StateSnapshot, TraceEvent

DEFAULT_MAX_EVENTS = 1_000_000

COMPLETED = "completed"
FAILED_RETRY_LIMIT = "failed_retry_limit"
ABORTED_SAFETY_LIMIT = "aborted_safety_limit"
STATUSES = (COMPLETED, FAILED_RETRY_LIMIT, ABORTED_SAFETY_LIMIT)

# Layers and actors used in the trace.
SIMULATION, DATA_LINK, CHANNEL_LAYER, APPLICATION = "simulation", "data_link", "channel", "application"
SIMULATOR, SENDER, RECEIVER, CHANNEL_ACTOR = "simulator", "sender", "receiver", "channel"


def config_to_dict(config: SimulationConfig) -> dict[str, Any]:
    """Every config field as plain JSON data (scripted losses as dicts)."""
    data = asdict(config)
    data["scripted_losses"] = [dict(loss) for loss in data["scripted_losses"]]
    return data


@dataclass(frozen=True)
class SimulationResult:
    status: str                   # one of STATUSES
    end_tick: int                 # tick of the last processed event
    events_processed: int
    failed_frame: int | None      # absolute index, for failed_retry_limit
    trace: tuple[TraceEvent, ...]
    metrics: Metrics

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "end_tick": self.end_tick,
            "events_processed": self.events_processed, "failed_frame": self.failed_frame,
            "metrics": self.metrics.to_dict(),
            "trace": [entry.to_dict() for entry in self.trace],
        }


def run_simulation(config: SimulationConfig, *, max_events: int = DEFAULT_MAX_EVENTS) -> SimulationResult:
    """Run one transfer. `max_events` is an engine safety setting, not a
    user-facing configuration field."""
    if not isinstance(config, SimulationConfig):
        raise TypeError(f"config must be a SimulationConfig, got {type(config).__name__}")
    if not isinstance(max_events, int) or isinstance(max_events, bool) or max_events < 1:
        raise ValueError(f"max_events must be an integer >= 1, got {max_events!r}")
    return _Simulation(config, max_events).run()


class _Simulation:
    def __init__(self, config: SimulationConfig, max_events: int) -> None:
        self.config = config
        self.max_events = max_events
        self.queue = EventQueue()
        self.timers = Timers()
        self.channel = Channel(config)
        self.sender, self.receiver = create_protocol(config)
        self.trace: list[TraceEvent] = []
        self.events_processed = 0
        self.end_tick = 0
        self.status: str | None = None
        self.failed_frame: int | None = None
        self.delivered: list[int] = []
        self.slot_pending = False
        self.link_free_at = 0
        self.current: Event | None = None
        self.last_snapshot: StateSnapshot | None = None

    # -- main loop ------------------------------------------------------------

    def run(self) -> SimulationResult:
        self.record("transfer_started", SIMULATION, SIMULATOR, event_id=None, time=0,
                    protocol=self.config.protocol, num_frames=self.config.num_frames,
                    window_size=self.config.window_size, seq_space=self.config.seq_space,
                    timeout=self.config.timeout, rtt=self.config.rtt)
        self.ensure_slot()

        while self.status is None:
            if not len(self.queue):
                raise SimulationInvariantError("event queue empty before the transfer ended")
            if self.events_processed >= self.max_events:
                self.status = ABORTED_SAFETY_LIMIT
                self.record("aborted", SIMULATION, SIMULATOR, event_id=None, time=self.end_tick,
                            reason="safety_limit", max_events=self.max_events)
                break
            event = self.queue.pop()
            self.events_processed += 1
            self.end_tick = event.time
            self.current = event
            self.dispatch(event)
            if self.status is None:
                self.sender.check_invariants()

        metrics = compute_metrics(self.trace, status=self.status, end_tick=self.end_tick,
                                  events_processed=self.events_processed)
        return SimulationResult(self.status, self.end_tick, self.events_processed,
                                self.failed_frame, tuple(self.trace), metrics)

    def dispatch(self, event: Event) -> None:
        if event.kind is EventKind.TRANSMISSION_SLOT:
            self.on_slot()
        elif event.kind is EventKind.FRAME_ARRIVAL:
            self.on_frame_arrival(event.payload)
        elif event.kind is EventKind.ACK_ARRIVAL:
            self.on_ack_arrival(event.payload)
        elif event.kind is EventKind.TIMEOUT:
            self.on_timeout(event.payload)
        else:
            raise SimulationInvariantError(f"no handler for {event.kind!r}")

    # -- handlers -------------------------------------------------------------

    def on_slot(self) -> None:
        now = self.queue.now
        self.slot_pending = False
        if now < self.link_free_at:
            raise SimulationInvariantError(f"transmission slot at tick {now}, link busy until {self.link_free_at}")
        send = self.sender.next_transmission()
        if send is None:
            return   # e.g. an ACK cancelled the queued retransmission
        sent = self.channel.send_frame(send.seq, send.frame_index, now)
        if sent.occurrence != send.attempt:
            raise SimulationInvariantError(
                f"frame {send.frame_index}: channel occurrence {sent.occurrence} != attempt {send.attempt}"
            )
        self.link_free_at = now + 1
        self.apply_timers(send.timers)
        self.record("frame_sent", DATA_LINK, SENDER, seq=send.seq, frame_index=send.frame_index,
                    attempt=send.attempt, retransmission=send.is_retransmission,
                    arrives_at=sent.arrives_at)
        if sent.lost:
            self.record_loss("frame_lost", sent)
        else:
            self.queue.schedule(sent.arrives_at, EventKind.FRAME_ARRIVAL, sent)
        self.ensure_slot()

    def on_frame_arrival(self, sent: Transmission) -> None:
        now = self.queue.now
        result = self.receiver.on_frame(sent.seq)
        self.record("frame_received", DATA_LINK, RECEIVER, seq=sent.seq, frame_index=sent.frame_index,
                    attempt=sent.occurrence, outcome=result.outcome)
        if result.outcome in ("buffered", "duplicate") and result.ack_frame_index != sent.frame_index:
            raise SimulationInvariantError(
                f"receiver took sequence number {sent.seq} for frame {result.ack_frame_index}, "
                f"but it was frame {sent.frame_index}"
            )
        for frame in result.delivered:
            if frame != len(self.delivered):
                raise SimulationInvariantError(f"delivered frame {frame}, expected {len(self.delivered)}")
            self.delivered.append(frame)
            self.record("frame_delivered", APPLICATION, RECEIVER, seq=frame % self.config.seq_space,
                        frame_index=frame)
        if result.delivered and sent.frame_index not in result.delivered:
            raise SimulationInvariantError(f"frame {sent.frame_index} arrived but frames {result.delivered} were delivered")

        if result.ack_seq is not None:
            ack = self.channel.send_ack(result.ack_seq, result.ack_frame_index, now)
            self.record("ack_sent", DATA_LINK, RECEIVER, seq=ack.seq, frame_index=ack.frame_index,
                        occurrence=ack.occurrence, arrives_at=ack.arrives_at)
            if ack.lost:
                self.record_loss("ack_lost", ack)
            else:
                self.queue.schedule(ack.arrives_at, EventKind.ACK_ARRIVAL, ack)

    def on_ack_arrival(self, ack: Transmission) -> None:
        old_base = self.sender.base
        result = self.sender.on_ack(ack.seq)
        if not result.is_duplicate and result.acked[-1] != ack.frame_index:
            raise SimulationInvariantError(
                f"sender took ACK {ack.seq} for frame {result.acked[-1]}, but it was for frame {ack.frame_index}"
            )
        self.apply_timers(result.timers)
        self.record("ack_received", DATA_LINK, SENDER, seq=ack.seq, frame_index=ack.frame_index,
                    occurrence=ack.occurrence, duplicate=result.is_duplicate, acked=list(result.acked))
        for frame in result.cancelled:
            self.record("send_cancelled", DATA_LINK, SENDER, seq=frame % self.config.seq_space,
                        frame_index=frame, reason="acknowledged")
        if result.window_advanced:
            self.record("window_advanced", DATA_LINK, SENDER, old_base=old_base, new_base=self.sender.base)

        if self.sender.done:
            if len(self.delivered) != self.config.num_frames:
                raise SimulationInvariantError(
                    f"sender done but only {len(self.delivered)} of {self.config.num_frames} frames delivered"
                )
            self.status = COMPLETED
            self.record("completed", SIMULATION, SIMULATOR, frames_delivered=len(self.delivered))
            return
        self.ensure_slot()

    def on_timeout(self, payload: TimeoutPayload) -> None:
        key, token = payload.timer_key, payload.token
        if not self.timers.is_valid(key, token):
            self.record("timeout", DATA_LINK, SENDER, timer=str(key), token=token, valid=False)
            return
        self.timers.stop(key)   # it has expired
        result = self.sender.on_timeout(key)
        self.record("timeout", DATA_LINK, SENDER, timer=str(key), token=token, valid=True,
                    frame_index=result.frame_index, exhausted=result.exhausted,
                    retransmit=list(result.retransmit))
        if result.exhausted:
            self.status = FAILED_RETRY_LIMIT
            self.failed_frame = result.frame_index
            self.record("failed", SIMULATION, SIMULATOR, reason="retry_limit",
                        frame_index=result.frame_index, retry_limit=self.config.retry_limit)
            return
        self.ensure_slot()

    # -- helpers --------------------------------------------------------------

    def ensure_slot(self) -> None:
        """Schedule one transmission slot if the sender has something to send."""
        if self.slot_pending or not self.sender.has_pending_send():
            return
        self.queue.schedule(max(self.queue.now, self.link_free_at), EventKind.TRANSMISSION_SLOT)
        self.slot_pending = True

    def apply_timers(self, commands: tuple[TimerCommand, ...]) -> None:
        now = self.queue.now
        for command in commands:
            if command.action == START:
                deadline = now + self.config.timeout
                token = self.timers.start(command.key, deadline)
                self.queue.schedule(deadline, EventKind.TIMEOUT, TimeoutPayload(command.key, token))
            elif command.action == STOP:
                self.timers.stop(command.key)
            else:
                raise SimulationInvariantError(f"unknown timer command {command.action!r}")

    def record_loss(self, kind: str, sent: Transmission) -> None:
        self.record(kind, CHANNEL_LAYER, CHANNEL_ACTOR, seq=sent.seq, frame_index=sent.frame_index,
                    occurrence=sent.occurrence, cause=sent.loss_cause, expected_arrival=sent.arrives_at)

    def snapshot(self) -> StateSnapshot:
        """Compact, immutable state. Reuses the previous snapshot object when
        nothing changed, so runs of entries share one copy."""
        sender, receiver = self.sender.snapshot(), self.receiver.snapshot()
        if self.config.protocol == "gbn":
            keys = ["base"]
        else:
            keys = [frame % self.config.seq_space for frame in self.sender.outstanding()]
        snapshot = StateSnapshot(
            seq_space=self.config.seq_space,
            base=sender["base"],
            next=sender["next"],
            retries=tuple(sender["retries"][str(f)] for f in sender["outstanding"]),
            retransmit_queue=tuple(sender["retransmit_queue"]),
            acked=tuple(sender["acked"]) if "acked" in sender else None,
            expected=receiver["expected"],
            buffered=tuple(receiver["buffered"]) if "buffered" in receiver else None,
            timers=tuple((str(k), self.timers.deadline(k)) for k in keys if self.timers.is_running(k)),
            delivered=len(self.delivered),
        )
        if snapshot == self.last_snapshot:
            return self.last_snapshot
        self.last_snapshot = snapshot
        return snapshot

    def record(self, type_: str, layer: str, actor: str, *, event_id: Any = ..., time: int | None = None,
               **details: Any) -> None:
        if type_ not in TRACE_TYPES:
            raise SimulationInvariantError(f"unknown trace type {type_!r}")
        if event_id is ...:
            event_id = self.current.event_id
        self.trace.append(TraceEvent.create(
            event_id, len(self.trace), self.queue.now if time is None else time,
            type_, layer, actor, self.snapshot(), details,
        ))
