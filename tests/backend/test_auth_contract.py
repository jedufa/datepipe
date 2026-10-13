from __future__ import annotations

import server


def test_register_login_refresh_logout_contract(api, isolated_runtime):
  _status, registered = api.post(
    "/api/auth/register",
    {"email": "auth@example.test", "password": "correct horse battery staple", "deviceLabel": "Phone", "platform": "android"},
    expected=201,
  )
  token = registered["sessionToken"]
  assert token.startswith("dtp_")

  raw_state = server.load_state()
  assert token not in str(raw_state)
  assert "correct horse battery staple" not in str(raw_state)
  assert "passwordHash" in str(raw_state)
  assert "tokenHash" in str(raw_state)

  _status, login = api.post(
    "/api/auth/login",
    {"email": "auth@example.test", "password": "correct horse battery staple", "deviceId": registered["deviceId"]},
    expected=200,
  )
  _status, refreshed = api.post("/api/auth/refresh", {"deviceId": login["deviceId"]}, token=login["sessionToken"], expected=200)

  assert refreshed["oldTokenRevoked"] is True
  api.post("/api/auth/refresh", {"deviceId": login["deviceId"]}, token=login["sessionToken"], expected=401)
  api.post("/api/auth/logout", {}, token=refreshed["sessionToken"], expected=200)
  api.get("/api/account", token=refreshed["sessionToken"], expected=401)


def test_login_rejects_bad_password_without_echoing_secret(api):
  api.post("/api/auth/register", {"email": "badlogin@example.test", "password": "correct horse battery staple"}, expected=201)

  _status, payload = api.post("/api/auth/login", {"email": "badlogin@example.test", "password": "wrong secret"}, expected=401)

  assert payload["error"] == "invalid_credentials"
  assert "wrong secret" not in str(payload)
