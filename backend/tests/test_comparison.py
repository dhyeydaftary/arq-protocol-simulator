"""GBN versus SR comparison: validation, paired seeds and aggregation."""

import dataclasses

import pytest

from arq import SimulationConfig, run_simulation
from arq.comparison import (
    DEFAULT_BASE_SEED, DEFAULT_RUNS, FRAME_LOSS_VALUES, MAX_RUNS, WINDOW_SIZE_VALUES,
    aggregate, run_comparison,
)
from arq.errors import ComparisonError
from arq.metrics import compute_metrics


def metrics(status, goodput_tick=None, retransmissions=0, transmissions=0):
    """A Metrics object with the fields aggregation uses."""
    m = compute_metrics([], status=status, end_tick=goodput_tick or 10, events_processed=1)
    delivered = 4 if status == "completed" else 0
    return dataclasses.replace(
        m, retransmissions=retransmissions, frame_transmissions=transmissions,
        frames_delivered=delivered,
        goodput=(delivered / goodput_tick) if status == "completed" else None,
    )


def fields_of(error_info):
    return {issue.field for issue in error_info.value.issues}


# --- sweep definitions ----------------------------------------------------------

def test_sweep_values_and_defaults():
    assert FRAME_LOSS_VALUES == (0.0, 0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4)
    assert WINDOW_SIZE_VALUES == (1, 2, 3, 4, 5, 6, 7, 8)
    assert (DEFAULT_RUNS, DEFAULT_BASE_SEED) == (50, 42)


# --- aggregation ------------------------------------------------------------------

def test_aggregate_counts_failed_runs_as_zero_goodput_in_all_runs_means():
    runs = [
        metrics("completed", goodput_tick=10, retransmissions=1, transmissions=5),   # goodput 0.4
        metrics("completed", goodput_tick=20, retransmissions=3, transmissions=7),   # goodput 0.2
        metrics("failed_retry_limit", retransmissions=8, transmissions=12),
        metrics("aborted_safety_limit", retransmissions=4, transmissions=6),
    ]
    agg = aggregate(runs)
    assert (agg["runs"], agg["completed"], agg["failed_retry_limit"], agg["aborted_safety_limit"]) == (4, 2, 1, 1)
    assert agg["completion_rate"] == 0.5
    assert agg["mean_goodput_all"] == pytest.approx((0.4 + 0.2 + 0 + 0) / 4)
    assert agg["mean_goodput_completed"] == pytest.approx(0.3)
    assert agg["mean_retransmissions"] == pytest.approx((1 + 3 + 8 + 4) / 4)   # failed runs included
    assert agg["mean_transmissions"] == pytest.approx((5 + 7 + 12 + 6) / 4)
    assert agg["mean_completion_time"] == pytest.approx(15)                    # completed only


def test_aggregate_with_no_completed_runs():
    agg = aggregate([metrics("failed_retry_limit", retransmissions=2, transmissions=3)])
    assert agg["completion_rate"] == 0.0 and agg["mean_goodput_all"] == 0.0
    assert agg["mean_goodput_completed"] is None and agg["mean_completion_time"] is None
    assert agg["mean_retransmissions"] == 2.0


def test_aggregate_needs_runs():
    with pytest.raises(ValueError):
        aggregate([])


# --- whole comparisons -------------------------------------------------------------

def test_frame_loss_comparison_shape():
    result = run_comparison({"sweep": "frame_loss", "runs": 2})
    assert result["values"] == list(FRAME_LOSS_VALUES)
    assert [p["value"] for p in result["points"]] == list(FRAME_LOSS_VALUES)
    assert result["seeds"] == {"first": 42, "last": 43, "paired": True}
    assert "frame_loss" not in result["fixed_config"] and "seed" not in result["fixed_config"]
    for point in result["points"]:
        assert point["gbn"]["runs"] == point["sr"]["runs"] == 2


def test_window_size_comparison_uses_seq_bits_4():
    result = run_comparison({"sweep": "window_size", "runs": 1})
    assert result["values"] == list(WINDOW_SIZE_VALUES)
    assert result["fixed_config"]["seq_bits"] == 4


def test_seeds_are_paired_across_protocols_and_points():
    request = {"sweep": "frame_loss", "runs": 3, "base_seed": 100, "config": {"num_frames": 10}}
    result = run_comparison(request)
    for point in result["points"]:
        for protocol in ("gbn", "sr"):
            expected = aggregate([
                run_simulation(SimulationConfig(protocol=protocol, num_frames=10, frame_loss=point["value"],
                                                seed=100 + r)).metrics
                for r in range(3)
            ])
            assert point[protocol] == expected


def test_zero_loss_point_has_no_retransmissions_with_a_long_timeout():
    result = run_comparison({"sweep": "frame_loss", "runs": 2, "config": {"ack_loss": 0.0, "timeout": 50}})
    zero = result["points"][0]
    for protocol in ("gbn", "sr"):
        assert zero[protocol]["mean_retransmissions"] == 0
        assert zero[protocol]["completion_rate"] == 1.0


