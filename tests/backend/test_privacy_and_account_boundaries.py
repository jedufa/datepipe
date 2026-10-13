from __future__ import annotations


def test_account_scoped_profile_memory_signal_and_subscription_do_not_leak(api):
  _status, a = api.post("/api/auth/register", {"email": "a@example.test", "password": "correct horse battery staple"}, expected=201)
  _status, b = api.post("/api/auth/register", {"email": "b@example.test", "password": "correct horse battery staple"}, expected=201)

  api.post("/api/settings", {"homeBase": "Graz", "likes": ["jazz"]}, token=a["sessionToken"], expected=200)
  api.post("/api/couple-memory", {"type": "note", "text": "Private jazz preference."}, token=a["sessionToken"], expected=201)
  api.post("/api/matching-signal", {"eventType": "saved", "candidate": {"title": "Jazz", "category": "social"}}, token=a["sessionToken"], expected=201)
  api.post("/api/subscription/activate-dev", {"plan": "monthly-test"}, token=a["sessionToken"], expected=200)

  _status, state_b = api.get("/api/state", token=b["sessionToken"], expected=200)
  _status, memory_b = api.get("/api/couple-memory", token=b["sessionToken"], expected=200)
  _status, matching_b = api.get("/api/matching-profile", token=b["sessionToken"], expected=200)
  _status, subscription_b = api.get("/api/subscription", token=b["sessionToken"], expected=200)

  assert state_b["profile"].get("homeBase") != "Graz"
  assert "Private jazz preference" not in str(memory_b)
  assert matching_b["signalCount"] == 0
  assert subscription_b["status"] == "free"
