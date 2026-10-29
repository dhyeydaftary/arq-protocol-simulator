"""Named, documented simulation presets.

Every preset is an ordinary configuration (the same field names and limits
as SimulationConfig.from_dict), so it is reproducible: the same preset always
gives the same result. Presets set protocol "gbn"; any of them can be run
with protocol "sr" instead.

P0-P6 are the hand-derived reference scenarios. Their `expected` results
(per protocol) are checked by the test suite. Defaults for P0-P6:
num_frames=8, seq_bits=3, window_size=4, propagation_delay=2 (RTT 5),
timeout=8, retry_limit=10, no random loss.
"""

from __future__ import annotations

import copy
from typing import Any

from .config import SimulationConfig

_REFERENCE = dict(protocol="gbn", num_frames=8, seq_bits=3, window_size=4, propagation_delay=2,
                  timeout=8, retry_limit=10, frame_loss=0.0, ack_loss=0.0, seed=42)


def _frame(index: int, occurrence: int = 1) -> dict[str, Any]:
    return {"kind": "frame", "index": index, "occurrence": occurrence}


def _ack(index: int, occurrence: int = 1) -> dict[str, Any]:
    return {"kind": "ack", "index": index, "occurrence": occurrence}


def _expect(gbn: tuple[int, int, int], sr: tuple[int, int, int]) -> dict[str, Any]:
    """(transmissions, retransmissions, completion tick) per protocol."""
    def one(values: tuple[int, int, int]) -> dict[str, Any]:
        tx, rtx, tick = values
        return {"status": "completed", "frame_transmissions": tx, "retransmissions": rtx,
                "completion_tick": tick}
    return {"gbn": one(gbn), "sr": one(sr)}


_PRESETS: list[dict[str, Any]] = [
    {
        "id": "no_loss", "scenario": "P0", "name": "No loss",
        "description": "Eight frames with no loss. Both protocols send 8 frames and complete at "
                       "tick 13 (last frame sent at 8, plus RTT 5).",
        "config": {**_REFERENCE, "scripted_losses": []},
        "expected": _expect((8, 0, 13), (8, 0, 13)),
    },
    {
        "id": "frame_loss", "scenario": "P1", "name": "One lost frame",
        "description": "Frame 2's first transmission is lost. GBN resends frames 2-5 after the "
                       "base timeout; SR resends only frame 2.",
        "config": {**_REFERENCE, "scripted_losses": [_frame(2)]},
        "expected": _expect((12, 4, 25), (9, 1, 21)),
    },
    {
        "id": "ack_loss", "scenario": "P2", "name": "One lost ACK",
        "description": "The ACK for frame 1 is lost. GBN's next cumulative ACK covers it, so "
                       "nothing is resent; SR must resend frame 1 after its timeout.",
        "config": {**_REFERENCE, "scripted_losses": [_ack(1)]},
        "expected": _expect((8, 0, 14), (9, 1, 21)),
    },
    {
        "id": "last_ack_loss", "scenario": "P3", "name": "Lost final ACK",
        "description": "The ACK for the last frame (7) is lost. Every frame is delivered by tick "
                       "11, but the sender only learns the transfer is complete after resending "
                       "frame 7.",
        "config": {**_REFERENCE, "scripted_losses": [_ack(7)]},
        "expected": _expect((9, 1, 25), (9, 1, 21)),
    },
    {
        "id": "consecutive_frame_losses", "scenario": "P4", "name": "Three consecutive lost frames",
        "description": "First transmissions of frames 2, 3 and 4 are lost. GBN costs the same as "
                       "P1; SR resends each lost frame individually.",
        "config": {**_REFERENCE, "scripted_losses": [_frame(2), _frame(3), _frame(4)]},
        "expected": _expect((12, 4, 25), (11, 3, 21)),
    },
    {
        "id": "repeated_frame_loss", "scenario": "P5", "name": "Frame lost twice",
        "description": "Frame 2's first two transmissions are lost, so it needs two timeouts.",
        "config": {**_REFERENCE, "scripted_losses": [_frame(2, 1), _frame(2, 2)]},
        "expected": _expect((16, 8, 33), (10, 2, 29)),
    },
    {
        "id": "premature_timeout", "scenario": "P6", "name": "Premature timeout",
        "description": "timeout 4 is shorter than the RTT of 5, so every frame times out once "
                       "and is resent although nothing is lost. Duplicates are never delivered "
                       "twice.",
        "config": {**_REFERENCE, "num_frames": 4, "timeout": 4, "scripted_losses": []},
        "expected": _expect((8, 4, 8), (8, 4, 8)),
    },
    {
        "id": "retry_exhaustion", "scenario": None, "name": "Retry limit exhausted",
        "description": "retry_limit 1 and frame 1 lost on both allowed attempts: the run ends "
                       "with failed_retry_limit when frame 1's timeout finds no retries left.",
        "config": {**_REFERENCE, "num_frames": 4, "retry_limit": 1,
                   "scripted_losses": [_frame(1, 1), _frame(1, 2)]},
        "expected": {"gbn": {"status": "failed_retry_limit", "end_tick": 21, "failed_frame": 1},
                     "sr": {"status": "failed_retry_limit", "end_tick": 17, "failed_frame": 1}},
    },
    {
        "id": "random_loss", "scenario": None, "name": "Random loss (defaults)",
        "description": "The default configuration: 12 frames, 10% frame loss and 5% ACK loss "
                       "decided by SHA-256 from seed 42. Change the seed for a different, "
                       "equally reproducible run.",
        "config": {**_REFERENCE, "num_frames": 12, "frame_loss": 0.10, "ack_loss": 0.05,
                   "scripted_losses": []},
        "expected": None,
    },
]


def list_presets() -> list[dict[str, Any]]:
    """All presets, as independent copies (callers may modify them)."""
    return copy.deepcopy(_PRESETS)


def get_preset(preset_id: str) -> dict[str, Any]:
    for preset in _PRESETS:
        if preset["id"] == preset_id:
            return copy.deepcopy(preset)
    raise KeyError(f"unknown preset {preset_id!r}")


def preset_config(preset_id: str, protocol: str | None = None) -> SimulationConfig:
    """The preset's configuration, optionally for the other protocol."""
    data = get_preset(preset_id)["config"]
    if protocol is not None:
        data["protocol"] = protocol
    return SimulationConfig.from_dict(data)
