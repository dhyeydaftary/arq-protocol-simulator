"""Tests for Phase 1, Component 2, milestone M3: Go-Back-N and Selective Repeat.

The protocols are tested on their own, by calling them directly. The full
event engine (M4) is not built yet.
"""

import pathlib
import re
from types import SimpleNamespace

import pytest

from arq import SimulationConfig
from arq.channel import Channel
from arq.errors import SimulationInvariantError
from arq.protocols import (
    GbnReceiver, GbnSender, SrReceiver, SrSender, create_protocol,
)
from arq.protocols.base import (
    BUFFERED, DELIVERED, DISCARDED, DUPLICATE, START, STOP, TimerCommand, index_in_range,
)
from arq.scheduler import Timers


def cfg(protocol, **kwargs):
    kwargs.setdefault("frame_loss", 0.0)
    kwargs.setdefault("ack_loss", 0.0)
    return SimulationConfig(protocol=protocol, **kwargs)


def send_all(sender):
    """Begin every transmission the sender currently allows."""
    sends = []
    while (send := sender.next_transmission()) is not None:
        sends.append(send)
    return sends


def run_lossless(config, duplicate_every_frame=False):
    """Drive sender and receiver in lock-step with no loss; return deliveries."""
    sender, receiver = create_protocol(config)
    delivered, acks = [], []
    while not sender.done:
        sends = send_all(sender)
        assert sends, "sender stalled"
        for send in sends:
            for _ in range(2 if duplicate_every_frame else 1):
                result = receiver.on_frame(send.seq)
                delivered.extend(result.delivered)
                acks.append(result.ack_seq)
                sender.on_ack(result.ack_seq)
                sender.check_invariants()
    return delivered, acks, sender, receiver


def apply_timers(timers, commands, deadline):
    """What the engine will do with TimerCommands; returns new tokens."""
    tokens = []
    for command in commands:
        if command.action == START:
            tokens.append(timers.start(command.key, deadline))
        else:
            timers.stop(command.key)
    return tokens


# --- shared helpers ---------------------------------------------------------

def test_index_in_range_maps_wire_numbers_into_a_window():
    assert index_in_range(1, 6, 10, 8) == 9       # window 6..9, wire 1 -> frame 9
    assert index_in_range(6, 6, 10, 8) == 6
    assert index_in_range(3, 6, 10, 8) is None    # wire 3 is not in 6..9
    assert index_in_range(0, 0, 0, 8) is None     # empty range


def test_index_in_range_rejects_ranges_longer_than_m():
    with pytest.raises(SimulationInvariantError):
        index_in_range(0, 0, 9, 8)


def test_create_protocol_picks_the_right_classes():
    assert [type(x) for x in create_protocol(cfg("gbn"))] == [GbnSender, GbnReceiver]
    assert [type(x) for x in create_protocol(cfg("sr"))] == [SrSender, SrReceiver]


@pytest.mark.parametrize("cls, protocol, window, seq_bits", [
    (GbnSender, "gbn", 8, 3), (GbnReceiver, "gbn", 8, 3),   # GBN: W <= M - 1 = 7
    (SrSender, "sr", 5, 3), (SrReceiver, "sr", 5, 3),       # SR:  W <= M / 2 = 4
    (GbnSender, "gbn", 0, 3), (SrSender, "sr", 0, 3),
])
def test_window_size_limits_are_enforced(cls, protocol, window, seq_bits):
    # SimulationConfig already rejects these; the protocols check again.
    bad = SimpleNamespace(protocol=protocol, seq_bits=seq_bits, window_size=window,
                          num_frames=12, retry_limit=10)
    with pytest.raises(SimulationInvariantError):
        cls(bad)


def test_protocol_must_match_the_config():
    with pytest.raises(SimulationInvariantError):
        SrSender(cfg("gbn"))


def test_largest_legal_windows_are_accepted():
    GbnSender(cfg("gbn", seq_bits=3, window_size=7))
    SrSender(cfg("sr", seq_bits=3, window_size=4))


