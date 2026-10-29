"""Simulation configuration and validation (Phase 1, Component 1).

A SimulationConfig holds every setting for one simulation run. It is:

* frozen  - it cannot be changed after it is created, so a running
            simulation can never have its settings altered mid-run;
* checked - it validates itself when it is created. If anything is wrong,
            ConfigError is raised listing *every* problem, field by field.
            So if you hold a SimulationConfig object, it is valid.

Settings that are legal but probably not what you intended (for example a
timeout shorter than the round-trip time) do NOT raise. Instead they are
reported by `config.warnings()`.

Two ways to build a config:
* SimulationConfig(protocol="sr", ...)   - from Python code
* SimulationConfig.from_dict({...})      - from outside data (e.g. JSON).
  Unknown or misspelled keys are reported as ConfigError, with a suggestion.

Provisional decisions (see README.md, "Provisional decisions"):
* Upper bounds and warning rules are implementation choices, not taken from
  the original specification (which is unavailable).
* The scripted-loss format (kind / index / occurrence) is provisional and
  will be revisited when the channel is built.
"""

from __future__ import annotations

import difflib
import math
from dataclasses import dataclass, fields
from typing import Any

from .errors import ConfigError, FieldIssue

# ---------------------------------------------------------------------------
# Allowed values and limits
# ---------------------------------------------------------------------------

PROTOCOLS = ("gbn", "sr")
LOSS_KINDS = ("frame", "ack")
SCRIPTED_LOSS_KEYS = ("kind", "index", "occurrence")
SCRIPTED_LOSS_REQUIRED_KEYS = ("kind", "index")  # occurrence defaults to 1

# Lower bounds come from the networking rules. Upper bounds are practical
# limits that keep simulations and event traces a sensible size.
SEQ_BITS_MIN, SEQ_BITS_MAX = 1, 16                    # provisional upper bound
NUM_FRAMES_MIN, NUM_FRAMES_MAX = 1, 100               # confirmed range
PROPAGATION_DELAY_MIN, PROPAGATION_DELAY_MAX = 0, 1_000   # provisional upper bound
TIMEOUT_MIN, TIMEOUT_MAX = 1, 100_000                 # provisional upper bound
RETRY_LIMIT_MIN, RETRY_LIMIT_MAX = 0, 1_000           # provisional upper bound
SEED_MIN, SEED_MAX = 0, 2**63 - 1                     # provisional range

# Loss probability at or above this level triggers a warning.
HIGH_LOSS_WARNING = 0.5


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    """True for real integers. bool is rejected even though, in Python,
    True == 1 and isinstance(True, int) is True."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    """True for int or float (but not bool)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _describe(value: Any) -> str:
    """Show a bad value in an error message, e.g. "float 2.0" or "str '4'".

    Long representations are shortened so one huge value cannot flood
    the error message.
    """
    text = repr(value)
    if len(text) > 40:
        text = text[:37] + "..."
    return f"{type(value).__name__} {text}"


def max_window_size(protocol: str, seq_bits: int) -> int:
    """Largest legal window size for a protocol.

    M = 2 ** seq_bits wire sequence numbers exist.
      GBN: window <= M - 1
      SR : window <= M / 2   (= 2 ** (seq_bits - 1))
    """
    m = 2 ** seq_bits
    return m - 1 if protocol == "gbn" else m // 2


# ---------------------------------------------------------------------------
# Scripted losses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScriptedLoss:
    """Force one specific transmission to be lost (PROVISIONAL design).

    kind:       "frame" or "ack"
    index:      zero-based ABSOLUTE frame index (0, 1, 2, ...), never the wire
                sequence number and never reduced modulo M. Absolute indices
                are simulator/channel bookkeeping only; protocol logic must use
                wire sequence numbers and its own state instead.
    occurrence: counts from 1.
                - frame loss: index = the frame; occurrence = its transmission
                  attempt. ScriptedLoss("frame", 3, 2) drops frame 3's second
                  attempt (its first retransmission).
                  Matches loss key (seed, "frame", index, attempt).
                - ACK loss: index = the absolute frame the ACK is ASSOCIATED
                  with; occurrence = the nth ACK event associated with that
                  index. Matches loss key (seed, "ack", index, ordinal).
                    SR : the individual frame being acknowledged.
                    GBN: the highest in-order frame the cumulative ACK covers.
                         A duplicate ACK triggered by an out-of-order frame is
                         associated with that highest in-order frame, not with
                         the frame that triggered it.
                  Frames that share a wire number after wrap-around (e.g.
                  frames 1 and 9 when M = 8) have separate counters.
                  See README "Scripted losses" for worked examples.

    Can also be written as a dict:
        {"kind": "frame", "index": 3, "occurrence": 2}

    Only the structure is validated here; the channel (Component 2) will
    decide exactly how these are applied.
    """

    kind: str
    index: int
    occurrence: int = 1


