"""API resource limits: worst-case transmission budgets (pure arithmetic).

arq_api needs Flask, so these tests are skipped when it is not installed.
"""

import inspect

import pytest

pytest.importorskip("flask")

from arq import DEFAULT_MAX_EVENTS, SimulationConfig, run_simulation   # noqa: E402
from arq.comparison import FRAME_LOSS_VALUES, WINDOW_SIZE_VALUES        # noqa: E402
from arq_api.limits import (                                             # noqa: E402
    API_MAX_EVENTS, MAX_COMPARISON_TRANSMISSIONS, MAX_RUN_TRANSMISSIONS,
    comparison_issues, comparison_transmissions, run_transmissions, simulation_issues,
)


def config(**kwargs):
    return SimulationConfig(**kwargs)


def fields(issues):
    return [issue.field for issue in issues]


# --- constants -----------------------------------------------------------------

def test_limit_values():
    assert (MAX_RUN_TRANSMISSIONS, MAX_COMPARISON_TRANSMISSIONS, API_MAX_EVENTS) == (2_500, 250_000, 25_000)


def test_engine_default_event_limit_is_unchanged():
    assert DEFAULT_MAX_EVENTS == 1_000_000
    assert inspect.signature(run_simulation).parameters["max_events"].default == 1_000_000


def test_sweep_point_counts():
    assert (len(FRAME_LOSS_VALUES), len(WINDOW_SIZE_VALUES)) == (9, 8)


# --- single run ------------------------------------------------------------------

def test_run_transmissions_formula():
    assert run_transmissions(12, 10) == 132          # defaults
    assert run_transmissions(100, 1000) == 100_100   # largest valid config


@pytest.mark.parametrize("num_frames, retry_limit, budget, accepted", [
    (100, 24, 2_500, True),      # exactly the limit
    (41, 60, 2_501, False),      # one over
    (100, 25, 2_600, False),
    (12, 10, 132, True),         # defaults
])
def test_single_run_boundary(num_frames, retry_limit, budget, accepted):
    assert run_transmissions(num_frames, retry_limit) == budget
    issues = simulation_issues(config(num_frames=num_frames, retry_limit=retry_limit))
    assert (issues == []) is accepted
    if not accepted:
        assert fields(issues) == ["transmissions_per_run"]
        assert str(budget) in issues[0].message and "2500" in issues[0].message


# --- comparison --------------------------------------------------------------------

@pytest.mark.parametrize("sweep, runs, total, accepted", [
    ("frame_loss", 50, 118_800, True),     # default request
    ("frame_loss", 100, 237_600, True),
    ("frame_loss", 105, 249_480, True),    # largest allowed at defaults
    ("frame_loss", 106, 251_856, False),
    ("window_size", 50, 105_600, True),    # default request
    ("window_size", 100, 211_200, True),
    ("window_size", 118, 249_216, True),   # largest allowed at defaults
    ("window_size", 119, 251_328, False),
])
def test_comparison_budget_at_defaults(sweep, runs, total, accepted):
    assert comparison_transmissions(sweep, runs, 12, 10) == total
    issues = comparison_issues(sweep, runs, config())
    assert (issues == []) is accepted
    if not accepted:
        assert fields(issues) == ["total_transmissions"]


@pytest.mark.parametrize("sweep, runs, num_frames, retry_limit, total, accepted", [
    ("window_size", 125, 25, 4, 250_000, True),    # exactly the limit
    ("window_size", 125, 25, 5, 300_000, False),
    ("frame_loss", 64, 31, 6, 249_984, True),      # closest below (totals are multiples of 18)
    ("frame_loss", 19, 43, 16, 250_002, False),    # closest above
])
def test_comparison_budget_exact_boundaries(sweep, runs, num_frames, retry_limit, total, accepted):
    assert comparison_transmissions(sweep, runs, num_frames, retry_limit) == total
    issues = comparison_issues(sweep, runs, config(num_frames=num_frames, retry_limit=retry_limit))
    assert (issues == []) is accepted


def test_per_run_limit_applies_even_when_the_total_fits():
    # 1 run: total 8 * 2 * 1 * 2600 = 41,600 fits, but one run's 2,600 does not.
    assert comparison_transmissions("window_size", 1, 100, 25) == 41_600
    issues = comparison_issues("window_size", 1, config(num_frames=100, retry_limit=25))
    assert fields(issues) == ["transmissions_per_run"]


def test_both_budgets_reported_together():
    issues = comparison_issues("frame_loss", 200, config(num_frames=100, retry_limit=1000))
    assert fields(issues) == ["transmissions_per_run", "total_transmissions"]


def test_budgets_use_exact_integers():
    total = comparison_transmissions("frame_loss", 200, 100, 1000)
    assert isinstance(total, int) and total == 9 * 2 * 200 * 100 * 1001
