"""Presets: schema, validity, reproducibility and expected results."""

import json

import pytest

from arq import SimulationConfig, run_simulation
from arq.presets import get_preset, list_presets, preset_config

PRESET_IDS = [p["id"] for p in list_presets()]


def test_preset_schema():
    presets = list_presets()
    assert len(PRESET_IDS) == len(set(PRESET_IDS))
    for preset in presets:
        assert list(preset) == ["id", "scenario", "name", "description", "config", "expected"]
        assert preset["name"] and preset["description"]
        json.dumps(preset)


def test_presets_cover_the_required_cases():
    ids = set(PRESET_IDS)
    assert {"no_loss", "frame_loss", "ack_loss", "premature_timeout"} <= ids
    assert [p["scenario"] for p in list_presets() if p["scenario"]] == [f"P{i}" for i in range(7)]


@pytest.mark.parametrize("preset_id", PRESET_IDS)
def test_preset_configs_are_valid_with_the_existing_schema(preset_id):
    config = get_preset(preset_id)["config"]
    assert set(config) == {f.name for f in SimulationConfig.__dataclass_fields__.values()}
    SimulationConfig.from_dict(config)


@pytest.mark.parametrize("preset_id", PRESET_IDS)
@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_presets_are_reproducible(preset_id, protocol):
    a = run_simulation(preset_config(preset_id, protocol)).to_dict()
    b = run_simulation(preset_config(preset_id, protocol)).to_dict()
    assert a == b


@pytest.mark.parametrize("preset_id", [p for p in PRESET_IDS if get_preset(p)["expected"]])
@pytest.mark.parametrize("protocol", ["gbn", "sr"])
def test_presets_produce_their_documented_results(preset_id, protocol):
    expected = get_preset(preset_id)["expected"][protocol]
    result = run_simulation(preset_config(preset_id, protocol))
    observed = {
        "status": result.status, "end_tick": result.end_tick, "failed_frame": result.failed_frame,
        "frame_transmissions": result.metrics.frame_transmissions,
        "retransmissions": result.metrics.retransmissions,
        "completion_tick": result.metrics.completion_tick,
    }
    assert {k: observed[k] for k in expected} == expected


def test_list_presets_returns_independent_copies():
    first = list_presets()
    first[0]["config"]["num_frames"] = 99
    first[0]["config"]["scripted_losses"].append({"kind": "frame", "index": 0})
    assert list_presets()[0]["config"]["num_frames"] == 8
    assert list_presets()[0]["config"]["scripted_losses"] == []


def test_unknown_preset():
    with pytest.raises(KeyError):
        get_preset("no_such_preset")