def test_protocol_modules_never_touch_the_channel():
    # Protocols see only wire sequence numbers; absolute indices attached to
    # channel transmissions are bookkeeping they must not read.
    folder = pathlib.Path(__file__).resolve().parents[1] / "arq" / "protocols"
    for path in folder.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(from|import)\b.*\bchannel\b", text, re.MULTILINE), path.name
        assert not re.search(r"\bTransmission\b|\.frame_index\b", text), path.name


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
@pytest.mark.parametrize("bad_seq", [-1, 8, 1.0, None, True])
def test_malformed_sequence_numbers_are_rejected(protocol, bad_seq):
    sender, receiver = create_protocol(cfg(protocol, seq_bits=3))
    send_all(sender)
    with pytest.raises(SimulationInvariantError):
        sender.on_ack(bad_seq)
    with pytest.raises(SimulationInvariantError):
        receiver.on_frame(bad_seq)


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_attempt_number_matches_channel_occurrence(protocol):
    config = cfg(protocol, retry_limit=3)
    sender, channel = create_protocol(config)[0], Channel(config)
    now = 0
    for send in send_all(sender):
        assert channel.send_frame(send.seq, send.frame_index, now).occurrence == send.attempt
        now += 1
    key = "base" if protocol == "gbn" else 0
    sender.on_timeout(key)
    send = sender.next_transmission()
    assert (send.frame_index, send.attempt) == (0, 2)
    assert channel.send_frame(send.seq, send.frame_index, now).occurrence == 2


# --- whole transfers without loss -------------------------------------------

@pytest.mark.parametrize("protocol, seq_bits, window", [
    ("gbn", 3, 4), ("gbn", 2, 3), ("gbn", 1, 1),
    ("sr", 3, 4), ("sr", 2, 2), ("sr", 1, 1),
])
def test_lossless_transfer_delivers_every_frame_once_in_order(protocol, seq_bits, window):
    config = cfg(protocol, seq_bits=seq_bits, window_size=window, num_frames=10)
    delivered, acks, sender, receiver = run_lossless(config)
    assert delivered == list(range(10))
    assert receiver.delivered_count == 10
    assert sender.done and sender.outstanding() == ()


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_sequence_numbers_wrap_around(protocol):
    config = cfg(protocol, seq_bits=2, window_size=2, num_frames=10)
    delivered, acks, *_ = run_lossless(config)
    assert acks == [0, 1, 2, 3, 0, 1, 2, 3, 0, 1]
    assert delivered == list(range(10))


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_duplicate_frames_are_never_delivered_twice(protocol):
    config = cfg(protocol, seq_bits=3, window_size=3, num_frames=9)
    delivered, *_ = run_lossless(config, duplicate_every_frame=True)
    assert delivered == list(range(9))


# --- Go-Back-N sender -------------------------------------------------------

def test_gbn_sends_up_to_the_window_then_waits():
    sender = GbnSender(cfg("gbn", window_size=4, num_frames=12))
    sends = send_all(sender)
    assert [s.seq for s in sends] == [0, 1, 2, 3]
    assert not any(s.is_retransmission for s in sends)
    assert not sender.has_pending_send()


def test_gbn_starts_its_single_timer_on_the_first_send_only():
    sender = GbnSender(cfg("gbn"))
    sends = send_all(sender)
    assert sends[0].timers == (TimerCommand(START, "base"),)
    assert all(s.timers == () for s in sends[1:])
    assert sender.timer_running


def test_gbn_ack_is_cumulative_and_restarts_the_timer():
    sender = GbnSender(cfg("gbn", window_size=4))
    send_all(sender)
    result = sender.on_ack(2)
    assert result.acked == (0, 1, 2)
    assert result.window_advanced and not result.is_duplicate
    assert result.timers == (TimerCommand(START, "base"),)   # frame 3 still outstanding
    assert sender.base == 3
    assert [s.seq for s in send_all(sender)] == [4, 5, 6]


def test_gbn_ack_covering_everything_stops_the_timer():
    sender = GbnSender(cfg("gbn", window_size=4))
    send_all(sender)
    result = sender.on_ack(3)
    assert result.timers == (TimerCommand(STOP, "base"),)
    assert not sender.timer_running


def test_gbn_duplicate_ack_changes_nothing():
    sender = GbnSender(cfg("gbn", window_size=4, retry_limit=3))
    send_all(sender)
    sender.on_ack(0)
    sender.on_timeout("base")
    sender.next_transmission()                  # frame 1 retransmitted, retries = 1
    before = sender.snapshot()
    result = sender.on_ack(0)                   # a repeat of ACK 0
    assert result.is_duplicate and result.acked == () and result.timers == ()
    assert sender.snapshot() == before
    assert sender.retransmissions(1) == 1


def test_gbn_stale_ack_outside_the_window_is_a_duplicate():
    sender = GbnSender(cfg("gbn", window_size=4))
    send_all(sender)                            # frames 0-3 outstanding
    assert sender.on_ack(6).is_duplicate        # wire 6 is not outstanding


