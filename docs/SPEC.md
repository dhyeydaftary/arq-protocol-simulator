# ARQ Protocol Simulator — Reconstructed Specification

> **This is a reconstructed specification, not the original.** The original
> finalized specification is unavailable. This document was written from the
> project handover requirements and the decisions confirmed during
> development (README decisions D1–D9 and later batch approvals). Where it and
> the original differ, the original takes precedence once it is found.
> Items marked *(provisional)* are implementation choices, not confirmed
> requirements.

## 1. Purpose

Simulate Go-Back-N (GBN) and Selective Repeat (SR) ARQ deterministically, so
that every run can be reproduced exactly, explained tick by tick, and compared
between the two protocols.

## 2. Configuration

R2.1 Fields (D1): `protocol`, `seq_bits`, `window_size`, `num_frames`,
`propagation_delay`, `timeout`, `frame_loss`, `ack_loss`, `retry_limit`,
`seed`, `scripted_losses`.

R2.2 `num_frames` is 1–100 (D2). Other bounds are provisional (D2b, D3).

R2.3 With M = 2^seq_bits: GBN `window_size ≤ M − 1`; SR `window_size ≤ M / 2`.

R2.4 Invalid configurations are rejected with every problem reported; legal
but doubtful ones produce warnings *(warning rules provisional, D7)*.

## 3. Timing and ordering

R3.1 Integer ticks. A frame sent at `t` arrives at `t + 1 + P`; an ACK sent at
`a` arrives at `a + P`; RTT = 1 + 2P.

R3.2 The first transmission slot is at tick 0; at most one frame transmission
begins per tick.

R3.3 Events are ordered by `(time, priority, insertion counter)` with
arrivals (0) before timeouts (1) before transmission slots (2). An ACK at a
timer's deadline is processed before the timeout.

## 4. Loss

R4.1 Loss is decided when a transmission begins; a lost transmission produces
no arrival.

R4.2 Random loss uses SHA-256 of `"seed:kind:index:occurrence"` (first 64
bits, big-endian, divided by 2^64, lost if below the probability). Python's
built-in hash function is never used.

R4.3 Occurrences count every send of a `(kind, absolute frame index)`,
including lost ones, counted before the loss decision (D5b). ACK association:
SR — the acknowledged frame; GBN — the highest in-order frame covered (D5a).

R4.4 Scripted losses `{kind, index, occurrence}` force a loss; a transmission
is lost if scripted OR random; both together are one loss with cause "both";
a scripted entry never forces delivery (D5c). Format provisional (D5).

## 5. Protocols

R5.1 GBN: cumulative ACKs; the receiver accepts only the expected frame and
re-ACKs the last in-order frame otherwise (no ACK before the first delivery);
one timer for the oldest unacknowledged frame; a valid timeout resends all
outstanding frames.

R5.2 SR: individual ACKs; one timer per outstanding frame; out-of-order frames
in the window are buffered and delivered in order once the gap is filled;
duplicates are re-ACKed; a valid timeout resends only that frame.

R5.3 Both: no frame is delivered twice; duplicate ACKs never cause fast
retransmit or change retry counts; queued retransmissions take priority over
new frames; protocol decisions use wire sequence numbers and window state,
never the simulator's absolute frame indices.

## 6. Timers and retries (D4, D9)

R6.1 Timers start when transmission begins; deadline = start + timeout.
GBN restarts its timer at the arrival tick of an ACK that advances the base
while frames remain, and when the base's retransmission begins after a
timeout; it stops when nothing is outstanding. SR timers stop on their own
frame's ACK.

R6.2 Generation tokens from one increasing counter; stale timeouts are
ignored completely.

R6.3 `retry_limit` is the per-frame number of retransmissions after the first
send (0 allowed); at most `1 + retry_limit` attempts. It is checked on a valid
timeout; if exhausted, the run ends at that tick with `failed_retry_limit`
before any retransmission. Premature retransmissions count.

## 7. Outcomes

R7.1 `completed`: the sender has accepted ACKs covering every frame; the run
then stops and in-flight events are discarded.

R7.2 `failed_retry_limit`: see R6.3.

R7.3 `aborted_safety_limit`: the next event would exceed `max_events`
(default 1,000,000) processed events, stale timeouts included. `max_events` is
an engine setting, not a configuration field.

R7.4 Impossible internal states are programming errors
(`SimulationInvariantError`), not outcomes.

## 8. Trace

R8.1 Fields: `event_id`, `step`, `time`, `type`, `layer`, `actor`, `state`,
`details`. `step` is processing order; `event_id` identifies the causing
scheduled event (null for `transfer_started` and `aborted`) and is not a
chronological order.

R8.2 Types: `transfer_started`, `frame_sent`, `frame_lost`, `frame_received`,
`frame_delivered`, `ack_sent`, `ack_lost`, `ack_received`, `timeout`,
`send_cancelled`, `window_advanced`, `completed`, `failed`, `aborted`. Stale
timeouts are `timeout` with `details.valid = false`.

R8.3 Entries are immutable; later processing cannot change earlier entries.

## 9. Metrics

R9.1 Metrics are derived from the trace: transmissions, retransmissions,
losses, deliveries, ACK counts, valid and stale timeouts, events processed,
completion tick, and goodput = frames delivered / completion tick (completed
runs only). No channel-utilization metric.

## 10. API

R10.1 `POST /api/simulate`, `GET /api/presets`, `POST /api/compare`, with JSON
errors and no tracebacks sent to clients (see README for schemas).

R10.2 Comparison sweeps: frame loss 0–0.4 in steps of 0.05; window size 1–8
with seq_bits 4. Default 50 runs per point, base seed 42, seeds paired between
GBN and SR, scripted losses rejected. Aggregates: completion rate, mean
goodput over all runs (non-completed = 0), mean goodput over completed runs,
mean retransmissions and transmissions over all runs, mean completion time
over completed runs.

## 11. Reference scenarios

P0–P6 (see README "Presets") with their hand-derived expected results are
regression requirements.
