from __future__ import annotations

import re


def normalize_text(value: object) -> str:
  return re.sub(r"[^a-z0-9äöüß]+", " ", str(value or "").lower()).strip()


def list_text(value: object) -> list[str]:
  if isinstance(value, list):
    rows = value
  elif isinstance(value, str):
    rows = re.split(r"[,|]", value)
  else:
    rows = []
  return [normalize_text(item) for item in rows if normalize_text(item)]


def candidate_text(candidate: dict) -> str:
  parts: list[str] = []
  for key in (
    "title",
    "category",
    "sourceName",
    "sourceType",
    "sourceKind",
    "whyNotGeneric",
    "whySpecial",
    "liveVerificationRule",
    "bookingNeed",
    "privacyFit",
    "eventUrl",
  ):
    parts.append(str(candidate.get(key, "")))
  for key in ("specialness", "surpriseFit", "bestFor", "budgetFit", "specialnessSignals"):
    value = candidate.get(key)
    if isinstance(value, list):
      for item in value:
        if isinstance(item, dict):
          parts.append(" ".join(str(part) for part in item.values()))
        else:
          parts.append(str(item))
    else:
      parts.append(str(value or ""))
  return normalize_text(" ".join(parts))


def token_in_text(token: str, text: str) -> bool:
  normalized = normalize_text(token)
  if not normalized:
    return False
  if len(normalized) <= 4 and " " not in normalized:
    return re.search(rf"\b{re.escape(normalized)}\b", text) is not None
  return normalized in text


SIGNALS = [
  ("scarcity", "fresh-timing", 16, ("festival", "seasonal", "current", "limited", "premiere", "opening", "tonight", "this week"), "it has a time window or scarcity"),
  ("place", "place-specific", 15, ("courtyard", "rooftop", "garden", "danube", "donau", "mur", "inn", "wörthersee", "woerthersee", "altstadt", "neighbourhood", "local"), "it belongs to a specific local setting"),
  ("story", "story-hook", 13, ("guided", "behind the scenes", "workshop", "architecture", "photography", "craft", "ceramic", "lab", "artist", "chef"), "it gives the date a story beyond attendance"),
  ("ambience", "quiet-intimate", 10, ("quiet", "intimate", "small", "studio", "jazz", "reading", "listening", "gallery", "museum", "cinema"), "it supports a personal pace"),
  ("effort", "intentional-effort", 11, ("reservation", "booking", "ticket", "limited seats", "registration", "menu", "tasting"), "it rewards deliberate planning"),
  ("pairing", "pairable", 8, ("walk", "market", "wine", "river", "lake", "old town", "bookshop", "record store", "food"), "it can be paired into a thoughtful plan"),
  ("resilience", "weather-flexible", 7, ("rainy day", "indoor", "museum", "cinema", "theatre", "gallery", "library"), "it gives the planner a backup path"),
  ("verification", "source-grounded", 13, ("official", "primary", "venue", "calendar", "operator", "museum program", "theatre", "ticket"), "it comes from a source DatePipe can verify"),
  ("privacy", "surprise-friendly", 9, ("privacyfit high", "privacy high", "discreet", "walk in", "low cost", "accomplice"), "it can be planned discreetly"),
]

SPECIAL_DINNER_SIGNALS = [
  ("chef-menu", ("chef", "tasting menu", "seasonal menu", "menu", "wine pairing"), "the food has a deliberate menu or maker hook"),
  ("setting", ("view", "rooftop", "courtyard", "garden", "terrace", "private room", "candle"), "the setting can make dinner feel intentional"),
  ("scarcity", ("pop up", "limited", "guest chef", "one night", "festival", "reservation"), "the dinner has scarcity or timing"),
  ("ritual", ("anniversary", "occasion", "celebration", "dress", "premium", "special dinner"), "it can carry occasion value"),
]

PENALTIES = [
  ("generic-food", -24, ("restaurant", "restaurants", "dinner", "brunch", "lunch", "bar", "cocktail"), "food-only listings need a stronger hook than simply eating somewhere"),
  ("broad-index", -14, ("home", "homepage", "overview", "all events", "alle veranstaltungen", "search", "suche", "category"), "broad index pages are weaker than specific current activities"),
  ("admin-page", -35, ("privacy", "datenschutz", "impressum", "contact", "kontakt", "login", "jobs", "press", "newsletter"), "administrative pages are not date candidates"),
  ("routine-default", -10, ("usual", "familiar", "mainstream", "generic"), "routine defaults should not outrank distinctive local options"),
]


