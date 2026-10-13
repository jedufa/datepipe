from __future__ import annotations

import json
from pathlib import Path

import server


def test_matching_signal_updates_learned_profile(api, registered):
  signal = json.loads(Path("tests/fixtures/sample_matching_signal.json").read_text(encoding="utf-8"))
  api.post("/api/matching-signal", signal, token=registered["sessionToken"], expected=201)

  _status, profile = api.get("/api/matching-profile", token=registered["sessionToken"], expected=200)

  assert profile["signalCount"] == 1
  assert profile["categoryWeights"]["cultural"] > 0


def test_specialness_and_generic_penalty_characterization():
  generic = server.apply_specialness_model({"title": "Dinner restaurant listing", "category": "culinary", "eventUrl": "https://example.test/restaurants"}, {"usualDates": ["dinner"]})
  specific = server.apply_specialness_model({"title": "Hidden courtyard jazz workshop", "category": "social", "eventUrl": "https://example.test/jazz", "sourceKind": "official-city-calendar", "privacyFit": "high"}, {"likes": ["jazz"], "usualDates": ["dinner"]})

  assert generic["specialnessScore"] < specific["specialnessScore"]
  assert generic["specialnessPenalties"]
  assert specific["specialnessSignals"]


def test_matching_review_endpoint_reports_ready_counts(api):
  _status, review = api.query("/api/matching-review", {"city": "Vienna", "date": "2026-06-12", "extractEvents": "false"}, expected=200)

  assert review["schema"] == "datepipe.matching_contract_review.v1"
  assert review["candidateCount"] >= review["readyCount"] >= 0
