"""Protocol interfaces shared by Go-Back-N and Selective Repeat (Component 2, M3).

Senders and receivers are pure state machines. They know nothing about
ticks, the event queue or the channel. The engine calls them and acts on
what they return:

Sender
  next_transmission() -> FrameSend | None   at a transmission slot
  on_ack(seq)         -> AckResult          when an ACK arrives
  on_timeout(key)     -> TimeoutResult      for a VALID timeout only (the
                                            engine drops stale ones first)
  has_pending_send()  -> bool               should a slot be scheduled?
  done                -> bool               every frame acknowledged
Receiver
  on_frame(seq)       -> ReceiveResult      when a frame arrives

Timers: the sender returns TimerCommands ("start" also means restart). The
engine applies them with scheduler.Timers, using deadline = now + timeout,
and schedules the TIMEOUT event.

Absolute indices: protocols keep absolute counters (base, next frame,
expected frame) as their own bookkeeping, and report absolute indices in
their results. But every input from the channel reaches them only as a wire
sequence number, which they map into their current window; decisions about
ACKs and frames are made from that wire number and window state alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Hashable
from dataclasses import dataclass
from typing import Any

from ..config import max_window_size
from ..errors import SimulationInvariantError

START = "start"
STOP = "stop"

DELIVERED = "delivered"    # accepted, and at least one frame passed up in order
BUFFERED = "buffered"      # SR only: accepted out of order, waiting for a gap
DUPLICATE = "duplicate"    # SR only: a copy of a frame already accepted
DISCARDED = "discarded"    # GBN only: not the expected frame
RECEIVE_OUTCOMES = (DELIVERED, BUFFERED, DUPLICATE, DISCARDED)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def index_in_range(seq: int, low: int, high: int, seq_space: int) -> int | None:
    """The absolute index k in [low, high) with k % seq_space == seq, if any.

    The range must be no longer than seq_space, so the answer is unique.
    This is how a wire sequence number is interpreted against a window.
    """
    if high - low > seq_space:
        raise SimulationInvariantError(f"range [{low}, {high}) is longer than M = {seq_space}")
    k = low + (seq - low) % seq_space
    return k if k < high else None


# ---------------------------------------------------------------------------
# Results returned to the engine
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TimerCommand:
    action: str          # START (also restarts) or STOP
    key: Hashable        # "base" for GBN, the wire sequence number for SR


@dataclass(frozen=True)
class FrameSend:
    """A frame transmission the sender has just begun."""

    seq: int
    frame_index: int
    is_retransmission: bool
    retransmissions: int                 # this frame's retransmissions so far, including this one
    timers: tuple[TimerCommand, ...] = ()

    @property
    def attempt(self) -> int:
        """Attempt number of this send (1 = first send). Equals the
        channel's frame occurrence number."""
        return self.retransmissions + 1


@dataclass(frozen=True)
class AckResult:
    """What an arriving ACK did.

    is_duplicate:    True if it acknowledged nothing new (no other effect).
    acked:           absolute indices newly acknowledged, in order.
    window_advanced: True if the window base moved.
    cancelled:       queued retransmissions dropped because they are now
                     acknowledged (absolute indices) - "send cancelled".
    """

    seq: int
    is_duplicate: bool
    acked: tuple[int, ...] = ()
    window_advanced: bool = False
    cancelled: tuple[int, ...] = ()
    timers: tuple[TimerCommand, ...] = ()


@dataclass(frozen=True)
class TimeoutResult:
    """What a valid timeout did.

    frame_index: the frame whose timer expired (the base frame for GBN).
    exhausted:   True if that frame had no retries left. The engine must then
                 end the run with failed_retry_limit at this tick, before
                 any further transmission (D4).
    retransmit:  frames queued for retransmission (empty if exhausted).
    """

    timer_key: Hashable
    frame_index: int
    exhausted: bool
    retransmit: tuple[int, ...] = ()


@dataclass(frozen=True)
class ReceiveResult:
    """What an arriving frame did at the receiver.

    delivered:       absolute indices passed to the upper layer, in order.
    ack_seq:         wire number of the ACK to send, or None for no ACK.
    ack_frame_index: absolute frame the ACK is associated with (D5a), used
                     by the channel for the ACK's loss key.
    """

    seq: int
    outcome: str
    delivered: tuple[int, ...] = ()
    ack_seq: int | None = None
    ack_frame_index: int | None = None


# ---------------------------------------------------------------------------
# Common sender and receiver state
# ---------------------------------------------------------------------------

def _check_config(config: Any, protocol: str) -> None:
    """Defensive check: SimulationConfig already validates all of this."""
    if config.protocol != protocol:
        raise SimulationInvariantError(f"{protocol} protocol given a {config.protocol!r} config")
    limit = max_window_size(protocol, config.seq_bits)
    if not 1 <= config.window_size <= limit:
        raise SimulationInvariantError(
            f"{protocol} window_size {config.window_size} is outside 1..{limit} "
            f"for seq_bits {config.seq_bits}"
        )


