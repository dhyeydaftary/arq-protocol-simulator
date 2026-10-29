"""Tests for Phase 1, Component 2, milestone M2: channel and deterministic loss."""

import hashlib
import os
import pathlib
import subprocess
import sys

import pytest

from arq import SimulationConfig
from arq.channel import (
    ACK, FRAME, Channel, loss_draw, loss_key, loss_value, randomly_lost,
)
from arq.errors import SimulationInvariantError
from arq.protocols import GbnReceiver


def no_loss(**kwargs):
    """A config with random loss switched off (scripted losses still apply)."""
    return SimulationConfig(frame_loss=0.0, ack_loss=0.0, **kwargs)


# --- the SHA-256 loss draw --------------------------------------------------

def test_loss_key_format():
    assert loss_key(42, "frame", 3, 2) == b"42:frame:3:2"
    assert loss_key(0, "ack", 0, 1) == b"0:ack:0:1"


def test_loss_value_is_first_64_bits_of_sha256_big_endian():
    digest = hashlib.sha256(b"42:frame:0:1").digest()
    assert loss_value(42, "frame", 0, 1) == int.from_bytes(digest[:8], "big")


@pytest.mark.parametrize("key, value", [
    ((42, "frame", 0, 1), 4882421309710824278),
    ((42, "frame", 0, 2), 11406326377602675545),
    ((42, "ack", 0, 1), 16787860125670079477),
    ((7, "frame", 3, 2), 7492809344053052235),
])
def test_pinned_loss_values(key, value):
    # Fixed expected values: any change to the loss mechanism shows up here.
    assert loss_value(*key) == value


def test_loss_draw_is_in_unit_interval():
    for occurrence in range(1, 50):
        assert 0.0 <= loss_draw(42, "frame", 5, occurrence) < 1.0


def test_random_loss_compares_draw_with_probability():
    draw = loss_draw(42, "frame", 0, 1)            # about 0.2647
    assert randomly_lost(42, "frame", 0, 1, 0.27)
    assert not randomly_lost(42, "frame", 0, 1, 0.26)
    assert draw < 0.27


def test_probability_zero_never_loses_and_one_always_loses():
    for occurrence in range(1, 200):
        assert not randomly_lost(1, "ack", 2, occurrence, 0.0)
        assert randomly_lost(1, "ack", 2, occurrence, 1.0)


def test_largest_possible_value_is_still_lost_at_probability_one():
    # 2**64 - 1 rounds to 1.0 as a float; the exact integer comparison
    # must still count it as below 1.0.
    assert (2 ** 64 - 1) < 1.0 * 2 ** 64
    assert float(2 ** 64 - 1) / 2 ** 64 == 1.0


def test_every_key_part_changes_the_draw():
    base = loss_value(42, "frame", 3, 2)
    assert loss_value(43, "frame", 3, 2) != base
    assert loss_value(42, "ack", 3, 2) != base
    assert loss_value(42, "frame", 4, 2) != base
    assert loss_value(42, "frame", 3, 3) != base


def test_loss_values_are_the_same_across_processes():
    code = (
        "from arq.channel import loss_value;"
        "print([loss_value(s, k, i, o) for s in (0, 42) for k in ('frame', 'ack')"
        " for i in range(3) for o in (1, 2)])"
    )
    backend = pathlib.Path(__file__).resolve().parents[1]
    outputs = []
    for hash_seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(backend)}
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, check=True)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


# --- timing -----------------------------------------------------------------

def test_frame_arrives_one_tick_plus_propagation_delay_later():
    sent = Channel(no_loss(propagation_delay=2)).send_frame(0, 0, now=3)
    assert (sent.sent_at, sent.arrives_at) == (3, 6)


def test_ack_arrives_propagation_delay_after_it_is_sent():
    sent = Channel(no_loss(propagation_delay=2)).send_ack(0, 0, now=6)
    assert sent.arrives_at == 8


@pytest.mark.parametrize("p", [0, 1, 2, 7])
def test_frame_and_its_ack_take_one_rtt(p):
    config = no_loss(propagation_delay=p)
    channel = Channel(config)
    frame = channel.send_frame(0, 0, now=10)
    ack = channel.send_ack(0, 0, now=frame.arrives_at)
    assert ack.arrives_at - frame.sent_at == config.rtt == 1 + 2 * p


def test_frames_arrive_in_the_order_they_were_sent():
    channel = Channel(no_loss(propagation_delay=3, num_frames=5))
    arrivals = [channel.send_frame(i, i, now=t).arrives_at for i, t in enumerate([0, 1, 4, 5, 9])]
    assert arrivals == sorted(arrivals) == [4, 5, 8, 9, 13]


