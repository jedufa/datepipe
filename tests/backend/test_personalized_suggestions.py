from __future__ import annotations

import json
from pathlib import Path


def test_personalized_suggestions_use_city_pack_without_llm(api):
  body = json.loads(Path("tests/fixtures/sample_personalized_request.json").read_text(encoding="utf-8"))

  _status, payload = api.post("/api/personalized-suggestions", body, expected=200)

  assert payload["schema"] == "datepipe.personalized_suggestions.v1"
  assert payload["digest"]["city"] == "Vienna"
  assert payload["digest"]["candidateCount"] >= 1
  assert len(payload["options"]) >= 1
  assert payload["source"].startswith("backend-city-digest")


def test_personalized_suggestions_accept_authenticated_account_context(api, registered):
  body = {
    "city": "Vienna",
    "date": "2026-06-12",
    "coupleProfile": {"homeBase": "Vienna", "likes": ["jazz"], "avoid": ["clubs"]},
  }

  _status, payload = api.post("/api/personalized-suggestions", body, token=registered["sessionToken"], expected=200)

  assert payload["options"]
  assert payload["digest"]["city"] == "Vienna"
