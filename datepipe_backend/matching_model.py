from __future__ import annotations

import re
from dataclasses import dataclass


def normalize_key(value: object) -> str:
  return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def listish(value: object) -> list[str]:
  if isinstance(value, list):
    raw = value
  elif isinstance(value, str):
    raw = re.split(r"[,|]", value)
  else:
    raw = []
  return [normalize_key(item) for item in raw if normalize_key(item)]


def text_list(value: object) -> list[str]:
  if not isinstance(value, list):
    return []
  out: list[str] = []
  for item in value:
    if isinstance(item, dict):
      out.append(" ".join(str(part) for part in item.values()))
    else:
      out.append(str(item))
  return out


@dataclass(frozen=True)
class MatchResult:
  score: int
  reasons: list[str]
  warnings: list[str]
  dimensions: dict[str, int]


class CoupleMatchModel:
  def __init__(self, profile: dict):
    self.profile = profile
    self.likes = listish(profile.get("likes"))
    self.avoid = listish(profile.get("avoid"))
    self.usual = listish(profile.get("usualDates"))
    self.special_means = listish(profile.get("specialMeans"))
    self.constraints = listish(profile.get("constraints"))
    self.food_drink = listish(profile.get("foodDrink"))
    self.occasions = listish(profile.get("specialOccasions"))
    self.surprise_level = normalize_key(profile.get("surpriseLevel"))
    self.energy = normalize_key(profile.get("energy"))

  def score_candidate(self, candidate: dict) -> MatchResult:
    text = normalize_key(" ".join([
      candidate.get("title", ""),
      candidate.get("category", ""),
      candidate.get("sourceName", ""),
      candidate.get("sourceType", ""),
      candidate.get("whyNotGeneric", ""),
      candidate.get("whySpecial", ""),
      " ".join(text_list(candidate.get("specialness"))),
      " ".join(text_list(candidate.get("specialnessSignals"))),
    ]))
    dimensions = {
      "profileFit": 0,
      "specialMeaning": 0,
      "novelty": 0,
      "privacy": 0,
      "practicality": 0,
      "learnedTaste": 0,
      "genericPenalty": 0,
    }
    reasons: list[str] = []
    warnings: list[str] = []

    for item in self.likes:
      if item in text:
        dimensions["profileFit"] += 12
        reasons.append(f"matches like:{item}")
    for item in self.avoid:
      if item in text:
        dimensions["profileFit"] -= 30
        warnings.append(f"matches avoid:{item}")
    for item in self.usual:
      if item in text:
        dimensions["novelty"] -= 8
        warnings.append(f"usual pattern:{item}")
    for item in self.special_means:
      if item in text:
        dimensions["specialMeaning"] += 10
        reasons.append(f"special means:{item}")
    for item in self.constraints:
      if item in text:
        dimensions["practicality"] -= 14
        warnings.append(f"possible constraint:{item}")
    for item in self.food_drink:
      if item in text:
        dimensions["profileFit"] += 5
        reasons.append(f"food/drink fit:{item}")
    for item in self.occasions:
      if item in text:
        dimensions["specialMeaning"] += 8
        reasons.append(f"occasion fit:{item}")
    couple_memory = self.profile.get("coupleMemory") if isinstance(self.profile.get("coupleMemory"), dict) else {}
    memories = couple_memory.get("memories") if isinstance(couple_memory.get("memories"), list) else []
    for memory in memories[:40]:
      memory_text = normalize_key(memory.get("text", ""))
      memory_type = normalize_key(memory.get("type", ""))
      confidence = float(memory.get("confidence") or 0.5)
      weight = int(memory.get("weight") or 1)
      if not any(token in text for token in memory_text.split() if len(token) > 3):
        continue
      adjustment = max(1, min(8, round(confidence * weight)))
      if memory_type in {"avoid", "usual pattern"}:
        dimensions["learnedTaste"] -= adjustment
        warnings.append("couple memory conflict")
      else:
        dimensions["learnedTaste"] += adjustment
        reasons.append("couple memory match")

    if candidate.get("privacyFit") == "high":
      dimensions["privacy"] += 6
      reasons.append("discreet/private fit")
    if candidate.get("imageUrl"):
      dimensions["practicality"] += 3
    if candidate.get("bookingNeed") in {"walk-in", "unknown"}:
      dimensions["practicality"] += 2
    if self.surprise_level and self.surprise_level in normalize_key(candidate.get("surpriseFit")):
      dimensions["specialMeaning"] += 8

    dimensions["specialMeaning"] += int(candidate.get("specialnessScore", 50)) // 10
    specialness = listish(candidate.get("specialness"))
    if any(item in specialness for item in {"seasonal", "limited", "unusual", "hands on", "live", "opening", "festival"}):
      dimensions["novelty"] += 8
      reasons.append("has time/place specificity")

    title = normalize_key(candidate.get("title"))
    category = normalize_key(candidate.get("category"))
    source_type = normalize_key(candidate.get("sourceType"))
    if self.is_generic_dinner(title, category, source_type, text, candidate):
      dimensions["genericPenalty"] -= 24
      warnings.append("generic dinner/listing risk")

    taste = self.profile.get("tasteProfile") if isinstance(self.profile.get("tasteProfile"), dict) else {}
    dimensions["learnedTaste"] += self.learned_taste_adjustment(candidate, text, taste)

    score = 50 + sum(dimensions.values())
    return MatchResult(
      score=max(1, min(100, score)),
      reasons=reasons[:8],
      warnings=warnings[:8],
      dimensions=dimensions,
    )

  def learned_taste_adjustment(self, candidate: dict, text: str, taste: dict) -> int:
    score = 0
    category_weights = taste.get("categoryWeights") if isinstance(taste.get("categoryWeights"), dict) else {}
    source_weights = taste.get("sourceWeights") if isinstance(taste.get("sourceWeights"), dict) else {}
    signal_weights = taste.get("signalWeights") if isinstance(taste.get("signalWeights"), dict) else {}
    category = normalize_key(candidate.get("category"))
    source = normalize_key(candidate.get("sourceName"))
    if category:
      score += max(-18, min(18, int(category_weights.get(category, 0))))
    if source:
      score += max(-14, min(14, int(source_weights.get(source, 0))))
    for token, weight in signal_weights.items():
      if normalize_key(token) in text:
        score += max(-8, min(8, int(weight)))
    for token in listish(taste.get("avoidTokens")):
      if token in text:
        score -= 12
    return score

  @staticmethod
  def is_generic_dinner(title: str, category: str, source_type: str, text: str, candidate: dict) -> bool:
    if category not in {"culinary", "standard"} and "restaurant" not in source_type:
      return False
    generic_tokens = {"dinner", "restaurant", "food", "meal", "bar", "drinks"}
    has_generic_title = any(token in title for token in generic_tokens)
    has_specific_signal = bool(candidate.get("specialness")) or any(token in text for token in ["tasting", "chef", "market", "seasonal", "workshop", "festival", "view", "menu"])
    return has_generic_title and not has_specific_signal


def score_candidate(candidate: dict, profile: dict) -> MatchResult:
  return CoupleMatchModel(profile).score_candidate(candidate)


def rank_candidates(candidates: list[dict], profile: dict) -> list[tuple[dict, MatchResult]]:
  rows = [(candidate, score_candidate(candidate, profile)) for candidate in candidates if isinstance(candidate, dict)]
  rows.sort(key=lambda item: item[1].score, reverse=True)
  return rows
