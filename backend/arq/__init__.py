"""ARQ Protocol Simulator: simulation engine package.

Phase 1 is in progress. Implemented: configuration (Component 1), and the
event queue, channel, GBN/SR protocols, engine and metrics (Component 2).
"""

from .config import ScriptedLoss, SimulationConfig, max_window_size
from .engine import (
    ABORTED_SAFETY_LIMIT, COMPLETED, DEFAULT_MAX_EVENTS, FAILED_RETRY_LIMIT,
    SimulationResult, TraceEvent, run_simulation,
)
from .errors import ConfigError, FieldIssue, SimulationInvariantError
from .metrics import Metrics

__all__ = [
    "ABORTED_SAFETY_LIMIT",
    "COMPLETED",
    "ConfigError",
    "DEFAULT_MAX_EVENTS",
    "FAILED_RETRY_LIMIT",
    "FieldIssue",
    "Metrics",
    "ScriptedLoss",
    "SimulationConfig",
    "SimulationInvariantError",
    "SimulationResult",
    "TraceEvent",
    "max_window_size",
    "run_simulation",
]
