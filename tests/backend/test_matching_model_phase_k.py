from __future__ import annotations

from datepipe_backend.matching_model import score_candidate


def test_matching_model_penalizes_generic_dinner_without_specificity():
  profile = {"usualDates": ["dinner"], "specialMeans": ["quiet"], "likes": ["jazz"], "avoid": []}
  generic = {
    "title": "Dinner at a restaurant",
    "category": "culinary",
    "sourceType": "restaurant",
    "specialness": [],
    "specialnessScore": 20,
  }
  specific = {
    "title": "Quiet jazz tasting night",
    "category": "culinary",
    "sourceType": "venue-calendar",
    "specialness": ["live", "limited", "seasonal"],
    "specialnessScore": 85,
    "privacyFit": "high",
    "whyNotGeneric": "A timed live tasting with jazz rather than a normal restaurant booking.",
  }

  assert score_candidate(specific, profile).score > score_candidate(generic, profile).score
  assert "generic dinner/listing risk" in score_candidate(generic, profile).warnings


def test_matching_model_uses_learned_taste_profile():
  candidate = {
    "title": "Architecture walk",
    "category": "cultural",
    "sourceName": "Architecture Forum",
    "specialness": ["unusual"],
    "specialnessScore": 70,
  }
  neutral = {"likes": [], "avoid": []}
  learned = {
    "likes": [],
    "avoid": [],
    "tasteProfile": {
      "categoryWeights": {"cultural": 12},
      "sourceWeights": {"architecture forum": 6},
      "signalWeights": {"architecture": 4},
    },
  }

  assert score_candidate(candidate, learned).score > score_candidate(candidate, neutral).score