def test_gbn_timeout_retransmits_all_outstanding_frames_first():
    sender = GbnSender(cfg("gbn", window_size=4, num_frames=12, retry_limit=2))
    send_all(sender)
    sender.on_ack(0)                            # base = 1; frames 1-3 outstanding
    result = sender.on_timeout("base")
    assert result.retransmit == (1, 2, 3) and not result.exhausted
    assert not sender.timer_running
    sends = send_all(sender)
    assert [(s.frame_index, s.is_retransmission) for s in sends] == [
        (1, True), (2, True), (3, True), (4, False),
    ]
    assert sends[0].timers == (TimerCommand(START, "base"),)   # restarts as the base resend begins
    assert [sender.retransmissions(f) for f in (1, 2, 3, 4)] == [1, 1, 1, 0]


def test_gbn_ack_during_go_back_cancels_queued_retransmissions():
    sender = GbnSender(cfg("gbn", window_size=4))
    send_all(sender)
    sender.on_timeout("base")
    sender.next_transmission()                  # frame 0 resent; 1, 2, 3 still queued
    result = sender.on_ack(1)                   # original ACK for frames 0-1 arrives
    assert result.acked == (0, 1)
    assert result.cancelled == (1,)
    assert sender.queued_retransmissions() == (2, 3)


def test_gbn_retry_limit_zero_fails_on_the_first_timeout():
    sender = GbnSender(cfg("gbn", retry_limit=0))
    send_all(sender)
    result = sender.on_timeout("base")
    assert result.exhausted and result.frame_index == 0 and result.retransmit == ()
    assert sender.failed_frame == 0
    with pytest.raises(SimulationInvariantError):
        sender.next_transmission()


def test_gbn_frame_gets_one_plus_retry_limit_attempts():
    sender = GbnSender(cfg("gbn", window_size=1, retry_limit=2))
    attempts = [sender.next_transmission().attempt]
    for _ in range(2):
        assert not sender.on_timeout("base").exhausted
        attempts.append(sender.next_transmission().attempt)
    assert attempts == [1, 2, 3]
    assert sender.on_timeout("base").exhausted


def test_gbn_base_frame_always_has_the_highest_retry_count():
    sender = GbnSender(cfg("gbn", window_size=4, num_frames=12, retry_limit=5))
    send_all(sender)
    sender.on_timeout("base")
    send_all(sender)
    sender.on_ack(1)                             # base -> 2
    send_all(sender)                             # new frames 4, 5 (count 0)
    sender.on_timeout("base")
    send_all(sender)
    sender.check_invariants()
    counts = [sender.retransmissions(f) for f in sender.outstanding()]
    assert counts == sorted(counts, reverse=True) and counts[0] == 2


def test_gbn_invariant_check_catches_corrupt_retry_counts():
    sender = GbnSender(cfg("gbn"))
    send_all(sender)
    sender._retries[2] = 5                       # deliberately impossible
    with pytest.raises(SimulationInvariantError):
        sender.check_invariants()


@pytest.mark.parametrize("key", ["base", 0, "timer"])
def test_gbn_timeout_without_a_running_timer_is_an_invariant_error(key):
    sender = GbnSender(cfg("gbn"))
    with pytest.raises(SimulationInvariantError):
        sender.on_timeout(key)                   # nothing sent, no timer


def test_gbn_restart_makes_the_old_timeout_stale():
    sender, timers = GbnSender(cfg("gbn", window_size=4)), Timers()
    (first,) = apply_timers(timers, send_all(sender)[0].timers, deadline=8)
    (second,) = apply_timers(timers, sender.on_ack(0).timers, deadline=13)
    assert not timers.is_valid("base", first)
    assert timers.is_valid("base", second)
    apply_timers(timers, sender.on_ack(3).timers, deadline=0)    # all acknowledged: STOP
    assert not timers.is_valid("base", second)


# --- Go-Back-N receiver -----------------------------------------------------

def test_gbn_receiver_delivers_the_expected_frame_and_acks_it():
    receiver = GbnReceiver(cfg("gbn"))
    result = receiver.on_frame(0)
    assert (result.outcome, result.delivered, result.ack_seq, result.ack_frame_index) == (
        DELIVERED, (0,), 0, 0)


def test_gbn_receiver_sends_no_ack_before_the_first_delivery():
    receiver = GbnReceiver(cfg("gbn"))
    result = receiver.on_frame(2)
    assert (result.outcome, result.ack_seq, result.ack_frame_index) == (DISCARDED, None, None)


