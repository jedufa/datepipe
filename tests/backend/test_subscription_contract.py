from __future__ import annotations


def test_subscription_default_is_free(api, registered):
  _status, subscription = api.get("/api/subscription", token=registered["sessionToken"], expected=200)

  assert subscription["status"] == "free"
  assert subscription["entitlements"]["heavyBackendAi"] is False


def test_dev_subscription_activation_is_account_scoped(api):
  _status, owner = api.post("/api/auth/register", {"email": "sub-a@example.test", "password": "correct horse battery staple"}, expected=201)
  _status, other = api.post("/api/auth/register", {"email": "sub-b@example.test", "password": "correct horse battery staple"}, expected=201)

  _status, activated = api.post("/api/subscription/activate-dev", {"plan": "monthly-test"}, token=owner["sessionToken"], expected=200)
  _status, other_subscription = api.get("/api/subscription", token=other["sessionToken"], expected=200)

  assert activated["status"] == "monthly_active"
  assert activated["entitlements"]["heavyBackendAi"] is True
  assert other_subscription["status"] == "free"
  assert other_subscription["entitlements"]["heavyBackendAi"] is False
