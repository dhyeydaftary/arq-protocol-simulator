"""Reference scenarios P0-P6 (hand-derived expectations), run through the engine.

Defaults unless stated: num_frames=8, seq_bits=3, window_size=4,
propagation_delay=2 (RTT 5), timeout=8, retry_limit=10, no random loss.
Frame indices are zero-based absolute indices.
"""

import pytest

from arq import COMPLETED, SimulationConfig, run_simulation

BASE = dict(num_frames=8, seq_bits=3, window_size=4, propagation_delay=2,
            timeout=8, retry_limit=10, frame_loss=0.0, ack_loss=0.0)


def frame(index, occurrence=1):
    return {"kind": "frame", "index": index, "occurrence": occurrence}


def ack(index, occurrence=1):
    return {"kind": "ack", "index": index, "occurrence": occurrence}


SCENARIOS = {
    "P0": dict(scripted_losses=[]),
    "P1": dict(scripted_losses=[frame(2)]),
    "P2": dict(scripted_losses=[ack(1)]),
    "P3": dict(scripted_losses=[ack(7)]),
    "P4": dict(scripted_losses=[frame(2), frame(3), frame(4)]),
    "P5": dict(scripted_losses=[frame(2, 1), frame(2, 2)]),
    "P6": dict(scripted_losses=[], num_frames=4, timeout=4),
}

# (scenario, protocol): (transmissions, retransmissions, completion tick)
EXPECTED = {
    ("P0", "gbn"): (8, 0, 13), ("P0", "sr"): (8, 0, 13),
    ("P1", "gbn"): (12, 4, 25), ("P1", "sr"): (9, 1, 21),
    ("P2", "gbn"): (8, 0, 14), ("P2", "sr"): (9, 1, 21),
    ("P3", "gbn"): (9, 1, 25), ("P3", "sr"): (9, 1, 21),
    ("P4", "gbn"): (12, 4, 25), ("P4", "sr"): (11, 3, 21),
    ("P5", "gbn"): (16, 8, 33), ("P5", "sr"): (10, 2, 29),
    ("P6", "gbn"): (8, 4, 8), ("P6", "sr"): (8, 4, 8),
}


def run(name, protocol):
    return run_simulation(SimulationConfig(protocol=protocol, **{**BASE, **SCENARIOS[name]}))


@pytest.mark.parametrize("name, protocol", sorted(EXPECTED))
def test_reference_scenario(name, protocol):
    result = run(name, protocol)
    m = result.metrics
    assert result.status == COMPLETED
    assert (m.frame_transmissions, m.retransmissions, m.completion_tick) == EXPECTED[(name, protocol)]
    assert m.frames_delivered == SimulationConfig(**{**BASE, **SCENARIOS[name]}).num_frames


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_p0_last_delivery_and_goodput(protocol):
    m = run("P0", protocol).metrics
    assert m.last_delivery_tick == 11
    assert m.goodput == pytest.approx(8 / 13) and round(m.goodput, 3) == 0.615


@pytest.mark.parametrize("protocol, goodput", [("gbn", 0.32), ("sr", 8 / 21)])
def test_p1_goodput(protocol, goodput):
    m = run("P1", protocol).metrics
    assert m.goodput == pytest.approx(goodput)
    if protocol == "sr":
        assert round(m.goodput, 3) == 0.381


def test_p4_gbn_costs_the_same_as_p1():
    p1, p4 = run("P1", "gbn").metrics, run("P4", "gbn").metrics
    assert (p4.frame_transmissions, p4.retransmissions) == (p1.frame_transmissions, p1.retransmissions)


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_p3_every_frame_is_delivered_before_the_sender_knows(protocol):
    result = run("P3", protocol)
    m = result.metrics
    assert m.last_delivery_tick == 11 < m.completion_tick
    completed = [e for e in result.trace if e.type == "completed"]
    assert len(completed) == 1 and completed[0].time == m.completion_tick
    lost = [e for e in result.trace if e.type == "ack_lost"]
    assert [(e.details["frame_index"], e.details["occurrence"]) for e in lost] == [(7, 1)]


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_p6_premature_timeouts_never_cause_duplicate_delivery(protocol):
    result = run("P6", protocol)
    assert result.metrics.goodput == pytest.approx(0.5)
    delivered = [e.details["frame_index"] for e in result.trace if e.type == "frame_delivered"]
    assert delivered == [0, 1, 2, 3]
    assert result.metrics.timeouts >= 1


def test_p1_lost_frame_creates_no_arrival_but_keeps_its_expected_tick():
    result = run("P1", "gbn")
    (lost,) = [e for e in result.trace if e.type == "frame_lost"]
    assert (lost.time, lost.details["frame_index"], lost.details["expected_arrival"]) == (2, 2, 5)
    assert lost.details["cause"] == "scripted"
    received_first_attempt = [e for e in result.trace if e.type == "frame_received"
                              and e.details["frame_index"] == 2 and e.details["attempt"] == 1]
    assert received_first_attempt == []


def test_p1_gbn_timer_restarts_at_ack_arrival():
    # ACK 1 arrives at tick 6, so the base timer for frame 2 expires at 14 (G1).
    result = run("P1", "gbn")
    valid = [e for e in result.trace if e.type == "timeout" and e.details["valid"]]
    assert [(e.time, e.details["frame_index"]) for e in valid] == [(14, 2)]
    resent = [(e.time, e.details["frame_index"]) for e in result.trace
              if e.type == "frame_sent" and e.details["retransmission"]]
    assert resent == [(14, 2), (15, 3), (16, 4), (17, 5)]
