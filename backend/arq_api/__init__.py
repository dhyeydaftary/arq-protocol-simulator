"""Flask HTTP API around the ARQ simulation engine.

The engine package `arq` does not depend on Flask; this package only parses
requests, calls the engine and turns results and errors into JSON.

Endpoints
  POST /api/simulate   body: a configuration object (SimulationConfig fields)
  GET  /api/presets    the named presets
  POST /api/compare    body: {"sweep", "runs"?, "base_seed"?, "config"?}

Errors are always JSON: {"error": {"code", "message", "issues"?}} with
400 (invalid_json, invalid_config, invalid_request, limit_exceeded),
404 (not_found), 405 (method_not_allowed), 413 (payload_too_large) or
500 (internal_error). A server traceback is never sent to the client.

Resource limits (see limits.py) are checked after ordinary validation and
before any simulation runs; every API simulation also gets an event backstop.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException

from arq import ConfigError, SimulationConfig, run_simulation
from arq.comparison import build_configs, run_built
from arq.engine import config_to_dict
from arq.errors import ComparisonError, FieldIssue
from arq.presets import list_presets

from .limits import API_MAX_EVENTS, LimitError, comparison_issues, simulation_issues

MAX_REQUEST_BYTES = 64 * 1024

_HTTP_CODES = {404: "not_found", 405: "method_not_allowed", 413: "payload_too_large"}

log = logging.getLogger(__name__)


class _BadJson(Exception):
    pass


def _json(payload: Any, status: int = 200) -> Response:
    # allow_nan=False: the API never emits NaN or Infinity, which are not JSON.
    body = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    return Response(body, status=status, mimetype="application/json")


def _error(status: int, code: str, message: str, issues: list[FieldIssue] | None = None) -> Response:
    error: dict[str, Any] = {"code": code, "message": message}
    if issues is not None:
        error["issues"] = [{"field": i.field, "message": i.message} for i in issues]
    return _json({"error": error}, status)


def _request_json() -> Any:
    """Parse the body as JSON whatever the Content-Type header says."""
    raw = request.get_data(cache=False)
    if not raw.strip():
        raise _BadJson("request body is empty; send a JSON object")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise _BadJson(f"request body is not valid JSON: {error}") from None


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES

    @app.post("/api/simulate")
    def simulate() -> Response:
        config = SimulationConfig.from_dict(_request_json())
        issues = simulation_issues(config)
        if issues:
            raise LimitError(issues)
        result = run_simulation(config, max_events=API_MAX_EVENTS)
        payload = result.to_dict()
        payload["config"] = config_to_dict(config)
        payload["warnings"] = [{"field": w.field, "message": w.message} for w in config.warnings()]
        return _json(payload)

    @app.get("/api/presets")
    def presets() -> Response:
        return _json({"presets": list_presets()})

    @app.post("/api/compare")
    def compare() -> Response:
        sweep, runs, base_seed, configs = build_configs(_request_json())
        any_config = next(iter(configs.values()))["gbn"]
        issues = comparison_issues(sweep, runs, any_config)
        if issues:
            raise LimitError(issues)
        return _json(run_built(sweep, runs, base_seed, configs, max_events=API_MAX_EVENTS))

    @app.errorhandler(_BadJson)
    def bad_json(error: _BadJson) -> Response:
        return _error(400, "invalid_json", str(error))

    @app.errorhandler(ConfigError)
    def bad_config(error: ConfigError) -> Response:
        return _error(400, "invalid_config", "Invalid simulation configuration", error.issues)

    @app.errorhandler(ComparisonError)
    def bad_comparison(error: ComparisonError) -> Response:
        return _error(400, "invalid_request", "Invalid comparison request", error.issues)

    @app.errorhandler(LimitError)
    def over_limit(error: LimitError) -> Response:
        return _error(400, "limit_exceeded", "Request exceeds the server's simulation limits", error.issues)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> Response:
        status = error.code or 500
        return _error(status, _HTTP_CODES.get(status, "http_error"), error.description or error.name)

    @app.errorhandler(Exception)
    def unexpected(error: Exception) -> Response:
        # Logged on the server only; the client gets no traceback or details.
        log.exception("unhandled error")
        return _error(500, "internal_error", "The simulator hit an internal error.")

    return app


app = create_app()
