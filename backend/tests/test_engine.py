"""Integration tests for Phase 1, Component 2, milestones M4-M5: engine,
failure handling, trace and metrics."""

import json
import os
import pathlib
import subprocess
import sys

import pytest

from arq import (
    ABORTED_SAFETY_LIMIT, COMPLETED, FAILED_RETRY_LIMIT, SimulationConfig, run_simulation,
)
from arq.engine import TRACE_TYPES
from arq.metrics import compute_metrics


def cfg(protocol="gbn", **kwargs):
    kwargs.setdefault("frame_loss", 0.0)
    kwargs.setdefault("ack_loss", 0.0)
    return SimulationConfig(protocol=protocol, **kwargs)


def lose(kind, index, occurrence=1):
    return {"kind": kind, "index": index, "occurrence": occurrence}


def of_type(result, type_):
    return [e for e in result.trace if e.type == type_]


def delivered(result):
    return [e.details["frame_index"] for e in of_type(result, "frame_delivered")]


# --- argument checks --------------------------------------------------------

@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10"])
def test_max_events_must_be_a_positive_integer(bad):
    with pytest.raises(ValueError):
        run_simulation(cfg(), max_events=bad)


def test_config_must_be_a_simulation_config():
    with pytest.raises(TypeError):
        run_simulation({"protocol": "gbn"})


# --- basic transfers --------------------------------------------------------

@pytest.mark.parametrize("protocol", ["gbn", "sr"])
@pytest.mark.parametrize("p", [0, 1, 3])
def test_lossless_transfer_with_a_big_window_completes_at_last_send_plus_rtt(protocol, p):
    config = cfg(protocol, num_frames=4, window_size=4, propagation_delay=p, timeout=20)
    result = run_simulation(config)
    assert result.status == COMPLETED
    assert result.metrics.completion_tick == (4 - 1) + config.rtt
    assert [e.time for e in of_type(result, "frame_sent")] == [0, 1, 2, 3]


@pytest.mark.parametrize("protocol, seq_bits, window", [("gbn", 2, 3), ("sr", 2, 2), ("gbn", 1, 1), ("sr", 1, 1)])
def test_sequence_numbers_wrap_around_in_a_full_run(protocol, seq_bits, window):
    config = cfg(protocol, seq_bits=seq_bits, window_size=window, num_frames=20,
                 scripted_losses=[lose("frame", 5), lose("ack", 9), lose("frame", 13, 1)])
    result = run_simulation(config)
    assert result.status == COMPLETED
    assert delivered(result) == list(range(20))
    m = 2 ** seq_bits
    for entry in of_type(result, "frame_sent"):
        assert entry.details["seq"] == entry.details["frame_index"] % m


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
@pytest.mark.parametrize("seed", range(8))
def test_random_loss_runs_complete_and_deliver_in_order(protocol, seed):
    config = SimulationConfig(protocol=protocol, seed=seed, frame_loss=0.2, ack_loss=0.2,
                              num_frames=20, retry_limit=10)
    result = run_simulation(config)
    assert result.status == COMPLETED
    assert delivered(result) == list(range(20))


# --- timer behaviour ---------------------------------------------------------

def test_ack_at_the_deadline_is_processed_before_the_timeout():
    # timeout == RTT == 5, retry_limit 0: frame 0's ACK arrives exactly at its
    # deadline. The ACK wins, the timeout is stale, and nothing fails.
    config = cfg("gbn", num_frames=2, window_size=1, propagation_delay=2, timeout=5, retry_limit=0)
    result = run_simulation(config)
    assert result.status == COMPLETED
    at_5 = [(e.type, e.details.get("valid")) for e in result.trace if e.time == 5]
    assert at_5 == [("ack_received", None), ("window_advanced", None), ("timeout", False),
                    ("frame_sent", None)]
    assert result.metrics.retransmissions == 0


def test_stale_timeouts_never_retransmit():
    # P0 for GBN: the timer started at 0 (deadline 8) is restarted at tick 5,
    # so the timeout at 8 is stale.
    result = run_simulation(cfg("gbn", num_frames=8))
    stale = [e for e in of_type(result, "timeout") if not e.details["valid"]]
    assert [e.time for e in stale] == [8]
    assert result.metrics.retransmissions == 0
    assert result.metrics.stale_timeouts == 1 and result.metrics.timeouts == 0


