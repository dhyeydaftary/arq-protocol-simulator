"""Tests for Phase 1, Component 1: configuration and validation."""

import dataclasses
import os
import pathlib
import re
import subprocess
import sys

import pytest

from arq import ConfigError, ScriptedLoss, SimulationConfig, max_window_size


def fields_with_errors(**kwargs):
    """Build a config that is expected to fail; return the failing field names."""
    with pytest.raises(ConfigError) as info:
        SimulationConfig(**kwargs)
    return info.value.fields


def error_text(**kwargs):
    """Build a config that is expected to fail; return the full error message."""
    with pytest.raises(ConfigError) as info:
        SimulationConfig(**kwargs)
    return str(info.value)


def warned_fields(config):
    return {w.field for w in config.warnings()}


# --- defaults ---------------------------------------------------------------

def test_defaults_match_confirmed_values():
    c = SimulationConfig()
    assert c.protocol == "gbn"
    assert c.seq_bits == 3
    assert c.window_size == 4
    assert c.num_frames == 12
    assert c.propagation_delay == 2
    assert c.timeout == 8
    assert c.frame_loss == 0.10
    assert c.ack_loss == 0.05
    assert c.retry_limit == 10
    assert c.seed == 42
    assert c.scripted_losses == ()


def test_field_names_are_the_canonical_ones():
    names = [f.name for f in dataclasses.fields(SimulationConfig)]
    assert names == [
        "protocol", "seq_bits", "window_size", "num_frames",
        "propagation_delay", "timeout", "frame_loss", "ack_loss",
        "retry_limit", "seed", "scripted_losses",
    ]


def test_defaults_produce_no_warnings():
    assert SimulationConfig().warnings() == []


def test_defaults_are_valid_for_sr_too():
    # seq_bits=3 -> M=8 -> SR limit is 4, and the default window is 4.
    c = SimulationConfig(protocol="sr")
    assert c.window_size == 4
    assert c.warnings() == []


def test_derived_values():
    c = SimulationConfig()
    assert c.seq_space == 8
    assert c.max_window == 7
    assert c.rtt == 5  # 1 + 2 * 2


# --- immutability, equality, hashing -------------------------------------------

def test_config_is_frozen():
    c = SimulationConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.window_size = 5


