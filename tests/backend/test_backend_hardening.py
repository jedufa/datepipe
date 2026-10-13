from __future__ import annotations

import json

import pytest

import server
from datepipe_backend.observability import OperationalEventSink


def test_readiness_and_metrics_are_redacted(api, registered):
  api.post("/api/settings", {"homeBase": "Vienna", "likes": ["jazz"]}, token=registered["sessionToken"], expected=200)

  _status, readiness = api.get("/api/readiness", expected=200)
  _status, metrics = api.get("/api/metrics", expected=200)

  assert readiness["schema"] == "datepipe.readiness.v1"
  assert readiness["ok"] is True
  assert readiness["checks"]["sourcesSelected"]["exists"] is True
  assert metrics["schema"] == "datepipe.metrics.v1"
  assert metrics["accounts"] == 1
  text = json.dumps(metrics)
  assert registered["sessionToken"] not in text
  assert "passwordHash" not in text
  assert metrics["operationalEvents"]["privacyScan"]["ok"] is True


def test_login_rate_limit_is_enforced_and_redacted(api, isolated_runtime):
  api.post("/api/auth/register", {"email": "limited@example.test", "password": "correct horse battery staple"}, expected=201)
  wrong = "wrong password must not be stored"

  for _ in range(5):
    _status, payload = api.post("/api/auth/login", {"email": "limited@example.test", "password": wrong}, expected=401)
    assert payload["error"] == "invalid_credentials"

  _status, limited = api.post("/api/auth/login", {"email": "limited@example.test", "password": "correct horse battery staple"}, expected=429)

  assert limited["error"] == "rate_limited"
  raw_state = server.load_state()
  state_text = json.dumps(raw_state)
  assert "limited@example.test" not in state_text
  assert wrong not in state_text
  assert "rate_limited" in state_text

  metrics_status, metrics = api.get("/api/metrics", expected=200)
  assert metrics_status == 200
  assert metrics["operationalEvents"]["categoryCounts"]["rate_limit_hit"] == 1
  assert wrong not in json.dumps(metrics)


def test_operational_sink_rejects_sensitive_extra(isolated_runtime):
  sink = OperationalEventSink(server.OPERATIONAL_LOG_PATH, server.now_iso)

  with pytest.raises(ValueError, match="forbidden sensitive"):
    sink.record(
      category="server_error",
      request_id="req",
      status_code=500,
      extra={"password": "do-not-store"},
    )


def test_json_state_write_survives_repeated_updates(api):
  for index in range(8):
    api.post("/api/settings", {"homeBase": f"Vienna {index}"}, expected=200)

  _status, state = api.get("/api/state", expected=200)
  assert state["profile"]["homeBase"] == "Vienna 7"
