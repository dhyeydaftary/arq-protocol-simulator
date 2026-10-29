"""Selective Repeat sender and receiver (Component 2, M3).

Sender (window <= M / 2):
* One timer per outstanding frame, keyed by its wire sequence number (D9).
  It starts or restarts when that frame's transmission begins and stops
  when that frame's own ACK is accepted. The window size limit makes the
  key unique among outstanding frames; generation tokens keep reused keys
  apart after wrap-around.
* Individual ACKs: an ACK acknowledges only its own frame. An ACK for a
  frame already acknowledged, or outside the window, is a duplicate and
  changes nothing (no fast retransmit, no retry counter change).
* Valid timeout (D4): if that frame already used retry_limit
  retransmissions, the transfer is exhausted. Otherwise only that frame is
  queued for retransmission; its retry count goes up when it begins.

Receiver:
* A frame in the receive window [expected, expected + W) is accepted
  (buffered if out of order) and ACKed. When the frame at the window base
  arrives, it and every buffered frame after it are delivered in order.
* A frame already accepted - still buffered, or in [expected - W, expected)
  and so already delivered - is a duplicate: re-ACKed, never delivered again.
* Any other sequence number cannot occur when the window size limit holds,
  so it raises SimulationInvariantError.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from ..errors import SimulationInvariantError
from .base import (
    BUFFERED, DELIVERED, DUPLICATE, START, STOP,
    AckResult, Receiver, ReceiveResult, Sender, TimeoutResult, TimerCommand,
    index_in_range,
)


class SrSender(Sender):
    PROTOCOL = "sr"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._acked: set[int] = set()    # outstanding frames acknowledged out of order
        self._timing: set[int] = set()   # outstanding frames whose timer is running

    def timer_running(self, frame_index: int) -> bool:
        return frame_index in self._timing

    def _timers_on_send(self, frame: int) -> tuple[TimerCommand, ...]:
        self._timing.add(frame)
        return (TimerCommand(START, frame % self._seq_space),)

    def on_ack(self, seq: int) -> AckResult:
        self._require_running()
        self._check_seq(seq)
        frame = index_in_range(seq, self._base, self._next, self._seq_space)
        if frame is None or frame in self._acked:
            return AckResult(seq, is_duplicate=True)

        self._acked.add(frame)
        timers: tuple[TimerCommand, ...] = ()
        if frame in self._timing:
            self._timing.remove(frame)
            timers = (TimerCommand(STOP, seq),)
        cancelled: tuple[int, ...] = ()
        if frame in self._queue:
            self._queue.remove(frame)
            cancelled = (frame,)

        old_base = self._base
        while self._base in self._acked:
            self._acked.remove(self._base)
            del self._retries[self._base]
            self._base += 1
        return AckResult(seq, False, (frame,), self._base != old_base, cancelled, timers)

    def on_timeout(self, key: Hashable) -> TimeoutResult:
        self._require_running()
        self._check_seq(key)
        frame = index_in_range(key, self._base, self._next, self._seq_space)
        if frame is None or frame not in self._timing:
            raise SimulationInvariantError(f"SR timeout for sequence number {key!r}, which has no running timer")
        self._timing.remove(frame)
        if self._retries[frame] >= self._retry_limit:
            return self._exhausted(key, frame)
        self._queue.append(frame)
        return TimeoutResult(key, frame, exhausted=False, retransmit=(frame,))

    def check_invariants(self) -> None:
        """Every outstanding, unacknowledged frame has exactly one of: a
        running timer, or a place in the retransmission queue."""
        if len(self.outstanding()) > self._window:
            raise SimulationInvariantError(f"{len(self.outstanding())} frames outstanding, window is {self._window}")
        for frame in self.outstanding():
            states = [frame in self._acked, frame in self._timing, frame in self._queue]
            if states.count(True) != 1:
                raise SimulationInvariantError(
                    f"frame {frame}: acked={states[0]} timing={states[1]} queued={states[2]}"
                )

    def snapshot(self) -> dict[str, Any]:
        state = super().snapshot()
        state["acked"] = sorted(self._acked)
        return state


class SrReceiver(Receiver):
    PROTOCOL = "sr"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._buffer: set[int] = set()   # accepted out-of-order frames, all > expected

    def on_frame(self, seq: int) -> ReceiveResult:
        self._check_seq(seq)
        frame = index_in_range(seq, self._expected, self._expected + self._window, self._seq_space)
        if frame is not None:
            if frame >= self._num_frames:
                raise SimulationInvariantError(f"frame {frame} (sequence number {seq}) does not exist")
            if frame in self._buffer:
                return ReceiveResult(seq, DUPLICATE, (), seq, frame)
            if frame != self._expected:
                self._buffer.add(frame)
                return ReceiveResult(seq, BUFFERED, (), seq, frame)
            delivered = [self._deliver()]
            while self._expected in self._buffer:
                self._buffer.remove(self._expected)
                delivered.append(self._deliver())
            return ReceiveResult(seq, DELIVERED, tuple(delivered), seq, frame)

        low = max(0, self._expected - self._window)
        frame = index_in_range(seq, low, self._expected, self._seq_space)
        if frame is not None:
            return ReceiveResult(seq, DUPLICATE, (), seq, frame)
        raise SimulationInvariantError(
            f"sequence number {seq} is outside the receive window and the "
            f"previous window (expected frame {self._expected}, window {self._window})"
        )

    def snapshot(self) -> dict[str, Any]:
        state = super().snapshot()
        state["buffered"] = sorted(self._buffer)
        return state