class Sender(ABC):
    PROTOCOL = ""

    def __init__(self, config: Any) -> None:
        _check_config(config, self.PROTOCOL)
        self._seq_space = 2 ** config.seq_bits
        self._window = config.window_size
        self._num_frames = config.num_frames
        self._retry_limit = config.retry_limit
        self._base = 0                       # oldest unacknowledged frame
        self._next = 0                       # next new frame to send
        self._retries: dict[int, int] = {}   # outstanding frame -> retransmissions so far
        self._queue: list[int] = []          # retransmissions waiting for a slot, in order
        self.failed_frame: int | None = None

    # -- read-only state ----------------------------------------------------

    @property
    def base(self) -> int:
        return self._base

    @property
    def next_frame(self) -> int:
        return self._next

    @property
    def done(self) -> bool:
        """True once acknowledgements cover every frame."""
        return self._base == self._num_frames

    def outstanding(self) -> tuple[int, ...]:
        """Frames sent but not yet acknowledged by the window base."""
        return tuple(range(self._base, self._next))

    def retransmissions(self, frame_index: int) -> int:
        return self._retries[frame_index]

    def queued_retransmissions(self) -> tuple[int, ...]:
        return tuple(self._queue)

    def has_pending_send(self) -> bool:
        self._require_running()
        return bool(self._queue) or self._can_send_new()

    # -- sending ------------------------------------------------------------

    def next_transmission(self) -> FrameSend | None:
        """Begin the next transmission: a queued retransmission first, else a
        new frame if the window allows, else nothing."""
        self._require_running()
        if self._queue:
            frame = self._queue.pop(0)
            if self._retries[frame] >= self._retry_limit:
                raise SimulationInvariantError(f"frame {frame} would exceed retry_limit {self._retry_limit}")
            self._retries[frame] += 1
            is_retransmission = True
        elif self._can_send_new():
            frame = self._next
            self._next += 1
            self._retries[frame] = 0
            is_retransmission = False
        else:
            return None
        return FrameSend(frame % self._seq_space, frame, is_retransmission,
                         self._retries[frame], self._timers_on_send(frame))

    def _can_send_new(self) -> bool:
        return self._next < self._num_frames and self._next < self._base + self._window

    def _require_running(self) -> None:
        if self.failed_frame is not None:
            raise SimulationInvariantError(f"sender used after frame {self.failed_frame} exhausted its retries")

    def _check_seq(self, seq: Any) -> None:
        if not _is_int(seq) or not 0 <= seq < self._seq_space:
            raise SimulationInvariantError(f"sequence number {seq!r} is outside 0..{self._seq_space - 1}")

    def _exhausted(self, key: Hashable, frame: int) -> TimeoutResult:
        self.failed_frame = frame
        self._queue.clear()
        return TimeoutResult(key, frame, exhausted=True)

    @abstractmethod
    def _timers_on_send(self, frame: int) -> tuple[TimerCommand, ...]: ...

    @abstractmethod
    def on_ack(self, seq: int) -> AckResult: ...

    @abstractmethod
    def on_timeout(self, key: Hashable) -> TimeoutResult: ...

    def snapshot(self) -> dict[str, Any]:
        """Plain, deterministic view of the sender state (for tests and,
        later, the trace's state snapshot)."""
        return {
            "base": self._base,
            "next": self._next,
            "base_seq": self._base % self._seq_space,
            "next_seq": self._next % self._seq_space,
            "outstanding": list(self.outstanding()),
            "retransmit_queue": list(self._queue),
            "retries": {str(k): v for k, v in sorted(self._retries.items())},
        }


class Receiver(ABC):
    PROTOCOL = ""

    def __init__(self, config: Any) -> None:
        _check_config(config, self.PROTOCOL)
        self._seq_space = 2 ** config.seq_bits
        self._window = config.window_size
        self._num_frames = config.num_frames
        self._expected = 0                   # next frame to deliver; = frames delivered so far

    @property
    def delivered_count(self) -> int:
        return self._expected

    def _check_seq(self, seq: Any) -> None:
        if not _is_int(seq) or not 0 <= seq < self._seq_space:
            raise SimulationInvariantError(f"sequence number {seq!r} is outside 0..{self._seq_space - 1}")

    def _deliver(self) -> int:
        """Pass the next frame up. Each frame is delivered exactly once."""
        if self._expected >= self._num_frames:
            raise SimulationInvariantError(f"delivery beyond the last frame ({self._num_frames - 1})")
        frame = self._expected
        self._expected += 1
        return frame

    @abstractmethod
    def on_frame(self, seq: int) -> ReceiveResult: ...

    def snapshot(self) -> dict[str, Any]:
        return {"expected": self._expected, "expected_seq": self._expected % self._seq_space}