def test_scripted_loss_is_frozen():
    loss = ScriptedLoss("frame", 0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        loss.index = 3


def test_fields_must_be_passed_by_name():
    with pytest.raises(TypeError):
        SimulationConfig("gbn")  # positional argument not allowed


def test_list_of_scripted_losses_is_stored_as_tuple():
    c = SimulationConfig(scripted_losses=[ScriptedLoss("frame", 1)])
    assert c.scripted_losses == (ScriptedLoss("frame", 1, 1),)


def test_equivalent_configs_are_equal_and_hash_equal():
    a = SimulationConfig(protocol="sr", scripted_losses=[ScriptedLoss("ack", 2)])
    b = SimulationConfig(protocol="sr", scripted_losses=({"kind": "ack", "index": 2},))
    assert a == b
    assert hash(a) == hash(b)
    assert len({a, b}) == 1          # usable as set members / dict keys


def test_different_configs_are_not_equal():
    assert SimulationConfig() != SimulationConfig(seed=43)


# --- unknown / misspelled field names ------------------------------------------

def test_direct_constructor_rejects_unknown_name_with_type_error():
    # Calling the class directly is Python code, so Python's own TypeError
    # is raised. Use from_dict for outside data.
    with pytest.raises(TypeError):
        SimulationConfig(prop_delay=2)


def test_from_dict_reports_misspelled_field_with_suggestion():
    with pytest.raises(ConfigError) as info:
        SimulationConfig.from_dict({"propagation_dely": 3})
    assert info.value.fields == {"propagation_dely"}
    assert "did you mean 'propagation_delay'" in str(info.value)


def test_from_dict_reports_old_field_names():
    with pytest.raises(ConfigError) as info:
        SimulationConfig.from_dict({"frame_loss_prob": 0.2, "prop_delay": 1})
    assert info.value.fields == {"frame_loss_prob", "prop_delay"}


def test_from_dict_reports_unknown_and_invalid_fields_together():
    with pytest.raises(ConfigError) as info:
        SimulationConfig.from_dict({"colour": "red", "timeout": 0})
    assert info.value.fields == {"colour", "timeout"}
    assert "did you mean" not in str(info.value)  # nothing similar to suggest


def test_from_dict_valid_input_and_defaults():
    c = SimulationConfig.from_dict({"protocol": "sr", "frame_loss": 0.2})
    assert c == SimulationConfig(protocol="sr", frame_loss=0.2)
    assert SimulationConfig.from_dict({}) == SimulationConfig()


def test_from_dict_requires_a_dict():
    with pytest.raises(ConfigError) as info:
        SimulationConfig.from_dict(["protocol", "gbn"])
    assert info.value.fields == {"config"}


# --- protocol ---------------------------------------------------------------

@pytest.mark.parametrize("value", ["GBN", "go-back-n", "", "stop-and-wait"])
def test_unknown_protocol_rejected(value):
    assert fields_with_errors(protocol=value) == {"protocol"}


def test_protocol_must_be_string():
    assert fields_with_errors(protocol=1) == {"protocol"}


# --- integer fields: wrong types ---------------------------------------------

INT_FIELDS = ["seq_bits", "window_size", "num_frames", "propagation_delay",
              "timeout", "retry_limit", "seed"]


@pytest.mark.parametrize("field", INT_FIELDS)
@pytest.mark.parametrize("value", [2.0, "4", None, True])
def test_integer_fields_reject_non_integers(field, value):
    assert fields_with_errors(**{field: value}) == {field}


@pytest.mark.parametrize("value, shown", [
    (2.0, "got float 2.0"),
    ("4", "got str '4'"),
    (True, "got bool True"),
    (None, "got NoneType None"),
])
def test_type_errors_show_the_invalid_value(value, shown):
    assert shown in error_text(timeout=value)


def test_long_invalid_values_are_shortened():
    message = error_text(seed="x" * 500)
    assert "..." in message
    assert "x" * 100 not in message


# --- integer fields: ranges ---------------------------------------------------

@pytest.mark.parametrize("field, value", [
    ("seq_bits", 0), ("seq_bits", 17),
    ("window_size", 0), ("window_size", -1),
    ("num_frames", 0), ("num_frames", 101),
    ("propagation_delay", -1), ("propagation_delay", 1_001),
    ("timeout", 0), ("timeout", -5),
    ("retry_limit", -1), ("retry_limit", 1_001),
    ("seed", -1), ("seed", 2**63),
])
def test_integer_fields_out_of_range(field, value):
    assert fields_with_errors(**{field: value}) == {field}


@pytest.mark.parametrize("field, value", [
    ("num_frames", 1), ("num_frames", 100),
    ("propagation_delay", 0), ("propagation_delay", 1_000),
    ("timeout", 1), ("timeout", 100_000),
    ("retry_limit", 0), ("retry_limit", 1_000),
    ("seed", 0), ("seed", 2**63 - 1),
])
def test_integer_field_boundaries_are_valid(field, value):
    SimulationConfig(**{field: value})  # must not raise
# (seq_bits boundaries are tested with matching windows below, because the
#  default window of 4 is too large when seq_bits = 1.)


def test_num_frames_100_accepted_and_101_rejected():
    assert SimulationConfig(num_frames=100).num_frames == 100
    message = error_text(num_frames=101)
    assert "num_frames: must be between 1 and 100, got 101" in message


def test_seq_bits_upper_boundary_is_valid():
    SimulationConfig(seq_bits=16)


# --- window-size rules ---------------------------------------------------------

@pytest.mark.parametrize("seq_bits, gbn_max, sr_max", [
    (1, 1, 1), (2, 3, 2), (3, 7, 4), (4, 15, 8), (16, 65_535, 32_768),
])
def test_max_window_size_table(seq_bits, gbn_max, sr_max):
    assert max_window_size("gbn", seq_bits) == gbn_max
    assert max_window_size("sr", seq_bits) == sr_max
    assert sr_max == 2 ** (seq_bits - 1)


def test_gbn_window_at_limit_is_valid():
    SimulationConfig(protocol="gbn", seq_bits=3, window_size=7)


def test_gbn_window_equal_to_m_is_rejected():
    # W = M = 8 would let a full window of new frames look like old ones.
    assert fields_with_errors(protocol="gbn", seq_bits=3, window_size=8) == {"window_size"}


def test_sr_window_at_limit_is_valid():
    SimulationConfig(protocol="sr", seq_bits=3, window_size=4)


def test_sr_window_above_half_m_is_rejected():
    # The same window (5) is fine for GBN but not for SR.
    SimulationConfig(protocol="gbn", seq_bits=3, window_size=5)
    assert fields_with_errors(protocol="sr", seq_bits=3, window_size=5) == {"window_size"}


def test_window_error_message_explains_the_rule():
    message = error_text(protocol="sr", seq_bits=3, window_size=5)
    assert "window_size" in message
    assert "<= 4" in message
    assert "M=8" in message
    assert "M / 2" in message


@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_one_bit_sequence_numbers_force_window_of_one(protocol):
    # With 1 bit, M=2: GBN limit M-1 = 1 and SR limit M/2 = 1.
    SimulationConfig(protocol=protocol, seq_bits=1, window_size=1)
    assert fields_with_errors(protocol=protocol, seq_bits=1, window_size=2) == {"window_size"}


@pytest.mark.parametrize("protocol, limit", [("gbn", 65_535), ("sr", 32_768)])
def test_window_limit_at_largest_seq_bits(protocol, limit):
    SimulationConfig(protocol=protocol, seq_bits=16, window_size=limit)
    assert fields_with_errors(protocol=protocol, seq_bits=16, window_size=limit + 1) == {"window_size"}


def test_window_upper_limit_skipped_when_protocol_invalid():
    # Only the protocol is reported; we cannot judge the window without it.
    assert fields_with_errors(protocol="xyz", window_size=100) == {"protocol"}


# --- probabilities -------------------------------------------------------------

@pytest.mark.parametrize("field", ["frame_loss", "ack_loss"])
@pytest.mark.parametrize("value", [-0.01, 1.01, -1, 2, float("nan"), float("inf"), float("-inf")])
def test_probability_out_of_range(field, value):
    assert fields_with_errors(**{field: value}) == {field}


@pytest.mark.parametrize("field", ["frame_loss", "ack_loss"])
@pytest.mark.parametrize("value", ["0.1", None, True])
def test_probability_wrong_type(field, value):
    assert fields_with_errors(**{field: value}) == {field}


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_probability_boundaries_are_valid(value):
    SimulationConfig(frame_loss=value, ack_loss=value)


# --- collecting multiple errors ----------------------------------------------

def test_all_errors_reported_together():
    with pytest.raises(ConfigError) as info:
        SimulationConfig(protocol="tcp", num_frames=0, timeout=0, frame_loss=2.0)
    assert info.value.fields == {"protocol", "num_frames", "timeout", "frame_loss"}
    assert len(info.value.issues) == 4


def test_config_error_is_a_value_error():
    with pytest.raises(ValueError):
        SimulationConfig(timeout=0)


# --- scripted losses -------------------------------------------------------------

def test_valid_scripted_losses():
    c = SimulationConfig(scripted_losses=(
        ScriptedLoss("frame", 0),
        ScriptedLoss("ack", 3, occurrence=2),
    ))
    assert len(c.scripted_losses) == 2
    assert c.scripted_losses[0].occurrence == 1  # default


def test_scripted_losses_accept_dicts():
    c = SimulationConfig(scripted_losses=[
        {"kind": "frame", "index": 3, "occurrence": 2},
        {"kind": "ack", "index": 0},                     # occurrence defaults to 1
    ])
    assert c.scripted_losses == (ScriptedLoss("frame", 3, 2), ScriptedLoss("ack", 0, 1))


@pytest.mark.parametrize("loss, bad_field", [
    (ScriptedLoss("packet", 0), "scripted_losses[0].kind"),
    (ScriptedLoss("FRAME", 0), "scripted_losses[0].kind"),
    (ScriptedLoss(None, 0), "scripted_losses[0].kind"),
    (ScriptedLoss(1, 0), "scripted_losses[0].kind"),
    (ScriptedLoss("frame", -1), "scripted_losses[0].index"),
    (ScriptedLoss("frame", 1.5), "scripted_losses[0].index"),
    (ScriptedLoss("frame", True), "scripted_losses[0].index"),
    (ScriptedLoss("ack", 0, occurrence=0), "scripted_losses[0].occurrence"),
    (ScriptedLoss("ack", 0, occurrence="1"), "scripted_losses[0].occurrence"),
    ({"kind": "ack", "index": 0, "occurrence": False}, "scripted_losses[0].occurrence"),
])
def test_invalid_scripted_loss_fields(loss, bad_field):
    assert fields_with_errors(scripted_losses=(loss,)) == {bad_field}


@pytest.mark.parametrize("entry", [
    {"kind": "frame"},                                  # missing index
    {"index": 0},                                       # missing kind
    {"kind": "frame", "index": 0, "attempt": 2},        # unknown key
    {},                                                 # empty
    "frame:0",                                          # not a dict at all
    ("frame", 0),                                       # tuple, not a ScriptedLoss
    None,
])
def test_malformed_scripted_loss_entries(entry):
    assert fields_with_errors(scripted_losses=[entry]) == {"scripted_losses[0]"}


def test_malformed_dict_messages_name_the_keys():
    message = error_text(scripted_losses=[{"kind": "frame", "attempt": 2}])
    assert "unknown keys ['attempt']" in message
    assert "missing required keys ['index']" in message


def test_multiple_issues_in_one_entry_all_reported():
    with pytest.raises(ConfigError) as info:
        SimulationConfig(scripted_losses=[ScriptedLoss("x", -1, 0)])
    assert info.value.fields == {
        "scripted_losses[0].kind",
        "scripted_losses[0].index",
        "scripted_losses[0].occurrence",
    }


def test_invalid_entry_at_nonzero_position_is_identified():
    losses = [ScriptedLoss("frame", 0), ScriptedLoss("ack", 1), {"kind": "ack", "index": -4}]
    assert fields_with_errors(scripted_losses=losses) == {"scripted_losses[2].index"}


@pytest.mark.parametrize("value", ["frame", None, ScriptedLoss("frame", 0), (x for x in [])])
def test_scripted_losses_must_be_a_list_or_tuple(value):
    assert fields_with_errors(scripted_losses=value) == {"scripted_losses"}


# --- warnings ------------------------------------------------------------------------

def test_timeout_shorter_than_rtt_warns_but_is_valid():
    c = SimulationConfig(propagation_delay=2, timeout=4)  # RTT = 5
    assert "timeout" in warned_fields(c)


def test_timeout_equal_to_rtt_does_not_warn():
    # Agreed ordering rule: an arrival at the same tick as a timeout is
    # processed first, so timeout == RTT is not reported as premature.
    # (The exact timer-start event is still to be confirmed with the engine.)
    assert "timeout" not in warned_fields(SimulationConfig(propagation_delay=2, timeout=5))


@pytest.mark.parametrize("field", ["frame_loss", "ack_loss"])
def test_high_and_total_loss_warn(field):
    assert field in warned_fields(SimulationConfig(**{field: 0.5}))
    assert field in warned_fields(SimulationConfig(**{field: 1.0}))
    assert field not in warned_fields(SimulationConfig(**{field: 0.49}))


def test_zero_retry_limit_warns():
    assert "retry_limit" in warned_fields(SimulationConfig(retry_limit=0))


def test_scripted_loss_beyond_last_frame_warns():
    c = SimulationConfig(num_frames=12, scripted_losses=(ScriptedLoss("frame", 12),))
    assert warned_fields(c) == {"scripted_losses[0]"}


def test_duplicate_scripted_loss_warns():
    loss = ScriptedLoss("frame", 2)
    c = SimulationConfig(scripted_losses=(loss, {"kind": "frame", "index": 2}))
    assert warned_fields(c) == {"scripted_losses[1]"}


# --- ACK-loss scripting semantics (configuration level only) -------------------
# No channel or protocol engine exists yet, so these tests check what the
# configuration guarantees: entries keep the ABSOLUTE frame index (never the
# wire sequence number), distinct (kind, index, occurrence) entries stay
# distinct, and the stored entries are reproducible. Counting ACK events and
# applying losses will be tested with the channel/engine in later components.

def test_sr_ack_loss_entry_keeps_acknowledged_frame_index():
    # SR: frame 2's ACK is lost, frame 2 is resent and re-ACKed -> drop ACK (2, 2).
    c = SimulationConfig(protocol="sr",
                         scripted_losses=[{"kind": "ack", "index": 2, "occurrence": 2}])
    assert c.scripted_losses == (ScriptedLoss("ack", 2, 2),)
    assert c.warnings() == []


def test_gbn_repeated_cumulative_acks_share_index_with_distinct_occurrences():
    # GBN: frame 1 lost; frames 0, 2, 3 arrive -> ACK events (0,1), (0,2), (0,3).
    # All repeats are associated with frame 0, the highest in-order frame.
    losses = [{"kind": "ack", "index": 0, "occurrence": n} for n in (1, 2, 3)]
    c = SimulationConfig(protocol="gbn", scripted_losses=losses)
    assert [loss.index for loss in c.scripted_losses] == [0, 0, 0]
    assert [loss.occurrence for loss in c.scripted_losses] == [1, 2, 3]
    assert c.warnings() == []  # not reported as duplicates


@pytest.mark.parametrize("kind", ["ack", "frame"])
def test_wraparound_frames_with_same_wire_number_stay_separate(kind):
    # seq_bits=3 -> M=8: frames 1 and 9 both use wire number 1.
    c = SimulationConfig(seq_bits=3, num_frames=12, scripted_losses=[
        {"kind": kind, "index": 1, "occurrence": 1},
        {"kind": kind, "index": 9, "occurrence": 1},
    ])
    assert c.seq_space == 8
    assert [loss.index for loss in c.scripted_losses] == [1, 9]  # 9 is NOT reduced to 1
    assert c.scripted_losses[0] != c.scripted_losses[1]
    assert c.warnings() == []  # different absolute frames, not duplicates


def test_index_beyond_wire_range_is_valid_but_beyond_num_frames_warns():
    # index >= M is normal (absolute index); only index >= num_frames is suspicious.
    ok = SimulationConfig(seq_bits=1, window_size=1, num_frames=12,
                          scripted_losses=[ScriptedLoss("ack", 11)])
    assert ok.warnings() == []
    far = SimulationConfig(num_frames=12, scripted_losses=[ScriptedLoss("ack", 12)])
    assert warned_fields(far) == {"scripted_losses[0]"}


def test_frame_and_ack_entries_with_same_numbers_are_distinct():
    c = SimulationConfig(scripted_losses=[ScriptedLoss("frame", 3, 2), ScriptedLoss("ack", 3, 2)])
    assert c.warnings() == []


def test_frame_loss_scripting_unchanged():
    # index = absolute frame, occurrence = transmission attempt (1 = first send).
    c = SimulationConfig(scripted_losses=[
        {"kind": "frame", "index": 3, "occurrence": 2},
        {"kind": "frame", "index": 0},
    ])
    assert c.scripted_losses == (ScriptedLoss("frame", 3, 2), ScriptedLoss("frame", 0, 1))


def test_scripted_losses_are_reproducible_in_the_same_process():
    data = {"protocol": "gbn", "scripted_losses": [
        {"kind": "ack", "index": 0, "occurrence": 2},
        {"kind": "ack", "index": 9, "occurrence": 1},
        {"kind": "frame", "index": 1, "occurrence": 1},
    ]}
    a = SimulationConfig.from_dict(data)
    b = SimulationConfig.from_dict(data)
    assert a == b
    assert a.scripted_losses == b.scripted_losses  # order preserved, nothing reordered


def test_scripted_losses_are_reproducible_across_processes():
    # Two fresh Python processes with different hash randomisation must
    # produce the identical configuration (no dependence on built-in hash()).
    code = (
        "from arq import SimulationConfig;"
        "c = SimulationConfig.from_dict({'protocol': 'sr', 'scripted_losses': ["
        "{'kind': 'ack', 'index': 9, 'occurrence': 2}, {'kind': 'frame', 'index': 1}]});"
        "print(repr(c))"
    )
    backend = pathlib.Path(__file__).resolve().parents[1]
    outputs = []
    for hash_seed in ("1", "2"):
        env = {**os.environ, "PYTHONHASHSEED": hash_seed, "PYTHONPATH": str(backend)}
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, check=True)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
    assert "ScriptedLoss(kind='ack', index=9, occurrence=2)" in outputs[0]


def test_engine_package_never_calls_builtin_hash():
    # Loss decisions must use SHA-256, never Python's randomised hash().
    package = pathlib.Path(__file__).resolve().parents[1] / "arq"
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if path.is_file()  # arq/invariants.py is currently a directory
        and re.search(r"(?<![\w.])hash\(", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []
