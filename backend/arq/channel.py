"""Channel: transmission timing and deterministic loss (Component 2, M2).

Timing (established model, integer ticks, P = propagation_delay):
* a frame whose transmission begins at tick t occupies the link for one tick
  and arrives at the receiver at t + 1 + P;
* an ACK is sent at the tick its frame arrives and arrives P ticks later,
  so an ACK for a frame sent at t arrives at t + 1 + 2P = t + RTT.
At most one frame transmission may begin per tick. With a constant delay,
both directions are therefore first-in first-out; the channel checks this.

Loss (README decisions D5, D5a-D5c):
* Loss is decided when a transmission begins. The occurrence counter for its
  (kind, absolute frame index) goes up first, so lost sends count too.
* Random loss: SHA-256 of the key "seed:kind:index:occurrence" (ASCII). The
  first 64 bits of the digest, as an unsigned big-endian integer, divided by
  2**64 give a value in [0, 1). The transmission is randomly lost when that
  value is less than the loss probability. The comparison is done exactly in
  integers (value < probability * 2**64), so 0.0 never loses and 1.0 always
  loses, with no floating-point rounding at the edges.
* Scripted loss: lost if a scripted entry matches (kind, index, occurrence).
* Lost if scripted OR random. Both together are still ONE loss, with cause
  "both". A scripted entry never forces delivery.
Each decision depends only on its own key, so adding a scripted loss never
changes any other transmission's random outcome.

The channel does not schedule events. It tells the engine whether a
transmission is lost and when it would arrive; the engine schedules an
arrival only for transmissions that are not lost.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .config import SimulationConfig
from .errors import SimulationInvariantError

FRAME = "frame"
ACK = "ack"
LOSS_CAUSES = ("random", "scripted", "both")

_TWO_TO_64 = 2 ** 64


def _is_int(value: object) -> bool:
    """True for real integers (bool is rejected, as in config.py)."""
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Deterministic random loss
# ---------------------------------------------------------------------------

def loss_key(seed: int, kind: str, index: int, occurrence: int) -> bytes:
    """The exact bytes fed to SHA-256, e.g. b"42:frame:3:2"."""
    return f"{seed}:{kind}:{index}:{occurrence}".encode("ascii")


def loss_value(seed: int, kind: str, index: int, occurrence: int) -> int:
    """First 64 bits of SHA-256(loss_key), as an unsigned big-endian integer."""
    digest = hashlib.sha256(loss_key(seed, kind, index, occurrence)).digest()
    return int.from_bytes(digest[:8], "big")


def loss_draw(seed: int, kind: str, index: int, occurrence: int) -> float:
    """loss_value / 2**64, a number in [0, 1). For display; decisions use
    `randomly_lost`, which compares exactly."""
    return loss_value(seed, kind, index, occurrence) / _TWO_TO_64


def randomly_lost(seed: int, kind: str, index: int, occurrence: int, probability: float) -> bool:
    """True if the deterministic random draw falls below `probability`."""
    # probability * 2**64 is exact (scaling by a power of two), and Python
    # compares an int with a float exactly.
    return loss_value(seed, kind, index, occurrence) < probability * _TWO_TO_64


# ---------------------------------------------------------------------------
# Transmissions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Transmission:
    """One frame or ACK send, with its loss decision.

    kind:        "frame" or "ack"
    seq:         wire sequence number (0 .. M-1)
    frame_index: absolute frame index - for frames the frame itself, for ACKs
                 the frame the ACK is associated with (D5a). Bookkeeping and
                 reporting only; protocols never read it.
    occurrence:  1 for the first send of this (kind, frame_index), then 2, ...
                 For frames this is the attempt number.
    sent_at:     tick the transmission begins
    arrives_at:  tick it reaches the other end - or would have, if lost
    lost:        whether it is lost
    loss_cause:  "random", "scripted", "both", or None if not lost
    """

    kind: str
    seq: int
    frame_index: int
    occurrence: int
    sent_at: int
    arrives_at: int
    lost: bool
    loss_cause: str | None


class Channel:
    """Decides loss and arrival time for every frame and ACK send."""

    def __init__(self, config: SimulationConfig) -> None:
        self._seed = config.seed
        self._delay = config.propagation_delay
        self._seq_space = config.seq_space
        self._num_frames = config.num_frames
        self._probability = {FRAME: config.frame_loss, ACK: config.ack_loss}
        # Duplicated entries are harmless: a set holds each target once.
        self._scripted = frozenset(
            (loss.kind, loss.index, loss.occurrence) for loss in config.scripted_losses
        )
        self._occurrences: dict[tuple[str, int], int] = {}
        self._now = 0
        self._last_frame_start: int | None = None
        self._last_arrival = {FRAME: -1, ACK: -1}

    def occurrences(self, kind: str, frame_index: int) -> int:
        """How many sends of this kind have been made for this frame so far."""
        return self._occurrences.get((kind, frame_index), 0)

    def send_frame(self, seq: int, frame_index: int, now: int) -> Transmission:
        """A frame transmission begins at `now`."""
        if self._last_frame_start is not None and _is_int(now) and now <= self._last_frame_start:
            raise SimulationInvariantError(
                f"frame transmission at tick {now}: the link is busy (a frame "
                f"began at tick {self._last_frame_start}; one frame per tick)"
            )
        sent = self._send(FRAME, seq, frame_index, now, delay=1 + self._delay)
        self._last_frame_start = now
        return sent

    def send_ack(self, seq: int, frame_index: int, now: int) -> Transmission:
        """An ACK associated with absolute frame `frame_index` is sent at `now`."""
        return self._send(ACK, seq, frame_index, now, delay=self._delay)

    def _send(self, kind: str, seq: int, frame_index: int, now: int, delay: int) -> Transmission:
        self._check(kind, seq, frame_index, now)
        arrives_at = now + delay
        if arrives_at < self._last_arrival[kind]:
            raise SimulationInvariantError(
                f"{kind} arriving at tick {arrives_at} would overtake one arriving "
                f"at tick {self._last_arrival[kind]} (the channel is FIFO)"
            )

        occurrence = self.occurrences(kind, frame_index) + 1   # counted before the loss decision
        self._occurrences[(kind, frame_index)] = occurrence

        random_loss = randomly_lost(self._seed, kind, frame_index, occurrence, self._probability[kind])
        scripted_loss = (kind, frame_index, occurrence) in self._scripted
        if random_loss and scripted_loss:
            cause = "both"
        elif random_loss:
            cause = "random"
        elif scripted_loss:
            cause = "scripted"
        else:
            cause = None

        self._now = now
        self._last_arrival[kind] = arrives_at
        return Transmission(kind, seq, frame_index, occurrence, now, arrives_at,
                            lost=cause is not None, loss_cause=cause)

    def _check(self, kind: str, seq: int, frame_index: int, now: int) -> None:
        if not _is_int(now) or now < self._now:
            raise SimulationInvariantError(f"{kind} send at tick {now!r}: time cannot go backwards from {self._now}")
        if not _is_int(seq) or not 0 <= seq < self._seq_space:
            raise SimulationInvariantError(f"{kind} sequence number {seq!r} is outside 0..{self._seq_space - 1}")
        if not _is_int(frame_index) or not 0 <= frame_index < self._num_frames:
            raise SimulationInvariantError(f"{kind} frame index {frame_index!r} is outside 0..{self._num_frames - 1}")
        if seq != frame_index % self._seq_space:
            raise SimulationInvariantError(
                f"{kind} sequence number {seq} does not match frame {frame_index} "
                f"(expected {frame_index % self._seq_space})"
            )