def test_gbn_receiver_discards_out_of_order_and_reacks_last_in_order():
    receiver = GbnReceiver(cfg("gbn"))
    receiver.on_frame(0)
    for seq in (2, 3):
        result = receiver.on_frame(seq)
        assert (result.outcome, result.delivered, result.ack_seq, result.ack_frame_index) == (
            DISCARDED, (), 0, 0)
    assert receiver.delivered_count == 1


def test_gbn_receiver_reacks_after_wrap_around_with_absolute_association():
    receiver = GbnReceiver(cfg("gbn", seq_bits=3, num_frames=12))
    for i in range(10):
        receiver.on_frame(i % 8)                  # frames 0-9 delivered
    result = receiver.on_frame(3)                 # old duplicate of frame 3
    assert (result.outcome, result.ack_seq, result.ack_frame_index) == (DISCARDED, 1, 9)


def test_gbn_receiver_discards_frames_after_the_last_one():
    receiver = GbnReceiver(cfg("gbn", num_frames=2))
    receiver.on_frame(0)
    receiver.on_frame(1)
    result = receiver.on_frame(2)                 # wire 2 cannot be a new frame
    assert (result.outcome, result.delivered, result.ack_frame_index) == (DISCARDED, (), 1)


# --- Selective Repeat sender ------------------------------------------------

def test_sr_starts_a_timer_per_frame():
    sender = SrSender(cfg("sr", window_size=4))
    sends = send_all(sender)
    assert [s.timers for s in sends] == [(TimerCommand(START, i),) for i in range(4)]


def test_sr_ack_is_individual():
    sender = SrSender(cfg("sr", window_size=4))
    send_all(sender)
    result = sender.on_ack(2)
    assert result.acked == (2,) and not result.window_advanced
    assert result.timers == (TimerCommand(STOP, 2),)
    assert sender.base == 0
    assert sender.on_ack(0).window_advanced and sender.base == 1
    assert sender.on_ack(1).acked == (1,) and sender.base == 3   # slides past acked frame 2
    sender.check_invariants()


def test_sr_duplicate_and_stale_acks_change_nothing():
    sender = SrSender(cfg("sr", window_size=4, retry_limit=3))
    send_all(sender)
    sender.on_ack(2)
    before = sender.snapshot()
    assert sender.on_ack(2).is_duplicate          # already acknowledged
    assert sender.on_ack(6).is_duplicate          # outside the window
    assert sender.snapshot() == before


def test_sr_timeout_retransmits_only_that_frame_before_new_frames():
    sender = SrSender(cfg("sr", window_size=4, num_frames=12, retry_limit=2))
    send_all(sender)
    sender.on_ack(0)                              # window 1-4; frame 4 may now be sent
    result = sender.on_timeout(2)
    assert result.retransmit == (2,) and result.frame_index == 2
    sends = send_all(sender)
    assert [(s.frame_index, s.is_retransmission) for s in sends] == [(2, True), (4, False)]
    assert sends[0].timers == (TimerCommand(START, 2),)
    assert [sender.retransmissions(f) for f in (1, 2, 3, 4)] == [0, 1, 0, 0]
    sender.check_invariants()


def test_sr_ack_for_a_queued_frame_cancels_its_retransmission():
    sender = SrSender(cfg("sr", window_size=4))
    send_all(sender)
    sender.on_timeout(1)
    result = sender.on_ack(1)
    assert result.cancelled == (1,) and result.timers == ()   # its timer was not running
    assert sender.queued_retransmissions() == ()


def test_sr_retry_exhaustion_fails_the_transfer():
    sender = SrSender(cfg("sr", retry_limit=1))
    send_all(sender)
    sender.on_timeout(3)
    sender.next_transmission()                    # frame 3 retransmitted once
    result = sender.on_timeout(3)
    assert result.exhausted and result.frame_index == 3
    assert sender.failed_frame == 3
    with pytest.raises(SimulationInvariantError):
        sender.on_ack(0)


def test_sr_timeout_for_a_frame_without_a_running_timer_is_an_invariant_error():
    sender = SrSender(cfg("sr", window_size=4))
    send_all(sender)
    sender.on_ack(1)
    with pytest.raises(SimulationInvariantError):
        sender.on_timeout(1)                      # stopped by its ACK
    with pytest.raises(SimulationInvariantError):
        sender.on_timeout(5)                      # never sent


def test_sr_stop_makes_that_frames_timeout_stale_only():
    sender, timers = SrSender(cfg("sr", window_size=4)), Timers()
    tokens = [apply_timers(timers, s.timers, deadline=8 + i)[0] for i, s in enumerate(send_all(sender))]
    apply_timers(timers, sender.on_ack(1).timers, deadline=0)
    assert [timers.is_valid(i, t) for i, t in enumerate(tokens)] == [True, False, True, True]