def test_retransmissions_go_before_new_frames():
    # timeout 4 < RTT 5: the base times out at 4 and frames 0-3 are queued.
    # The original ACK 0 arrives at 5 and opens room for frame 4, but the
    # queued retransmissions of 1, 2, 3 are sent first.
    result = run_simulation(cfg("gbn", num_frames=8, timeout=4, retry_limit=5))
    sends = [(e.time, e.details["frame_index"], e.details["retransmission"]) for e in of_type(result, "frame_sent")]
    assert sends[:9] == [(0, 0, False), (1, 1, False), (2, 2, False), (3, 3, False),
                         (4, 0, True), (5, 1, True), (6, 2, True), (7, 3, True), (8, 4, False)]


def test_gbn_ack_during_go_back_cancels_queued_retransmissions():
    # ACKs for frames 0 and 1 are lost, so the base times out at 6 and frames
    # 0-5 are queued. Frame 0 is resent at 6; at 7 the cumulative ACK for
    # frame 2 arrives and cancels the queued resends of frames 1 and 2.
    result = run_simulation(cfg("gbn", seq_bits=4, window_size=8, num_frames=8, timeout=6, retry_limit=5,
                                scripted_losses=[lose("ack", 0), lose("ack", 1)]))
    assert result.status == COMPLETED
    cancelled = of_type(result, "send_cancelled")
    assert [(e.time, e.details["frame_index"]) for e in cancelled] == [(7, 1), (7, 2)]
    assert result.metrics.sends_cancelled == 2
    sent_after = {(e.details["frame_index"], e.time) for e in of_type(result, "frame_sent")}
    for entry in cancelled:   # a cancelled retransmission is never sent later
        later = [t for (f, t) in sent_after if f == entry.details["frame_index"] and t > entry.time]
        assert later == []


# --- retry limit and failure -------------------------------------------------

@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_retry_limit_zero_fails_at_the_first_valid_timeout(protocol):
    config = cfg(protocol, num_frames=4, retry_limit=0, scripted_losses=[lose("frame", 0)])
    result = run_simulation(config)
    assert result.status == FAILED_RETRY_LIMIT
    assert (result.end_tick, result.failed_frame) == (8, 0)
    assert result.trace[-1].type == "failed" and result.trace[-1].time == 8
    assert result.metrics.retransmissions == 0
    assert result.metrics.goodput is None and result.metrics.completion_tick is None


def test_retry_limit_zero_with_timeout_below_rtt_fails_without_any_loss():
    result = run_simulation(cfg("sr", num_frames=1, timeout=4, retry_limit=0))
    assert (result.status, result.end_tick) == (FAILED_RETRY_LIMIT, 4)


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_repeated_loss_fails_after_one_plus_retry_limit_attempts(protocol):
    config = cfg(protocol, num_frames=1, retry_limit=2,
                 scripted_losses=[lose("frame", 0, o) for o in (1, 2, 3)])
    result = run_simulation(config)
    assert (result.status, result.end_tick, result.failed_frame) == (FAILED_RETRY_LIMIT, 24, 0)
    assert [e.time for e in of_type(result, "frame_sent")] == [0, 8, 16]
    assert result.metrics.frame_transmissions == 3 and result.metrics.retransmissions == 2


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_last_allowed_attempt_succeeding_completes(protocol):
    config = cfg(protocol, num_frames=1, retry_limit=2,
                 scripted_losses=[lose("frame", 0, o) for o in (1, 2)])
    result = run_simulation(config)
    assert (result.status, result.metrics.completion_tick) == (COMPLETED, 21)


def test_lost_final_attempt_fails_only_when_its_timeout_fires():
    config = cfg("sr", num_frames=1, retry_limit=1, scripted_losses=[lose("frame", 0, 1), lose("frame", 0, 2)])
    result = run_simulation(config)
    second_loss = of_type(result, "frame_lost")[-1]
    assert second_loss.time == 8 and result.end_tick == 16


def test_sr_failure_of_one_frame_fails_the_whole_transfer():
    config = cfg("sr", num_frames=8, retry_limit=1,
                 scripted_losses=[lose("frame", 5, 1), lose("frame", 5, 2)])
    result = run_simulation(config)
    assert result.status == FAILED_RETRY_LIMIT and result.failed_frame == 5
    assert len(of_type(result, "failed")) == 1


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_duplicate_acks_do_not_change_retry_counts(protocol):
    # Lost ACKs and premature timeouts produce duplicate ACKs; retransmission
    # counts in the trace must still match each frame's attempts.
    result = run_simulation(cfg(protocol, num_frames=8, timeout=4, retry_limit=5,
                                scripted_losses=[lose("ack", 2)]))
    assert result.metrics.duplicate_acks > 0
    attempts = {}
    for entry in of_type(result, "frame_sent"):
        f = entry.details["frame_index"]
        attempts[f] = attempts.get(f, 0) + 1
        assert entry.details["attempt"] == attempts[f]
    assert sum(a - 1 for a in attempts.values()) == result.metrics.retransmissions


