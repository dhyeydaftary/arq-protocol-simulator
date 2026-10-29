"""HTTP API tests. They need Flask (declared in pyproject's "api"/"dev"
extras) and are skipped when it is not installed."""

import json

import pytest

flask = pytest.importorskip("flask")

import arq_api                                         # noqa: E402
from arq.presets import get_preset, list_presets      # noqa: E402

P_EXPECTED = {
    ("no_loss", "gbn"): (8, 0, 13), ("no_loss", "sr"): (8, 0, 13),
    ("frame_loss", "gbn"): (12, 4, 25), ("frame_loss", "sr"): (9, 1, 21),
    ("ack_loss", "gbn"): (8, 0, 14), ("ack_loss", "sr"): (9, 1, 21),
    ("last_ack_loss", "gbn"): (9, 1, 25), ("last_ack_loss", "sr"): (9, 1, 21),
    ("consecutive_frame_losses", "gbn"): (12, 4, 25), ("consecutive_frame_losses", "sr"): (11, 3, 21),
    ("repeated_frame_loss", "gbn"): (16, 8, 33), ("repeated_frame_loss", "sr"): (10, 2, 29),
    ("premature_timeout", "gbn"): (8, 4, 8), ("premature_timeout", "sr"): (8, 4, 8),
}


@pytest.fixture
def client():
    return arq_api.create_app().test_client()


def post(client, path, body):
    data = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return client.post(path, data=data, content_type="application/json")


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.mimetype == "application/json"
    error = response.get_json()["error"]
    assert error["code"] == code and error["message"]
    assert "Traceback" not in response.get_data(as_text=True)
    return error


# --- /api/simulate --------------------------------------------------------------

def test_simulate_returns_result_trace_and_metrics(client):
    response = post(client, "/api/simulate", get_preset("frame_loss")["config"])
    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"status", "end_tick", "events_processed", "failed_frame", "metrics",
                         "trace", "config", "warnings"}
    assert body["status"] == "completed"
    assert body["metrics"]["completion_tick"] == 25
    entry = body["trace"][0]
    assert list(entry) == ["event_id", "step", "time", "type", "layer", "actor", "state", "details"]
    assert entry["type"] == "transfer_started" and entry["event_id"] is None


def test_simulate_with_defaults_from_an_empty_object(client):
    body = post(client, "/api/simulate", {}).get_json()
    assert body["config"]["protocol"] == "gbn" and body["config"]["seed"] == 42


@pytest.mark.parametrize("preset_id, protocol", sorted(P_EXPECTED))
def test_reference_scenarios_through_the_api(client, preset_id, protocol):
    config = {**get_preset(preset_id)["config"], "protocol": protocol}
    m = post(client, "/api/simulate", config).get_json()["metrics"]
    assert (m["frame_transmissions"], m["retransmissions"], m["completion_tick"]) == P_EXPECTED[(preset_id, protocol)]


def test_simulate_reports_warnings(client):
    body = post(client, "/api/simulate", {"timeout": 4}).get_json()
    assert [w["field"] for w in body["warnings"]] == ["timeout"]


def test_simulate_is_reproducible(client):
    config = {"protocol": "sr", "seed": 7, "frame_loss": 0.3}
    assert post(client, "/api/simulate", config).get_data() == post(client, "/api/simulate", config).get_data()


def test_invalid_config_lists_every_problem(client):
    error = assert_error(post(client, "/api/simulate", {"window_size": 0, "timeout": -1}), 400, "invalid_config")
    assert {i["field"] for i in error["issues"]} == {"window_size", "timeout"}


def test_unknown_field_gets_a_suggestion(client):
    error = assert_error(post(client, "/api/simulate", {"propagation_dely": 3}), 400, "invalid_config")
    assert "propagation_delay" in error["issues"][0]["message"]


@pytest.mark.parametrize("body", ["", "   ", "{not json", "{\"protocol\": }", b"\xff\xfe"])
def test_malformed_json(client, body):
    assert_error(post(client, "/api/simulate", body), 400, "invalid_json")


@pytest.mark.parametrize("body", [[], "\"gbn\"", 5, None])
def test_json_that_is_not_an_object(client, body):
    raw = body if isinstance(body, str) else json.dumps(body)
    assert_error(post(client, "/api/simulate", raw), 400, "invalid_config")


def test_wrong_types_are_client_errors(client):
    error = assert_error(post(client, "/api/simulate", {"num_frames": "8", "frame_loss": "NaN"}), 400,
                         "invalid_config")
    assert {i["field"] for i in error["issues"]} == {"num_frames", "frame_loss"}


def test_nan_is_rejected(client):
    assert_error(post(client, "/api/simulate", '{"frame_loss": NaN}'), 400, "invalid_config")


def test_body_too_large(client):
    huge = json.dumps({"scripted_losses": [{"kind": "frame", "index": 0}] * 5000})
    assert_error(post(client, "/api/simulate", huge), 413, "payload_too_large")


def test_internal_errors_hide_details(client, monkeypatch):
    def broken(config, **kwargs):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(arq_api, "run_simulation", broken)
    response = post(client, "/api/simulate", {})
    assert_error(response, 500, "internal_error")
    assert "secret internal detail" not in response.get_data(as_text=True)


# --- routing --------------------------------------------------------------------

def test_unknown_endpoint(client):
    assert_error(client.get("/api/nothing"), 404, "not_found")


@pytest.mark.parametrize("method, path", [("get", "/api/simulate"), ("post", "/api/presets"),
                                          ("get", "/api/compare"), ("delete", "/api/simulate")])
def test_wrong_method(client, method, path):
    assert_error(getattr(client, method)(path), 405, "method_not_allowed")


