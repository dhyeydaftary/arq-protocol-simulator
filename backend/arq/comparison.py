"""GBN versus Selective Repeat comparison sweeps.

Two sweeps are supported:
* "frame_loss":  frame_loss = 0.00, 0.05, ..., 0.40 (9 points)
* "window_size": seq_bits = 4 and window_size = 1, 2, ..., 8 (8 points;
                 8 is the largest SR window for seq_bits 4)

At every point both protocols run `runs` times (default 50). Seeds are
PAIRED: run r uses seed base_seed + r for GBN and for SR, at every point, so
both protocols face the same deterministic loss decisions for the same
(kind, frame, occurrence) keys. Scripted losses are rejected: a comparison
is statistical. The rest of the configuration comes from `config` (fields
and limits exactly as SimulationConfig.from_dict), except protocol, seed and
the swept field, which the sweep controls.

Aggregates per point and protocol (failed and aborted runs are never
dropped from "all runs" figures):
  completion_rate          completed runs / runs
  mean_goodput_all         mean goodput over ALL runs, a run that did not
                           complete counting as 0
  mean_goodput_completed   mean goodput over completed runs (None if none)
  mean_retransmissions     over all runs
  mean_transmissions       over all runs (frames that began transmission)
  mean_completion_time     mean completion tick over completed runs (None if none)
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any, Iterable

from .config import SEED_MAX, SimulationConfig, _describe
from .engine import (
    ABORTED_SAFETY_LIMIT, COMPLETED, DEFAULT_MAX_EVENTS, FAILED_RETRY_LIMIT, config_to_dict, run_simulation,
)
from .errors import ComparisonError, ConfigError, FieldIssue
from .metrics import Metrics

SWEEPS = ("frame_loss", "window_size")
FRAME_LOSS_VALUES = tuple(round(i * 0.05, 2) for i in range(9))      # 0.0 .. 0.4
WINDOW_SIZE_VALUES = tuple(range(1, 9))                               # 1 .. 8
WINDOW_SWEEP_SEQ_BITS = 4
PROTOCOLS = ("gbn", "sr")

DEFAULT_RUNS = 50
MAX_RUNS = 200
DEFAULT_BASE_SEED = 42

REQUEST_KEYS = ("sweep", "runs", "base_seed", "config")
# Fields the sweep itself controls; they may not be set in `config`.
_CONTROLLED = ("protocol", "seed")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def sweep_values(sweep: str) -> tuple[Any, ...]:
    return FRAME_LOSS_VALUES if sweep == "frame_loss" else WINDOW_SIZE_VALUES


def aggregate(metrics: Iterable[Metrics]) -> dict[str, Any]:
    """Summarise the runs at one point for one protocol."""
    runs = list(metrics)
    if not runs:
        raise ValueError("aggregate needs at least one run")
    n = len(runs)
    completed = [m for m in runs if m.status == COMPLETED]

    def mean(values: list[float]) -> float | None:
        return math.fsum(values) / len(values) if values else None

    return {
        "runs": n,
        "completed": len(completed),
        "failed_retry_limit": sum(1 for m in runs if m.status == FAILED_RETRY_LIMIT),
        "aborted_safety_limit": sum(1 for m in runs if m.status == ABORTED_SAFETY_LIMIT),
        "completion_rate": len(completed) / n,
        "mean_goodput_all": mean([m.goodput if m.status == COMPLETED else 0.0 for m in runs]),
        "mean_goodput_completed": mean([m.goodput for m in completed]),
        "mean_retransmissions": mean([m.retransmissions for m in runs]),
        "mean_transmissions": mean([m.frame_transmissions for m in runs]),
        "mean_completion_time": mean([m.completion_tick for m in completed]),
    }


def _parse(request: Any) -> tuple[str, int, int, dict[str, Any]]:
    """Validate the request shape; return (sweep, runs, base_seed, config)."""
    if not isinstance(request, dict):
        raise ComparisonError([FieldIssue("request", f"must be a JSON object, got {type(request).__name__}")])
    issues: list[FieldIssue] = []
    for key in request:
        if key not in REQUEST_KEYS:
            issues.append(FieldIssue(str(key), f"is not a known comparison setting; allowed: {list(REQUEST_KEYS)}"))

    sweep = request.get("sweep")
    if sweep not in SWEEPS:
        issues.append(FieldIssue("sweep", f"must be one of {list(SWEEPS)}, got {sweep!r}"))

    runs = request.get("runs", DEFAULT_RUNS)
    if not _is_int(runs) or not 1 <= runs <= MAX_RUNS:
        issues.append(FieldIssue("runs", f"must be an integer from 1 to {MAX_RUNS}, got {runs!r}"))

    base_seed = request.get("base_seed", DEFAULT_BASE_SEED)
    if not _is_int(base_seed) or base_seed < 0:
        issues.append(FieldIssue("base_seed", f"must be an integer >= 0, got {base_seed!r}"))
    elif _is_int(runs) and base_seed + max(runs, 1) - 1 > SEED_MAX:
        issues.append(FieldIssue("base_seed", f"base_seed + runs - 1 must not exceed {SEED_MAX}"))

    config = request.get("config", {})
    if not isinstance(config, dict):
        issues.append(FieldIssue("config", f"must be a JSON object, got {type(config).__name__}"))
        config = {}
    for key in _CONTROLLED:
        if key in config:
            issues.append(FieldIssue(f"config.{key}", "is set by the comparison and may not be given"))
    if sweep in SWEEPS and sweep in config:
        issues.append(FieldIssue(f"config.{sweep}", "is the swept setting and may not be given"))
    if sweep == "window_size" and "seq_bits" in config:
        # Check the type first: 4.0 == 4 in Python, but integer fields reject floats.
        bits = config["seq_bits"]
        if not _is_int(bits):
            issues.append(FieldIssue("config.seq_bits", f"must be an integer, got {_describe(bits)}"))
        elif bits != WINDOW_SWEEP_SEQ_BITS:
            issues.append(FieldIssue("config.seq_bits", f"must be {WINDOW_SWEEP_SEQ_BITS} for the window_size sweep"))
    if config.get("scripted_losses"):
        issues.append(FieldIssue("config.scripted_losses", "are not allowed in a statistical comparison"))

    if issues:
        raise ComparisonError(issues)
    return sweep, runs, base_seed, config


def build_configs(request: Any) -> tuple[str, int, int, dict[Any, dict[str, SimulationConfig]]]:
    """Validate everything and build the config for every point and protocol
    (seed left at base_seed). Nothing runs until all of it is valid."""
    sweep, runs, base_seed, config = _parse(request)
    configs: dict[Any, dict[str, SimulationConfig]] = {}
    issues: list[FieldIssue] = []
    for value in sweep_values(sweep):
        configs[value] = {}
        for protocol in PROTOCOLS:
            data = {**config, sweep: value, "protocol": protocol, "seed": base_seed}
            if sweep == "window_size":
                data["seq_bits"] = WINDOW_SWEEP_SEQ_BITS
            try:
                configs[value][protocol] = SimulationConfig.from_dict(data)
            except ConfigError as error:
                for issue in error.issues:
                    problem = FieldIssue(f"config.{issue.field}", f"{issue.message} ({protocol}, {sweep}={value})")
                    if problem not in issues:
                        issues.append(problem)
    if issues:
        raise ComparisonError(issues)
    return sweep, runs, base_seed, configs


def run_comparison(request: Any, *, max_events: int = DEFAULT_MAX_EVENTS) -> dict[str, Any]:
    """Validate and run a sweep; return its JSON-ready summary."""
    return run_built(*build_configs(request), max_events=max_events)


def run_built(sweep: str, runs: int, base_seed: int, configs: dict[Any, dict[str, SimulationConfig]],
              *, max_events: int = DEFAULT_MAX_EVENTS) -> dict[str, Any]:
    """Run a sweep already validated by build_configs. Callers such as the
    API can apply their own checks between the two steps."""
    points = []
    for value, by_protocol in configs.items():
        point: dict[str, Any] = {"value": value}
        for protocol in PROTOCOLS:
            base = by_protocol[protocol]
            results = [
                run_simulation(_with_seed(base, base_seed + r), max_events=max_events).metrics
                for r in range(runs)
            ]
            point[protocol] = aggregate(results)
        points.append(point)

    fixed = config_to_dict(next(iter(configs.values()))["gbn"])
    for key in ("protocol", "seed", sweep):
        fixed.pop(key)
    return {
        "sweep": sweep,
        "values": list(sweep_values(sweep)),
        "runs": runs,
        "base_seed": base_seed,
        "seeds": {"first": base_seed, "last": base_seed + runs - 1, "paired": True},
        "fixed_config": fixed,
        "points": points,
    }


def _with_seed(config: SimulationConfig, seed: int) -> SimulationConfig:
    return dataclasses.replace(config, seed=seed)   # re-validated on creation
