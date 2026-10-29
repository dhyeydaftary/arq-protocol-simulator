"""Resource limits for the HTTP API.

These apply only to requests made through the API; the engine and
arq.comparison keep their own behaviour when used directly.

The budgets count WORST-CASE transmissions, which can be computed before
any simulation runs: every frame is sent at most 1 + retry_limit times, so
one run sends at most num_frames * (1 + retry_limit) frames. Time and
response size grow with transmissions, not with ticks, so this bounds both.
All arithmetic is in integers.
"""

from __future__ import annotations

from arq import SimulationConfig
from arq.comparison import PROTOCOLS, sweep_values
from arq.errors import FieldIssue

MAX_RUN_TRANSMISSIONS = 2_500           # per simulation
MAX_COMPARISON_TRANSMISSIONS = 250_000  # per comparison request, all runs together
API_MAX_EVENTS = 25_000                 # event backstop for every API simulation


class LimitError(Exception):
    """A valid request that exceeds the API's resource limits."""

    def __init__(self, issues: list[FieldIssue]) -> None:
        self.issues = list(issues)
        super().__init__("; ".join(str(issue) for issue in self.issues))


def run_transmissions(num_frames: int, retry_limit: int) -> int:
    """Worst-case transmissions of one run."""
    return num_frames * (1 + retry_limit)


def comparison_transmissions(sweep: str, runs: int, num_frames: int, retry_limit: int) -> int:
    """Worst-case transmissions of a whole comparison: every point, both
    protocols, every run."""
    points = len(sweep_values(sweep))
    return points * len(PROTOCOLS) * runs * run_transmissions(num_frames, retry_limit)


def _run_issue(config: SimulationConfig) -> list[FieldIssue]:
    value = run_transmissions(config.num_frames, config.retry_limit)
    if value <= MAX_RUN_TRANSMISSIONS:
        return []
    return [FieldIssue(
        "transmissions_per_run",
        f"num_frames * (1 + retry_limit) = {config.num_frames} * (1 + {config.retry_limit}) = {value} "
        f"exceeds the limit of {MAX_RUN_TRANSMISSIONS}; lower num_frames or retry_limit",
    )]


def simulation_issues(config: SimulationConfig) -> list[FieldIssue]:
    """Problems with a single simulation request (empty if within limits)."""
    return _run_issue(config)


def comparison_issues(sweep: str, runs: int, config: SimulationConfig) -> list[FieldIssue]:
    """Problems with a validated comparison request. `config` is any of its
    configurations: num_frames and retry_limit are the same at every point."""
    issues = _run_issue(config)
    total = comparison_transmissions(sweep, runs, config.num_frames, config.retry_limit)
    if total > MAX_COMPARISON_TRANSMISSIONS:
        points = len(sweep_values(sweep))
        per_run = run_transmissions(config.num_frames, config.retry_limit)
        issues.append(FieldIssue(
            "total_transmissions",
            f"{points} points * {len(PROTOCOLS)} protocols * {runs} runs * {per_run} = {total} "
            f"exceeds the limit of {MAX_COMPARISON_TRANSMISSIONS}; lower runs, num_frames or retry_limit",
        ))
    return issues
