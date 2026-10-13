from __future__ import annotations


def test_health_returns_stable_success_shape(api):
  _status, payload = api.get("/api/health", expected=200)

  assert payload["ok"] is True
  assert payload["statePath"]
  assert payload["time"]


def test_state_does_not_expose_account_secret_material(api, registered):
  token = registered["sessionToken"]
  _status, _account = api.get("/api/account", token=token, expected=200)

  _status, state = api.get("/api/state", expected=200)

  assert "_accounts" not in state
  assert "passwordHash" not in str(state)
  assert "tokenHash" not in str(state)
  assert token not in str(state)


def test_settings_update_preserves_unrelated_state(api):
  _status, before = api.get("/api/state", expected=200)
  idea_count = len(before.get("ideas", []))

  _status, profile = api.post("/api/settings", {"homeBase": "Graz", "budgetComfort": "casual"}, expected=200)
  _status, after = api.get("/api/state", expected=200)

  assert profile["homeBase"] == "Graz"
  assert after["profile"]["budgetComfort"] == "casual"
  assert len(after.get("ideas", [])) == idea_count
