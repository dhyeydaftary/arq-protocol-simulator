"""Go-Back-N sender and receiver (Component 2, M3).

Sender (window <= M - 1):
* One timer, key "base", for the oldest unacknowledged frame (D9):
  - started when a frame begins transmission and no timer is running;
  - restarted when an ACK advances the base and frames remain outstanding;
  - stopped when nothing is outstanding;
  - after a timeout it is not running, so it restarts when the base frame's
    retransmission begins.
* Cumulative ACKs: an ACK for wire number a acknowledges every outstanding
  frame up to the one numbered a. Any other ACK is a duplicate and changes
  nothing (no fast retransmit, no retry counter change).
* Valid timeout (D4): if the base frame already used retry_limit
  retransmissions, the transfer is exhausted. Otherwise every outstanding
  frame is queued for retransmission, in order; each one's retry count goes
  up when its retransmission begins.

Receiver:
* Accepts only the expected frame, delivers it and ACKs it.
* Discards anything else, re-ACKing the last in-order frame - or sending no
  ACK at all before the first delivery.
"""

from __future__ import annotations

from collections.abc import Hashable

from ..errors import SimulationInvariantError
from .base import (
    DELIVERED, DISCARDED, START, STOP,
    AckResult, Receiver, ReceiveResult, Sender, TimeoutResult, TimerCommand,
    index_in_range,
)

TIMER_KEY = "base"


class GbnSender(Sender):
    PROTOCOL = "gbn"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._timer_running = False

    @property
    def timer_running(self) -> bool:
        return self._timer_running

    def _timers_on_send(self, frame: int) -> tuple[TimerCommand, ...]:
        if self._timer_running:
            return ()
        self._timer_running = True
        return (TimerCommand(START, TIMER_KEY),)

    def on_ack(self, seq: int) -> AckResult:
        self._require_running()
        self._check_seq(seq)
        newest = index_in_range(seq, self._base, self._next, self._seq_space)
        if newest is None:
            return AckResult(seq, is_duplicate=True)

        acked = tuple(range(self._base, newest + 1))
        for frame in acked:
            del self._retries[frame]
        self._base = newest + 1
        cancelled = tuple(f for f in self._queue if f < self._base)
        self._queue = [f for f in self._queue if f >= self._base]

        if self._base == self._next:
            self._timer_running = False
            timers = (TimerCommand(STOP, TIMER_KEY),)
        else:
            self._timer_running = True
            timers = (TimerCommand(START, TIMER_KEY),)   # restart at this ACK's arrival tick
        return AckResult(seq, False, acked, True, cancelled, timers)

    def on_timeout(self, key: Hashable) -> TimeoutResult:
        self._require_running()
        if key != TIMER_KEY or not self._timer_running or self._base == self._next:
            raise SimulationInvariantError(
                f"GBN timeout for {key!r} with timer running={self._timer_running} "
                f"and outstanding frames {list(self.outstanding())}"
            )
        self._timer_running = False
        if self._retries[self._base] >= self._retry_limit:
            return self._exhausted(key, self._base)
        self._queue = list(range(self._base, self._next))
        return TimeoutResult(key, self._base, exhausted=False, retransmit=tuple(self._queue))

    def check_invariants(self) -> None:
        """The base frame has the highest retry count: every go-back that
        resent a later frame also resent all earlier outstanding ones."""
        counts = [self._retries[f] for f in self.outstanding()]
        if counts != sorted(counts, reverse=True):
            raise SimulationInvariantError(f"GBN retry counts not non-increasing from the base: {counts}")
        if len(counts) > self._window:
            raise SimulationInvariantError(f"{len(counts)} frames outstanding, window is {self._window}")


class GbnReceiver(Receiver):
    PROTOCOL = "gbn"

    def on_frame(self, seq: int) -> ReceiveResult:
        self._check_seq(seq)
        if self._expected < self._num_frames and seq == self._expected % self._seq_space:
            frame = self._deliver()
            return ReceiveResult(seq, DELIVERED, (frame,), seq, frame)
        if self._expected == 0:
            return ReceiveResult(seq, DISCARDED)   # nothing delivered yet: no ACK
        last = self._expected - 1
        return ReceiveResult(seq, DISCARDED, (), last % self._seq_space, last)