def _to_scripted_loss(entry: Any) -> Any:
    """Turn a well-formed dict into a ScriptedLoss; return anything else
    unchanged so that validation can report exactly what is wrong with it."""
    if isinstance(entry, dict):
        keys = set(entry)
        if keys <= set(SCRIPTED_LOSS_KEYS) and set(SCRIPTED_LOSS_REQUIRED_KEYS) <= keys:
            return ScriptedLoss(**entry)
    return entry


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class SimulationConfig:
    """All settings for one simulation run. Defaults are the confirmed ones.

    Time values (propagation_delay, timeout) are in integer simulation ticks.
    Fields must be passed by name, e.g. SimulationConfig(protocol="sr").
    """

    protocol: str = "gbn"            # "gbn" (Go-Back-N) or "sr" (Selective Repeat)
    seq_bits: int = 3                # sequence numbers are 0 .. 2**seq_bits - 1
    window_size: int = 4             # max frames sent but not yet acknowledged
    num_frames: int = 12             # frames to deliver (1..100)
    propagation_delay: int = 2       # P: propagation delay in ticks
    timeout: int = 8                 # retransmission timeout in ticks
    frame_loss: float = 0.10         # probability any one frame transmission is lost
    ack_loss: float = 0.05           # probability any one ACK is lost
    retry_limit: int = 10            # max retransmissions per frame (0 = none); README D4
    seed: int = 42                   # makes random loss reproducible
    scripted_losses: tuple[ScriptedLoss, ...] = ()

    # -- building from outside data -------------------------------------------

    @classmethod
    def from_dict(cls, data: Any) -> SimulationConfig:
        """Build a config from a dict such as parsed JSON.

        Unlike calling SimulationConfig(...) directly, unknown or misspelled
        keys are reported as ConfigError (with a "did you mean" hint) together
        with any other problems, instead of Python's generic TypeError.
        Missing keys take their default values.
        """
        if not isinstance(data, dict):
            raise ConfigError([FieldIssue("config", f"must be a dict, got {_describe(data)}")])

        known = [f.name for f in fields(cls)]
        issues: list[FieldIssue] = []
        for key in data:
            if key not in known:
                hint = difflib.get_close_matches(str(key), known, n=1)
                suggestion = f"; did you mean {hint[0]!r}?" if hint else ""
                issues.append(FieldIssue(str(key), f"is not a known setting{suggestion}"))

        try:
            config = cls(**{k: v for k, v in data.items() if k in known})
        except ConfigError as error:
            issues.extend(error.issues)
        if issues:
            raise ConfigError(issues)
        return config

    # -- creation-time validation -------------------------------------------

    def __post_init__(self) -> None:
        # Accept a list and dict entries for convenience, but store an
        # immutable tuple of ScriptedLoss. (object.__setattr__ is the standard
        # way to set a field inside a frozen dataclass's own __post_init__.)
        if isinstance(self.scripted_losses, (list, tuple)):
            converted = tuple(_to_scripted_loss(e) for e in self.scripted_losses)
            object.__setattr__(self, "scripted_losses", converted)

        issues = self._find_errors()
        if issues:
            raise ConfigError(issues)

    # -- derived values -----------------------------------------------------

    @property
    def seq_space(self) -> int:
        """M = 2 ** seq_bits, the number of distinct wire sequence numbers."""
        return 2 ** self.seq_bits

    @property
    def max_window(self) -> int:
        """Largest window allowed for this protocol and seq_bits."""
        return max_window_size(self.protocol, self.seq_bits)

    @property
    def rtt(self) -> int:
        """Round-trip time in ticks: 1 tick to transmit + P there + P back."""
        return 1 + 2 * self.propagation_delay

    # -- errors (invalid configurations) ------------------------------------

    def _find_errors(self) -> list[FieldIssue]:
        issues: list[FieldIssue] = []

        def bad(field: str, message: str) -> None:
            issues.append(FieldIssue(field, message))

        # protocol
        if not isinstance(self.protocol, str):
            bad("protocol", f"must be a string, got {_describe(self.protocol)}")
        elif self.protocol not in PROTOCOLS:
            bad("protocol", f"must be one of {PROTOCOLS}, got {self.protocol!r}")

        # whole-number fields with a [min, max] range
        int_ranges = {
            "seq_bits": (SEQ_BITS_MIN, SEQ_BITS_MAX),
            "num_frames": (NUM_FRAMES_MIN, NUM_FRAMES_MAX),
            "propagation_delay": (PROPAGATION_DELAY_MIN, PROPAGATION_DELAY_MAX),
            "timeout": (TIMEOUT_MIN, TIMEOUT_MAX),
            "retry_limit": (RETRY_LIMIT_MIN, RETRY_LIMIT_MAX),
            "seed": (SEED_MIN, SEED_MAX),
        }
        for field, (low, high) in int_ranges.items():
            value = getattr(self, field)
            if not _is_int(value):
                bad(field, f"must be an integer, got {_describe(value)}")
            elif not low <= value <= high:
                bad(field, f"must be between {low} and {high}, got {value}")

        # window_size: type and lower bound first ...
        window_ok = False
        if not _is_int(self.window_size):
            bad("window_size", f"must be an integer, got {_describe(self.window_size)}")
        elif self.window_size < 1:
            bad("window_size", f"must be at least 1, got {self.window_size}")
        else:
            window_ok = True

        # ... then the protocol-specific upper bound, which can only be
        # checked when protocol and seq_bits are themselves valid.
        protocol_ok = self.protocol in PROTOCOLS if isinstance(self.protocol, str) else False
        seq_bits_ok = _is_int(self.seq_bits) and SEQ_BITS_MIN <= self.seq_bits <= SEQ_BITS_MAX
        if window_ok and protocol_ok and seq_bits_ok:
            m = 2 ** self.seq_bits
            limit = max_window_size(self.protocol, self.seq_bits)
            if self.window_size > limit:
                rule = "M - 1" if self.protocol == "gbn" else "M / 2"
                bad(
                    "window_size",
                    f"must be <= {limit} for {self.protocol.upper()} with "
                    f"seq_bits={self.seq_bits} (M={m}, limit is {rule}), "
                    f"got {self.window_size}",
                )

        # probabilities
        for field in ("frame_loss", "ack_loss"):
            value = getattr(self, field)
            if not _is_number(value):
                bad(field, f"must be a number, got {_describe(value)}")
            elif not math.isfinite(value) or not 0.0 <= value <= 1.0:
                bad(field, f"must be between 0.0 and 1.0, got {value}")

        # scripted losses
        if not isinstance(self.scripted_losses, tuple):
            bad(
                "scripted_losses",
                f"must be a list or tuple of scripted losses, got "
                f"{_describe(self.scripted_losses)}",
            )
        else:
            for i, loss in enumerate(self.scripted_losses):
                issues.extend(_scripted_loss_errors(f"scripted_losses[{i}]", loss))

        return issues

    # -- warnings (valid, but probably not what you meant) -------------------

    def warnings(self) -> list[FieldIssue]:
        """Return concerns about a VALID configuration. Never raises.

        An empty list means nothing looks suspicious.
        """
        notes: list[FieldIssue] = []

        def warn(field: str, message: str) -> None:
            notes.append(FieldIssue(field, message))

        if self.timeout < self.rtt:
            warn(
                "timeout",
                f"timeout {self.timeout} is shorter than the round-trip time "
                f"{self.rtt} (1 + 2*{self.propagation_delay}); timers may expire "
                f"before ACKs can return, causing unnecessary retransmissions",
            )

        for field in ("frame_loss", "ack_loss"):
            value = getattr(self, field)
            if value == 1.0:
                warn(field, "is 1.0: every transmission will be lost, so the transfer cannot complete")
            elif value >= HIGH_LOSS_WARNING:
                warn(field, f"is very high ({value}); the run may hit the retry limit")

        if self.retry_limit == 0:
            warn("retry_limit", "is 0: no retransmissions allowed, so any single loss may fail the run")

        seen: set[ScriptedLoss] = set()
        for i, loss in enumerate(self.scripted_losses):
            where = f"scripted_losses[{i}]"
            if loss.index >= self.num_frames:
                warn(
                    where,
                    f"index {loss.index} is outside frames 0..{self.num_frames - 1}; "
                    f"it will never take effect",
                )
            if loss in seen:
                warn(where, "duplicates an earlier entry")
            seen.add(loss)

        return notes


