"""Trace stabilisation: compact immutable snapshots, serialisation, ordering."""

import dataclasses
import json

import pytest

from arq import SimulationConfig, run_simulation
from arq.trace import ACTORS, LAYERS, TRACE_TYPES, StateSnapshot


def cfg(protocol="gbn", **kwargs):
    kwargs.setdefault("frame_loss", 0.0)
    kwargs.setdefault("ack_loss", 0.0)
    return SimulationConfig(protocol=protocol, **kwargs)


def lose(kind, index, occurrence=1):
    return {"kind": kind, "index": index, "occurrence": occurrence}


RUNS = [
    cfg("gbn", num_frames=8, scripted_losses=[lose("frame", 2)]),
    cfg("sr", num_frames=8, scripted_losses=[lose("frame", 2), lose("ack", 5)]),
    SimulationConfig(protocol="sr", seed=9, frame_loss=0.3, ack_loss=0.3, num_frames=15),
    SimulationConfig(protocol="gbn", seed=9, frame_loss=0.3, ack_loss=0.3, num_frames=15),
]


# --- immutability -------------------------------------------------------------

def test_changing_a_returned_state_does_not_change_the_trace():
    entry = run_simulation(RUNS[0]).trace[3]
    before = entry.to_dict()
    state = entry.state
    state["sender"]["base"] = 999
    state["timers"]["base"] = -1
    state.clear()
    assert entry.to_dict() == before


def test_details_cannot_be_changed():
    result = run_simulation(RUNS[0])
    entry = next(e for e in result.trace if e.type == "ack_received")
    with pytest.raises(TypeError):
        entry.details["seq"] = 7
    assert isinstance(entry.details["acked"], tuple)
    exported = entry.to_dict()["details"]
    exported["acked"].append(99)                  # the export is a copy
    assert 99 not in entry.details["acked"]


def test_trace_entries_and_snapshots_are_frozen():
    entry = run_simulation(RUNS[0]).trace[1]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.time = 5
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.snapshot.base = 5
    assert isinstance(entry.snapshot.retries, tuple)


def test_early_snapshots_keep_the_state_of_their_moment():
    # GBN P0: when frame 0 is sent the timer deadline is 8 and only frame 0
    # is outstanding; the run continues long after, without changing that.
    result = run_simulation(cfg("gbn", num_frames=8))
    first_send = next(e for e in result.trace if e.type == "frame_sent")
    assert first_send.state["timers"] == {"base": 8}
    assert first_send.state["sender"]["outstanding"] == [0]
    assert result.trace[-1].state["sender"]["base"] == 8


def test_consecutive_identical_states_share_one_snapshot():
    result = run_simulation(RUNS[2])
    shared = sum(1 for a, b in zip(result.trace, result.trace[1:])
                 if a.snapshot == b.snapshot and a.snapshot is b.snapshot)
    unequal_but_same = sum(1 for a, b in zip(result.trace, result.trace[1:])
                           if a.snapshot != b.snapshot and a.snapshot is b.snapshot)
    assert shared > 0 and unequal_but_same == 0
    assert len({id(e.snapshot) for e in result.trace}) < len(result.trace)


def test_equal_snapshots_are_never_mistaken_for_different_moments():
    # Sharing only happens between neighbours with equal values.
    for config in RUNS:
        trace = run_simulation(config).trace
        for a, b in zip(trace, trace[1:]):
            if a.snapshot is b.snapshot:
                assert a.state == b.state


# --- shape --------------------------------------------------------------------

@pytest.mark.parametrize("index", range(len(RUNS)))
def test_serialised_trace_keeps_the_approved_schema(index):
    result = run_simulation(RUNS[index])
    data = json.loads(json.dumps(result.to_dict()))
    for i, entry in enumerate(data["trace"]):
        assert list(entry) == ["event_id", "step", "time", "type", "layer", "actor", "state", "details"]
        assert entry["step"] == i
        assert entry["type"] in TRACE_TYPES
        assert entry["layer"] in LAYERS and entry["actor"] in ACTORS
        assert list(entry["state"]) == ["sender", "receiver", "timers", "delivered"]
        assert list(entry["state"]["sender"])[:7] == [
            "base", "next", "base_seq", "next_seq", "outstanding", "retransmit_queue", "retries"]


@pytest.mark.parametrize("index", range(len(RUNS)))
def test_trace_is_ordered_by_step_and_time(index):
    trace = run_simulation(RUNS[index]).trace
    assert [e.step for e in trace] == list(range(len(trace)))
    assert [e.time for e in trace] == sorted(e.time for e in trace)


def test_sr_state_has_acked_and_buffered_and_gbn_does_not():
    sr = run_simulation(RUNS[1]).trace[-1].state
    gbn = run_simulation(RUNS[0]).trace[-1].state
    assert "acked" in sr["sender"] and "buffered" in sr["receiver"]
    assert "acked" not in gbn["sender"] and "buffered" not in gbn["receiver"]


def test_snapshot_to_dict_rebuilds_derived_fields():
    snap = StateSnapshot(seq_space=8, base=6, next=10, retries=(2, 1, 1, 0), retransmit_queue=(7,),
                         acked=(8,), expected=6, buffered=(), timers=(("6", 30),), delivered=6)
    d = snap.to_dict()
    assert d["sender"]["base_seq"] == 6 and d["sender"]["next_seq"] == 2
    assert d["sender"]["outstanding"] == [6, 7, 8, 9]
    assert d["sender"]["retries"] == {"6": 2, "7": 1, "8": 1, "9": 0}
    assert d["timers"] == {"6": 30}