def test_only_one_frame_may_begin_per_tick():
    channel = Channel(no_loss())
    channel.send_frame(0, 0, now=2)
    with pytest.raises(SimulationInvariantError):
        channel.send_frame(1, 1, now=2)


def test_several_acks_may_be_sent_in_one_tick_in_fifo_order():
    channel = Channel(no_loss())
    first = channel.send_ack(0, 0, now=5)
    second = channel.send_ack(0, 0, now=5)
    assert first.arrives_at == second.arrives_at == 7


def test_time_cannot_go_backwards():
    channel = Channel(no_loss())
    channel.send_frame(0, 0, now=5)
    with pytest.raises(SimulationInvariantError):
        channel.send_ack(0, 0, now=4)


# --- occurrence counting ----------------------------------------------------

def test_lost_sends_still_count_as_occurrences():
    channel = Channel(no_loss(scripted_losses=[{"kind": "frame", "index": 0, "occurrence": 1}]))
    first = channel.send_frame(0, 0, now=0)
    second = channel.send_frame(0, 0, now=8)
    assert (first.occurrence, first.lost) == (1, True)
    assert (second.occurrence, second.lost) == (2, False)
    assert channel.occurrences(FRAME, 0) == 2


def test_frame_and_ack_occurrences_are_counted_separately():
    channel = Channel(no_loss())
    channel.send_frame(0, 0, now=0)
    channel.send_frame(0, 0, now=1)
    channel.send_ack(0, 0, now=3)
    assert channel.occurrences(FRAME, 0) == 2
    assert channel.occurrences(ACK, 0) == 1
    assert channel.occurrences(ACK, 1) == 0


def test_wrapped_frames_have_separate_counters():
    # seq_bits = 3: frames 1 and 9 are both wire number 1.
    channel = Channel(no_loss(seq_bits=3, num_frames=12))
    channel.send_frame(1, 1, now=0)
    frame9 = channel.send_frame(1, 9, now=1)
    ack1 = channel.send_ack(1, 1, now=4)
    ack9 = channel.send_ack(1, 9, now=5)
    assert frame9.occurrence == 1
    assert (ack1.occurrence, ack9.occurrence) == (1, 1)


def test_sequence_number_must_match_the_frame_index():
    with pytest.raises(SimulationInvariantError):
        Channel(no_loss(seq_bits=3)).send_frame(2, 9, now=0)   # 9 % 8 == 1, not 2


@pytest.mark.parametrize("seq, index, now", [
    (-1, 0, 0), (8, 0, 0), (0, -1, 0), (4, 12, 0), (0, 0, -1),
    (0.0, 0, 0), (0, True, 0), ("0", 0, 0), (0, 0, 1.5),
])
def test_malformed_sends_are_rejected(seq, index, now):
    channel = Channel(no_loss(seq_bits=3, num_frames=12))
    with pytest.raises(SimulationInvariantError):
        channel.send_frame(seq, index, now)


# --- scripted, random and combined loss -------------------------------------

def test_no_loss_when_both_probabilities_are_zero():
    channel = Channel(no_loss(num_frames=50))
    assert not any(channel.send_frame(i % 8, i, now=i).lost for i in range(50))


def test_scripted_loss_drops_only_the_targeted_occurrence():
    config = no_loss(scripted_losses=[{"kind": "frame", "index": 3, "occurrence": 2}])
    channel = Channel(config)
    results = [channel.send_frame(3, 3, now=t) for t in (0, 8, 16)]
    assert [(r.occurrence, r.lost, r.loss_cause) for r in results] == [
        (1, False, None), (2, True, "scripted"), (3, False, None),
    ]


def test_scripted_ack_loss_does_not_affect_frames_with_the_same_index():
    channel = Channel(no_loss(scripted_losses=[{"kind": "ack", "index": 0, "occurrence": 1}]))
    assert not channel.send_frame(0, 0, now=0).lost
    ack = channel.send_ack(0, 0, now=3)
    assert (ack.lost, ack.loss_cause) == (True, "scripted")


def test_random_loss_matches_the_sha256_rule():
    # With seed 42 and frame_loss 0.1, frame 9's first attempt draws below 0.1.
    config = SimulationConfig(seed=42, frame_loss=0.1, ack_loss=0.0)
    channel = Channel(config)
    sent = channel.send_frame(9 % 8, 9, now=0)
    assert randomly_lost(42, "frame", 9, 1, 0.1)
    assert (sent.lost, sent.loss_cause) == (True, "random")