# --- safety limit ------------------------------------------------------------

def test_safety_limit_aborts_before_the_next_event():
    full = run_simulation(cfg("gbn", num_frames=8))
    n = full.events_processed
    assert run_simulation(cfg("gbn", num_frames=8), max_events=n).status == COMPLETED

    cut = run_simulation(cfg("gbn", num_frames=8), max_events=n - 1)
    assert cut.status == ABORTED_SAFETY_LIMIT
    assert cut.events_processed == n - 1
    last = cut.trace[-1]
    assert (last.type, last.event_id, last.layer, last.actor) == ("aborted", None, "simulation", "simulator")
    assert cut.end_tick == last.time == cut.trace[-2].time
    assert of_type(cut, "completed") == [] and of_type(cut, "failed") == []


def test_safety_limit_of_one_processes_exactly_one_event():
    result = run_simulation(cfg("sr"), max_events=1)
    assert (result.status, result.events_processed, result.end_tick) == (ABORTED_SAFETY_LIMIT, 1, 0)
    assert [e.type for e in result.trace] == ["transfer_started", "frame_sent", "aborted"]


def test_stale_timeouts_count_towards_the_safety_limit():
    result = run_simulation(cfg("sr", num_frames=8))
    assert result.metrics.stale_timeouts > 0
    traced = {e.event_id for e in result.trace if e.event_id is not None}
    assert result.events_processed >= len(traced)


# --- completion --------------------------------------------------------------

def test_completion_stops_processing_in_flight_events():
    # P3 for GBN: the receiver has everything at tick 11; the sender only
    # learns at 25. The run ends there, with timers still pending.
    result = run_simulation(cfg("gbn", num_frames=8, scripted_losses=[lose("ack", 7)]))
    assert result.trace[-1].type == "completed" and result.trace[-1].time == 25
    assert max(e.time for e in result.trace) == 25


# --- trace schema --------------------------------------------------------------

def _trace_runs():
    return [
        run_simulation(cfg("gbn", num_frames=8, scripted_losses=[lose("frame", 2)])),
        run_simulation(cfg("sr", num_frames=8, scripted_losses=[lose("frame", 2), lose("ack", 4)])),
        run_simulation(cfg("sr", num_frames=4, retry_limit=0, scripted_losses=[lose("frame", 1)])),
        run_simulation(cfg("gbn"), max_events=7),
    ]


@pytest.mark.parametrize("index", range(4))
def test_trace_schema(index):
    result = _trace_runs()[index]
    keys = ["event_id", "step", "time", "type", "layer", "actor", "state", "details"]
    for i, entry in enumerate(result.trace):
        d = entry.to_dict()
        assert list(d) == keys
        assert d["step"] == i
        assert d["type"] in TRACE_TYPES
        assert d["layer"] in ("simulation", "data_link", "channel", "application")
        assert d["actor"] in ("simulator", "sender", "receiver", "channel")
        assert set(d["state"]) == {"sender", "receiver", "timers", "delivered"}
        assert (d["event_id"] is None) == (d["type"] in ("transfer_started", "aborted"))
    json.dumps(result.to_dict())     # everything is plain JSON data


@pytest.mark.parametrize("index", range(4))
def test_trace_order_and_termination(index):
    result = _trace_runs()[index]
    times = [e.time for e in result.trace]
    assert times == sorted(times)
    assert result.trace[0].type == "transfer_started" and result.trace[0].time == 0
    ends = [e for e in result.trace if e.type in ("completed", "failed", "aborted")]
    assert len(ends) == 1 and ends[0] is result.trace[-1]
    expected_end = {COMPLETED: "completed", FAILED_RETRY_LIMIT: "failed", ABORTED_SAFETY_LIMIT: "aborted"}
    assert ends[0].type == expected_end[result.status]


def test_event_ids_are_not_chronological():
    result = run_simulation(cfg("gbn", num_frames=8))
    ids = [e.event_id for e in result.trace if e.event_id is not None]
    assert ids != sorted(ids)            # e.g. an early-scheduled timeout processed late
    steps = [e.step for e in result.trace]
    assert steps == list(range(len(steps)))


def test_entries_from_one_event_share_its_id_and_tick():
    result = run_simulation(cfg("sr", num_frames=8, scripted_losses=[lose("frame", 1)]))
    by_id = {}
    for entry in result.trace:
        if entry.event_id is not None:
            by_id.setdefault(entry.event_id, set()).add(entry.time)
    assert all(len(times) == 1 for times in by_id.values())


