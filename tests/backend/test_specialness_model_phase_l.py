from __future__ import annotations

from datepipe_backend.specialness_model import evaluate_specialness


def test_phase_l_special_dinner_needs_setting_menu_or_scarcity():
  generic = evaluate_specialness({
    "title": "Dinner restaurant overview",
    "category": "culinary",
    "sourceType": "restaurant",
    "whyNotGeneric": "Restaurants and dinner options.",
  })
  special = evaluate_specialness({
    "title": "Rooftop chef tasting menu with wine pairing",
    "category": "culinary",
    "sourceType": "venue-calendar",
    "bookingNeed": "reservation",
    "privacyFit": "high",
    "whyNotGeneric": "A seasonal chef menu with a city view and limited reservations.",
  })

  assert generic["genericRisk"] == "high"
  assert generic["specialnessLabel"] == "generic-risk"
  assert special["genericRisk"] in {"low", "medium"}
  assert special["specialDinnerSignals"]
  assert special["specialnessScore"] > generic["specialnessScore"]


def test_phase_l_exposes_actionable_hooks_and_dimensions():
  result = evaluate_specialness({
    "title": "Hidden courtyard architecture workshop",
    "category": "cultural",
    "sourceType": "official-city-calendar",
    "privacyFit": "high",
    "whyNotGeneric": "Small local workshop in a specific courtyard.",
  }, {"likes": ["architecture"], "specialMeans": ["local"]})

  assert result["specialnessHooks"]
  assert result["specialnessDimensions"]["story"] > 0
  assert result["specialnessDimensions"]["place"] > 0
  assert result["specialnessLabel"] in {"promising", "strong"}
  assert "This is special because" in result["whySpecial"]