def evaluate_specialness(candidate: dict, profile: dict | None = None) -> dict:
  text = candidate_text(candidate)
  profile = profile or {}
  dimensions: dict[str, int] = {}
  signals = []
  penalties = []
  hooks = []
  score = 18

  for dimension, signal_id, weight, tokens, reason in SIGNALS:
    matched = [token for token in tokens if token_in_text(token, text)]
    if not matched:
      continue
    score += weight
    dimensions[dimension] = dimensions.get(dimension, 0) + weight
    signals.append({"id": signal_id, "dimension": dimension, "weight": weight, "reason": reason, "matched": matched[:3]})
    hooks.append(reason)

  special_dinner = []
  for signal_id, tokens, reason in SPECIAL_DINNER_SIGNALS:
    matched = [token for token in tokens if token_in_text(token, text)]
    if matched:
      special_dinner.append({"id": signal_id, "reason": reason, "matched": matched[:3]})
      dimensions["special-dinner"] = dimensions.get("special-dinner", 0) + 9
      score += 9
      hooks.append(reason)

  generic_food_matched = []
  for penalty_id, weight, tokens, reason in PENALTIES:
    matched = [token for token in tokens if token_in_text(token, text)]
    if not matched:
      continue
    if penalty_id == "generic-food":
      generic_food_matched = matched
      if special_dinner:
        weight = -6
        reason = "food is present, but it has enough setting/menu/scarcity signals to avoid being generic"
      elif has_food_context(candidate):
        weight = -12
    score += weight
    dimensions["generic-penalty"] = dimensions.get("generic-penalty", 0) + weight
    penalties.append({"id": penalty_id, "weight": weight, "reason": reason, "matched": matched[:3]})

  if candidate.get("privacyFit") == "high":
    score += 6
    dimensions["privacy"] = dimensions.get("privacy", 0) + 6
  if candidate.get("imageUrl"):
    score += 4
    dimensions["presentation"] = dimensions.get("presentation", 0) + 4
  if candidate.get("candidateKind") == "extracted-event-lead":
    score -= 3
    dimensions["verification"] = dimensions.get("verification", 0) - 3

  for item in list_text(profile.get("likes")):
    if item in text:
      score += 8
      dimensions["personal-fit"] = dimensions.get("personal-fit", 0) + 8
      hooks.append(f"it reflects a stated like: {item}")
  for item in list_text(profile.get("usualDates")):
    if item in text:
      score -= 7
      dimensions["novelty"] = dimensions.get("novelty", 0) - 7
      penalties.append({"id": "usual-pattern", "weight": -7, "reason": f"it overlaps with the couple's usual pattern: {item}", "matched": [item]})
  for item in list_text(profile.get("avoid")):
    if item in text:
      score -= 20
      dimensions["personal-fit"] = dimensions.get("personal-fit", 0) - 20
      penalties.append({"id": "profile-avoid", "weight": -20, "reason": f"it overlaps with something the couple wants to avoid: {item}", "matched": [item]})
  for item in list_text(profile.get("specialMeans")):
    if item in text:
      score += 10
      dimensions["meaning"] = dimensions.get("meaning", 0) + 10
      hooks.append(f"it matches what special means to them: {item}")

  if len([value for value in dimensions.values() if value > 0]) >= 4:
    score += 8
  elif len([value for value in dimensions.values() if value > 0]) <= 1:
    score -= 8

  generic_risk = "high" if generic_food_matched and not special_dinner else "medium" if generic_food_matched else "low"
  signals.sort(key=lambda item: item["weight"], reverse=True)
  penalties.sort(key=lambda item: item["weight"])
  hooks = list(dict.fromkeys(hooks))[:5]
  why = "This is special because " + "; ".join(hooks[:3]) + "." if hooks else "This is only lightly special and should not outrank stronger local candidates without live verification."
  final_score = max(1, min(100, score))
  return {
    "specialnessScore": final_score,
    "specialnessSignals": signals[:8],
    "specialnessPenalties": penalties[:6],
    "specialnessDimensions": dimensions,
    "specialnessHooks": hooks,
    "specialDinnerSignals": special_dinner,
    "genericRisk": generic_risk,
    "specialnessLabel": label_for_score(final_score, generic_risk),
    "whySpecial": why,
  }


def has_food_context(candidate: dict) -> bool:
  category = normalize_text(candidate.get("category"))
  source_type = normalize_text(candidate.get("sourceType"))
  return category in {"culinary", "premium", "standard"} or "restaurant" in source_type


def label_for_score(score: int, generic_risk: str) -> str:
  if generic_risk == "high":
    return "generic-risk"
  if score >= 78:
    return "strong"
  if score >= 58:
    return "promising"
  if score >= 38:
    return "thin"
  return "weak"