def test_trace_shows_buffered_and_duplicate_frames_for_sr():
    result = run_simulation(cfg("sr", num_frames=8, scripted_losses=[lose("frame", 2), lose("ack", 5)]))
    outcomes = {e.details["outcome"] for e in of_type(result, "frame_received")}
    assert {"buffered", "duplicate", "delivered"} <= outcomes


def test_trace_shows_discarded_frames_for_gbn():
    result = run_simulation(cfg("gbn", num_frames=8, scripted_losses=[lose("frame", 2)]))
    assert "discarded" in {e.details["outcome"] for e in of_type(result, "frame_received")}


def test_state_snapshot_shows_timers_and_window():
    result = run_simulation(cfg("gbn", num_frames=8))
    first_send = of_type(result, "frame_sent")[0]
    assert first_send.state["timers"] == {"base": 8}
    assert first_send.state["sender"]["outstanding"] == [0]
    advanced = of_type(result, "window_advanced")[0]
    assert (advanced.details["old_base"], advanced.details["new_base"]) == (0, 1)
    assert advanced.state["timers"] == {"base": 13}     # restarted at the ACK's arrival, tick 5


# --- metrics -----------------------------------------------------------------

def test_metrics_match_trace_counts():
    result = run_simulation(SimulationConfig(protocol="sr", seed=5, frame_loss=0.3, ack_loss=0.3,
                                             num_frames=15, retry_limit=10))
    m = result.metrics
    assert m.frame_transmissions == len(of_type(result, "frame_sent"))
    assert m.frames_lost == len(of_type(result, "frame_lost"))
    assert m.acks_sent == len(of_type(result, "ack_sent"))
    assert m.acks_lost == len(of_type(result, "ack_lost"))
    assert m.acks_received == m.acks_sent - m.acks_lost - _acks_in_flight(result)
    assert m.frames_received + m.frames_lost <= m.frame_transmissions
    assert m.events_processed == result.events_processed


def _acks_in_flight(result):
    """ACKs sent but neither lost nor received before the run ended."""
    end = result.end_tick
    sent = of_type(result, "ack_sent")
    lost = {(e.details["frame_index"], e.details["occurrence"]) for e in of_type(result, "ack_lost")}
    return sum(1 for e in sent
               if (e.details["frame_index"], e.details["occurrence"]) not in lost
               and e.details["arrives_at"] > end)


def test_compute_metrics_from_a_hand_made_trace():
    def entry(type_, time=0, **details):
        return {"type": type_, "time": time, "details": details}
    trace = [
        entry("transfer_started"),
        entry("frame_sent", 0, retransmission=False), entry("frame_lost", 0),
        entry("frame_sent", 8, retransmission=True),
        entry("frame_received", 11, outcome="delivered"), entry("frame_delivered", 11),
        entry("ack_sent", 11), entry("ack_received", 13, duplicate=False),
        entry("timeout", 8, valid=True), entry("timeout", 9, valid=False),
        entry("completed", 13),
    ]
    m = compute_metrics(trace, status="completed", end_tick=13, events_processed=6)
    assert (m.frame_transmissions, m.retransmissions, m.frames_lost) == (2, 1, 1)
    assert (m.frames_delivered, m.last_delivery_tick, m.completion_tick) == (1, 11, 13)
    assert (m.timeouts, m.stale_timeouts, m.acks_sent, m.acks_received) == (1, 1, 1, 1)
    assert m.goodput == pytest.approx(1 / 13)


def test_goodput_is_undefined_unless_completed():
    m = compute_metrics([], status="failed_retry_limit", end_tick=8, events_processed=3)
    assert m.goodput is None and m.completion_tick is None


def test_metrics_have_no_utilization_field():
    fields = run_simulation(cfg()).metrics.to_dict()
    assert not any("util" in name for name in fields)


# --- determinism -------------------------------------------------------------

def _random_config():
    return SimulationConfig(protocol="sr", seed=42, frame_loss=0.25, ack_loss=0.25, num_frames=12)


def test_same_config_gives_identical_results():
    assert run_simulation(_random_config()).to_dict() == run_simulation(_random_config()).to_dict()


def test_results_are_identical_across_processes():
    code = (
        "import json; from arq import SimulationConfig, run_simulation;"
        "c = SimulationConfig(protocol='sr', seed=42, frame_loss=0.25, ack_loss=0.25, num_frames=12);"
        "print(json.dumps(run_simulation(c).to_dict(), sort_keys=False))"
    )
    backend = pathlib.Path(__file__).resolve().parents[1]
    outputs = []
    for hash_seed in ("1", "2", "random"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(backend)}
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, check=True)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1] == outputs[2]
    assert json.loads(outputs[0]) == json.loads(json.dumps(run_simulation(_random_config()).to_dict()))