def _scripted_loss_errors(where: str, loss: Any) -> list[FieldIssue]:
    """Structural checks for one scripted-loss entry. No loss behaviour."""
    if isinstance(loss, dict):
        # Only malformed dicts reach here; well-formed ones became ScriptedLoss.
        issues = []
        unknown = sorted(str(k) for k in loss if k not in SCRIPTED_LOSS_KEYS)
        missing = [k for k in SCRIPTED_LOSS_REQUIRED_KEYS if k not in loss]
        if unknown:
            issues.append(FieldIssue(where, f"has unknown keys {unknown}; allowed keys are {list(SCRIPTED_LOSS_KEYS)}"))
        if missing:
            issues.append(FieldIssue(where, f"is missing required keys {missing}"))
        return issues

    if not isinstance(loss, ScriptedLoss):
        return [FieldIssue(where, f"must be a ScriptedLoss or a dict, got {_describe(loss)}")]

    issues = []
    if not isinstance(loss.kind, str) or loss.kind not in LOSS_KINDS:
        issues.append(FieldIssue(f"{where}.kind", f"must be one of {LOSS_KINDS}, got {loss.kind!r}"))
    if not _is_int(loss.index):
        issues.append(FieldIssue(f"{where}.index", f"must be an integer, got {_describe(loss.index)}"))
    elif loss.index < 0:
        issues.append(FieldIssue(f"{where}.index", f"must be >= 0, got {loss.index}"))
    if not _is_int(loss.occurrence):
        issues.append(FieldIssue(f"{where}.occurrence", f"must be an integer, got {_describe(loss.occurrence)}"))
    elif loss.occurrence < 1:
        issues.append(FieldIssue(f"{where}.occurrence", f"must be >= 1, got {loss.occurrence}"))
    return issues