# --- /api/presets ---------------------------------------------------------------

def test_presets_endpoint(client):
    response = client.get("/api/presets")
    assert response.status_code == 200
    assert response.get_json() == {"presets": list_presets()}


# --- /api/compare ---------------------------------------------------------------

def test_compare_frame_loss(client):
    body = post(client, "/api/compare", {"sweep": "frame_loss", "runs": 2}).get_json()
    assert body["values"][0] == 0.0 and body["values"][-1] == 0.4
    assert body["seeds"] == {"first": 42, "last": 43, "paired": True}
    point = body["points"][0]
    assert set(point) == {"value", "gbn", "sr"}
    assert set(point["gbn"]) == {
        "runs", "completed", "failed_retry_limit", "aborted_safety_limit", "completion_rate",
        "mean_goodput_all", "mean_goodput_completed", "mean_retransmissions",
        "mean_transmissions", "mean_completion_time",
    }


def test_compare_window_size(client):
    body = post(client, "/api/compare", {"sweep": "window_size", "runs": 1}).get_json()
    assert body["values"] == list(range(1, 9)) and body["fixed_config"]["seq_bits"] == 4


@pytest.mark.parametrize("body, field", [
    ({"sweep": "frame_loss", "config": {"scripted_losses": [{"kind": "ack", "index": 1}]}}, "config.scripted_losses"),
    ({"sweep": "frame_loss", "runs": 201}, "runs"),
    ({"sweep": "frame_loss", "runs": 0}, "runs"),
    ({"sweep": "nope"}, "sweep"),
    ({"sweep": "frame_loss", "config": {"num_frames": 101}}, "config.num_frames"),
])
def test_compare_rejects_invalid_requests(client, body, field):
    error = assert_error(post(client, "/api/compare", body), 400, "invalid_request")
    assert field in {i["field"] for i in error["issues"]}


def test_compare_malformed_json(client):
    assert_error(post(client, "/api/compare", "{"), 400, "invalid_json")


# --- resource limits ------------------------------------------------------------

import arq.comparison                                   # noqa: E402
from arq import run_simulation as real_run_simulation   # noqa: E402


def spy(monkeypatch, module, calls):
    """Replace module.run_simulation with a recording wrapper around the real one."""
    def recording(config, **kwargs):
        calls.append(kwargs)
        return real_run_simulation(config, **kwargs)
    monkeypatch.setattr(module, "run_simulation", recording)


def test_simulate_exactly_at_the_run_limit_is_accepted(client):
    response = post(client, "/api/simulate", {"num_frames": 100, "retry_limit": 24})   # 2,500
    assert response.status_code == 200
    assert response.get_json()["config"]["retry_limit"] == 24


@pytest.mark.parametrize("num_frames, retry_limit, budget", [(41, 60, 2501), (100, 25, 2600)])
def test_simulate_over_the_run_limit_is_rejected_before_running(client, monkeypatch, num_frames, retry_limit, budget):
    calls = []
    spy(monkeypatch, arq_api, calls)
    error = assert_error(post(client, "/api/simulate", {"num_frames": num_frames, "retry_limit": retry_limit}),
                         400, "limit_exceeded")
    assert [i["field"] for i in error["issues"]] == ["transmissions_per_run"]
    assert str(budget) in error["issues"][0]["message"]
    assert calls == []


def test_invalid_config_takes_precedence_over_the_limit(client):
    body = {"num_frames": 100, "retry_limit": 1000, "window_size": 0}
    assert_error(post(client, "/api/simulate", body), 400, "invalid_config")


def test_simulate_passes_the_api_event_backstop(client, monkeypatch):
    calls = []
    spy(monkeypatch, arq_api, calls)
    assert post(client, "/api/simulate", {}).status_code == 200
    assert calls == [{"max_events": 25_000}]


@pytest.mark.parametrize("body, field", [
    ({"sweep": "frame_loss", "runs": 106}, "total_transmissions"),                       # 251,856
    ({"sweep": "window_size", "runs": 119}, "total_transmissions"),                      # 251,328
    ({"sweep": "window_size", "runs": 1, "config": {"num_frames": 100, "retry_limit": 25}},
     "transmissions_per_run"),                                                           # total fits
])
def test_oversized_comparison_is_rejected_before_any_simulation(client, monkeypatch, body, field):
    calls = []
    spy(monkeypatch, arq.comparison, calls)
    error = assert_error(post(client, "/api/compare", body), 400, "limit_exceeded")
    assert [i["field"] for i in error["issues"]] == [field]
    assert calls == []


def test_invalid_comparison_takes_precedence_over_the_limit(client):
    body = {"sweep": "frame_loss", "runs": 200,
            "config": {"num_frames": 100, "retry_limit": 1000,
                       "scripted_losses": [{"kind": "frame", "index": 0}]}}
    assert_error(post(client, "/api/compare", body), 400, "invalid_request")


def test_compare_passes_the_api_event_backstop_to_every_run(client, monkeypatch):
    calls = []
    spy(monkeypatch, arq.comparison, calls)
    assert post(client, "/api/compare", {"sweep": "frame_loss", "runs": 1}).status_code == 200
    assert len(calls) == 9 * 2 and all(c == {"max_events": 25_000} for c in calls)


@pytest.mark.parametrize("sweep, points", [("frame_loss", 9), ("window_size", 8)])
def test_default_comparison_requests_still_run(client, sweep, points):
    response = post(client, "/api/compare", {"sweep": sweep})
    assert response.status_code == 200
    body = response.get_json()
    assert body["runs"] == 50 and len(body["points"]) == points