def test_failed_runs_are_kept_in_all_runs_metrics():
    request = {"sweep": "frame_loss", "runs": 6, "config": {"retry_limit": 0, "num_frames": 20}}
    result = run_comparison(request)
    high = result["points"][-1]                        # frame_loss 0.4
    for protocol in ("gbn", "sr"):
        agg = high[protocol]
        assert agg["runs"] == agg["completed"] + agg["failed_retry_limit"] + agg["aborted_safety_limit"] == 6
        assert agg["completed"] < 6
        if agg["completed"]:
            assert agg["mean_goodput_all"] == pytest.approx(agg["mean_goodput_completed"] * agg["completion_rate"])
        else:
            assert agg["mean_goodput_all"] == 0.0


def test_comparison_is_deterministic():
    request = {"sweep": "window_size", "runs": 2, "config": {"frame_loss": 0.2}}
    assert run_comparison(request) == run_comparison(request)


# --- validation ------------------------------------------------------------------

@pytest.mark.parametrize("request_, field", [
    ([], "request"),
    ({}, "sweep"),
    ({"sweep": "timeout"}, "sweep"),
    ({"sweep": "frame_loss", "runs": 0}, "runs"),
    ({"sweep": "frame_loss", "runs": MAX_RUNS + 1}, "runs"),
    ({"sweep": "frame_loss", "runs": 1.5}, "runs"),
    ({"sweep": "frame_loss", "runs": True}, "runs"),
    ({"sweep": "frame_loss", "base_seed": -1}, "base_seed"),
    ({"sweep": "frame_loss", "base_seed": 2 ** 63 - 1, "runs": 2}, "base_seed"),
    ({"sweep": "frame_loss", "extra": 1}, "extra"),
    ({"sweep": "frame_loss", "config": []}, "config"),
    ({"sweep": "frame_loss", "config": {"protocol": "sr"}}, "config.protocol"),
    ({"sweep": "frame_loss", "config": {"seed": 1}}, "config.seed"),
    ({"sweep": "frame_loss", "config": {"frame_loss": 0.1}}, "config.frame_loss"),
    ({"sweep": "window_size", "config": {"window_size": 2}}, "config.window_size"),
    ({"sweep": "window_size", "config": {"seq_bits": 3}}, "config.seq_bits"),
    ({"sweep": "frame_loss", "config": {"scripted_losses": [{"kind": "frame", "index": 0}]}},
     "config.scripted_losses"),
    ({"sweep": "frame_loss", "config": {"num_frames": 0}}, "config.num_frames"),
    ({"sweep": "frame_loss", "config": {"num_frame": 5}}, "config.num_frame"),
    ({"sweep": "frame_loss", "config": {"seq_bits": 2, "window_size": 3}}, "config.window_size"),
])
def test_invalid_requests_are_rejected(request_, field):
    with pytest.raises(ComparisonError) as info:
        run_comparison(request_)
    assert field in fields_of(info)


@pytest.mark.parametrize("bits", [4.0, True, "4", None])
def test_window_sweep_rejects_non_integer_seq_bits(bits):
    # 4.0 == 4 in Python, but integer fields reject floats (as SimulationConfig does).
    with pytest.raises(ComparisonError) as info:
        run_comparison({"sweep": "window_size", "runs": 1, "config": {"seq_bits": bits}})
    (issue,) = [i for i in info.value.issues if i.field == "config.seq_bits"]
    assert "must be an integer" in issue.message


def test_window_sweep_accepts_integer_seq_bits_4():
    explicit = run_comparison({"sweep": "window_size", "runs": 1, "config": {"seq_bits": 4}})
    implicit = run_comparison({"sweep": "window_size", "runs": 1})
    assert explicit == implicit
    assert explicit["fixed_config"]["seq_bits"] == 4


def test_empty_scripted_loss_list_is_allowed():
    run_comparison({"sweep": "frame_loss", "runs": 1, "config": {"scripted_losses": []}})


def test_all_problems_are_reported_together():
    with pytest.raises(ComparisonError) as info:
        run_comparison({"sweep": "x", "runs": 0, "base_seed": -5})
    assert {"sweep", "runs", "base_seed"} <= fields_of(info)


# --- direct use is not limited by the API -----------------------------------------

def test_direct_comparison_uses_the_engine_default_and_no_api_budget(monkeypatch):
    # 106 runs exceeds the API's budget, but direct use is not restricted.
    # run_simulation is replaced by a cheap stand-in so no real sweep runs.
    import arq.comparison as comparison
    result = run_simulation(SimulationConfig(num_frames=1, frame_loss=0.0, ack_loss=0.0))
    calls = []

    def fake(config, **kwargs):
        calls.append(kwargs)
        return result
    monkeypatch.setattr(comparison, "run_simulation", fake)
    out = run_comparison({"sweep": "frame_loss", "runs": 106})
    assert len(calls) == 9 * 2 * 106
    assert all(c == {"max_events": 1_000_000} for c in calls)
    assert out["points"][0]["gbn"]["runs"] == 106