def test_sr_reused_key_after_wrap_around_gets_a_new_token():
    config = cfg("sr", seq_bits=2, window_size=2, num_frames=6)
    sender, timers = SrSender(config), Timers()
    old = [apply_timers(timers, s.timers, deadline=8)[0] for s in send_all(sender)]   # frames 0, 1
    for seq in (0, 1):
        apply_timers(timers, sender.on_ack(seq).timers, deadline=0)
    send_all(sender)                              # frames 2, 3
    for seq in (2, 3):
        apply_timers(timers, sender.on_ack(seq).timers, deadline=0)
    (new,) = apply_timers(timers, sender.next_transmission().timers, deadline=20)   # frame 4, wire 0
    assert new not in old
    assert not timers.is_valid(0, old[0]) and timers.is_valid(0, new)


# --- Selective Repeat receiver ----------------------------------------------

def test_sr_receiver_buffers_out_of_order_frames_and_acks_each():
    receiver = SrReceiver(cfg("sr", window_size=4))
    for seq in (1, 2):
        result = receiver.on_frame(seq)
        assert (result.outcome, result.delivered, result.ack_seq, result.ack_frame_index) == (
            BUFFERED, (), seq, seq)
    assert receiver.snapshot()["buffered"] == [1, 2]


def test_sr_receiver_delivers_buffered_frames_when_the_gap_is_filled():
    receiver = SrReceiver(cfg("sr", window_size=4))
    receiver.on_frame(1)
    receiver.on_frame(2)
    result = receiver.on_frame(0)
    assert (result.outcome, result.delivered, result.ack_seq) == (DELIVERED, (0, 1, 2), 0)
    assert receiver.delivered_count == 3 and receiver.snapshot()["buffered"] == []


def test_sr_receiver_reacks_a_duplicate_of_a_buffered_frame():
    receiver = SrReceiver(cfg("sr", window_size=4))
    receiver.on_frame(2)
    result = receiver.on_frame(2)
    assert (result.outcome, result.delivered, result.ack_seq, result.ack_frame_index) == (
        DUPLICATE, (), 2, 2)


def test_sr_receiver_reacks_a_duplicate_of_a_delivered_frame():
    # README D5a example: frame 2's ACK is lost, frame 2 is resent and re-ACKed.
    receiver = SrReceiver(cfg("sr", window_size=4))
    for seq in (0, 1, 2):
        receiver.on_frame(seq)
    result = receiver.on_frame(2)
    assert (result.outcome, result.delivered, result.ack_seq, result.ack_frame_index) == (
        DUPLICATE, (), 2, 2)
    assert receiver.delivered_count == 3


def test_sr_receiver_duplicate_after_wrap_around_maps_to_the_right_frame():
    receiver = SrReceiver(cfg("sr", seq_bits=3, window_size=4, num_frames=12))
    for i in range(10):
        receiver.on_frame(i % 8)                  # frames 0-9 delivered; window 10-13
    result = receiver.on_frame(7)                 # wire 7 = frame 7, in the previous window
    assert (result.outcome, result.ack_frame_index) == (DUPLICATE, 7)


def test_sr_receiver_rejects_impossible_sequence_numbers():
    # M = 8, W = 2, expected frame 4: valid numbers are 2-3 (old) and 4-5 (new).
    receiver = SrReceiver(cfg("sr", seq_bits=3, window_size=2, num_frames=12))
    for seq in range(4):
        receiver.on_frame(seq)
    with pytest.raises(SimulationInvariantError):
        receiver.on_frame(7)


def test_sr_receiver_rejects_frames_beyond_the_last_one():
    receiver = SrReceiver(cfg("sr", window_size=4, num_frames=2))
    with pytest.raises(SimulationInvariantError):
        receiver.on_frame(3)


def test_sr_receiver_has_no_previous_window_at_the_start():
    receiver = SrReceiver(cfg("sr", seq_bits=3, window_size=4))
    with pytest.raises(SimulationInvariantError):
        receiver.on_frame(7)                      # would be frame -1


# --- determinism ------------------------------------------------------------

@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_same_inputs_give_the_same_states(protocol):
    def run():
        sender = create_protocol(cfg(protocol, retry_limit=3))[0]
        send_all(sender)
        sender.on_ack(1 if protocol == "gbn" else 2)
        sender.on_timeout("base" if protocol == "gbn" else 0)
        send_all(sender)
        return sender.snapshot()
    assert run() == run()