def test_random_loss_decision_equals_randomly_lost_for_every_send():
    config = SimulationConfig(seed=7, frame_loss=0.3, ack_loss=0.3, num_frames=40)
    channel = Channel(config)
    for i in range(40):
        frame = channel.send_frame(i % 8, i, now=i)
        assert frame.lost == randomly_lost(7, "frame", i, 1, 0.3)
    for i in range(40):
        ack = channel.send_ack(i % 8, i, now=40 + i)
        assert ack.lost == randomly_lost(7, "ack", i, 1, 0.3)


def test_scripted_and_random_together_is_one_loss_with_cause_both():
    config = SimulationConfig(seed=42, frame_loss=0.1, ack_loss=0.0,
                              scripted_losses=[{"kind": "frame", "index": 9, "occurrence": 1}])
    channel = Channel(config)
    sent = channel.send_frame(1, 9, now=0)
    assert (sent.lost, sent.loss_cause) == (True, "both")
    assert channel.occurrences(FRAME, 9) == 1          # one transmission, one loss


def test_scripted_loss_never_forces_delivery():
    # frame_loss = 1.0 loses everything; a scripted entry cannot rescue it.
    config = SimulationConfig(frame_loss=1.0, ack_loss=0.0,
                              scripted_losses=[{"kind": "frame", "index": 0, "occurrence": 2}])
    channel = Channel(config)
    first = channel.send_frame(0, 0, now=0)
    second = channel.send_frame(0, 0, now=8)
    assert (first.lost, first.loss_cause) == (True, "random")
    assert (second.lost, second.loss_cause) == (True, "both")


def test_adding_a_scripted_loss_does_not_change_other_random_outcomes():
    def outcomes(scripted):
        config = SimulationConfig(seed=3, frame_loss=0.4, ack_loss=0.4, num_frames=30,
                                  scripted_losses=scripted)
        channel = Channel(config)
        return [channel.send_frame(i % 8, i, now=i).lost for i in range(30)]

    plain = outcomes([])
    scripted = outcomes([{"kind": "frame", "index": 5, "occurrence": 1}])
    assert scripted[5] is True
    assert scripted[:5] + scripted[6:] == plain[:5] + plain[6:]


def test_duplicated_scripted_entries_cause_one_loss():
    entry = {"kind": "frame", "index": 0, "occurrence": 1}
    channel = Channel(no_loss(scripted_losses=[entry, entry]))
    sent = channel.send_frame(0, 0, now=0)
    assert (sent.lost, sent.loss_cause) == (True, "scripted")
    assert channel.occurrences(FRAME, 0) == 1


def test_lost_transmission_keeps_its_expected_arrival_tick():
    channel = Channel(no_loss(propagation_delay=2,
                              scripted_losses=[{"kind": "frame", "index": 0, "occurrence": 1}]))
    sent = channel.send_frame(0, 0, now=4)
    assert sent.lost and sent.arrives_at == 7


def test_same_config_gives_the_same_decisions():
    def run():
        channel = Channel(SimulationConfig(seed=11, frame_loss=0.5, ack_loss=0.5, num_frames=20))
        return [channel.send_frame(i % 8, i, now=i) for i in range(20)]
    assert run() == run()


# --- ACK association with the protocols (D5a examples) ----------------------

def test_gbn_duplicate_acks_are_associated_with_the_last_in_order_frame():
    # README example: frames 0-3 sent, frame 1 lost. Frame 0 is delivered,
    # frames 2 and 3 are discarded; ACK events are (0,1), (0,2), (0,3), and
    # {"kind": "ack", "index": 0, "occurrence": 2} drops the second one.
    config = SimulationConfig(protocol="gbn", frame_loss=0.0, ack_loss=0.0,
                              scripted_losses=[{"kind": "frame", "index": 1, "occurrence": 1},
                                               {"kind": "ack", "index": 0, "occurrence": 2}])
    channel, receiver = Channel(config), GbnReceiver(config)
    frames = [channel.send_frame(i, i, now=i) for i in range(4)]   # ticks 0-3
    acks = []
    for frame in frames:                                           # arrivals at 3-6, in order
        if frame.lost:
            continue
        result = receiver.on_frame(frame.seq)
        ack = channel.send_ack(result.ack_seq, result.ack_frame_index, now=frame.arrives_at)
        acks.append((ack.frame_index, ack.occurrence, ack.lost))
    assert acks == [(0, 1, False), (0, 2, True), (0, 3, False)]
