"""Error and warning types for the ARQ simulator.

Component 1 only needs configuration errors. Later components may add
their own exception classes here.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldIssue:
    """One problem (or concern) found in one configuration field.

    Example: FieldIssue(field="window_size", message="must be <= 7 for GBN ...")
    """

    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}"


class ConfigError(ValueError):
    """Raised when a SimulationConfig is invalid.

    All problems are collected first and reported together, so the user
    can fix every mistake in one go instead of one error at a time.

    Attributes:
        issues: list of FieldIssue, one per problem found.
    """

    def __init__(self, issues: list[FieldIssue]) -> None:
        self.issues = list(issues)
        lines = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"Invalid simulation configuration:\n{lines}")

    @property
    def fields(self) -> set[str]:
        """Names of all fields that had at least one problem."""
        return {issue.field for issue in self.issues}


class SimulationInvariantError(RuntimeError):
    """Raised when the simulator reaches a state that should be impossible.

    This is a programming error in the simulator, never an ordinary
    simulation outcome (those are "completed", "failed_retry_limit" and
    "aborted_safety_limit"). Checks raise it explicitly instead of using
    `assert`, which Python skips when run with -O.
    """


class ComparisonError(ValueError):
    """Raised when a GBN-versus-SR comparison request is invalid.

    Like ConfigError, every problem is collected and reported together.

    Attributes:
        issues: list of FieldIssue, one per problem found.
    """

    def __init__(self, issues: list[FieldIssue]) -> None:
        self.issues = list(issues)
        lines = "\n".join(f"  - {issue}" for issue in self.issues)
        super().__init__(f"Invalid comparison request:\n{lines}")
