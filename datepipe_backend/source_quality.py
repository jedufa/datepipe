from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable


SOURCE_BUCKET_ORDER = (
  "venue_primary",
  "local_curated",
  "official_baseline",
  "event_platform",
  "national_discovery",
  "other",
)


def normalize_source_text(value: object) -> str:
  return " ".join(str(value or "").lower().replace("_", "-").split())


def source_text(source: dict) -> str:
  values = [
    source.get("id", ""),
    source.get("name", ""),
    source.get("scope", ""),
    source.get("sourceKind") or source.get("sourceType") or "",
    source.get("sourceRole", ""),
    source.get("variety", ""),
    source.get("datePipeUse", ""),
    source.get("confidence", ""),
    source.get("trustLevel", ""),
    " ".join(source.get("bestFor", []) if isinstance(source.get("bestFor"), list) else []),
    " ".join(source.get("categories", []) if isinstance(source.get("categories"), list) else []),
  ]
  return normalize_source_text(" ".join(str(value) for value in values))


def source_quality_bucket(source: dict) -> str:
  text = source_text(source)
  scope = normalize_source_text(source.get("scope"))
  kind = normalize_source_text(source.get("sourceKind") or source.get("sourceType"))

  if scope == "national":
    return "national_discovery"
  if "event-platform" in kind or "ticket-platform" in kind:
    return "event_platform"
  if (
    "venue" in kind
    or "anchor-venue" in text
    or "museum-program" in kind
    or "cinema" in kind
    or "theatre" in kind
    or "music-venue" in kind
    or "jazz-venue" in kind
    or "dance-performance" in kind
    or "comedy-cabaret" in kind
    or "art-photography" in kind
    or "gallery-program" in kind
    or "festival" in kind
    or "outdoor-cinema" in kind
    or "food-discovery" in kind
    or "workshop-provider" in kind
    or "tasting-provider" in kind
    or "atelier" in kind
    or "class-provider" in kind
  ):
    return "venue_primary"
  if "local-guide" in kind or "editorial" in kind or "local-event-calendar" in kind:
    return "local_curated"
  if "official" in kind or "tourism-calendar" in kind or "city-calendar" in kind or "city-events" in kind:
    return "official_baseline"
  return "other"


def source_quality_score(source: dict) -> int:
  text = source_text(source)
  bucket = source_quality_bucket(source)
  score = {
    "venue_primary": 58,
    "local_curated": 50,
    "official_baseline": 32,
    "event_platform": 28,
    "national_discovery": 12,
    "other": 22,
  }.get(bucket, 22)

  trust = normalize_source_text(source.get("trustLevel") or source.get("confidence"))
  if trust == "high":
    score += 12
  elif trust == "medium":
    score += 6

  scope = normalize_source_text(source.get("scope"))
  if scope in {"city", "city-pack"}:
    score += 10
  if scope == "city-pack":
    score += 6

  if "city-core" in text:
    score += 6
  if any(token in text for token in ("local-secret", "weird-original", "quiet-intimate", "accomplice-friendly")):
    score += 12
  if "fresh" in text:
    score += 6
  if any(token in text for token in ("evening-compatible", "rainy-day-backup", "food-pairing")):
    score += 5
  if any(token in text for token in ("tourist-but-useful", "obvious")):
    score -= 8
  if bucket == "national_discovery":
    score -= 8

  return score


def source_quality_profile(source: dict) -> dict:
  return {
    "sourceQualityBucket": source_quality_bucket(source),
    "sourceQualityScore": source_quality_score(source),
  }


def source_bucket_cap(bucket: str, requested_limit: int) -> int:
  if bucket == "official_baseline":
    return max(2, min(6, requested_limit // 4))
  if bucket == "event_platform":
    return max(2, min(5, requested_limit // 5))
  if bucket == "national_discovery":
    return max(1, min(3, requested_limit // 8))
  return requested_limit


def select_sources_for_research(sources: Iterable[dict], limit: int) -> list[dict]:
  if limit <= 0:
    return []

  grouped: dict[str, list[dict]] = defaultdict(list)
  for source in sources:
    row = dict(source)
    row.update(source_quality_profile(row))
    grouped[row["sourceQualityBucket"]].append(row)

  for rows in grouped.values():
    rows.sort(key=lambda item: source_quality_score(item), reverse=True)

  selected: list[dict] = []
  counts: Counter[str] = Counter()
  while len(selected) < limit:
    progressed = False
    for bucket in SOURCE_BUCKET_ORDER:
      if len(selected) >= limit:
        break
      if counts[bucket] >= source_bucket_cap(bucket, limit):
        continue
      rows = grouped.get(bucket) or []
      if not rows:
        continue
      selected.append(rows.pop(0))
      counts[bucket] += 1
      progressed = True
    if not progressed:
      break

  return selected[:limit]


def source_mix_summary(items: Iterable[dict]) -> dict:
  buckets = Counter()
  kinds = Counter()
  scopes = Counter()
  for item in items:
    bucket = item.get("sourceQualityBucket") or source_quality_bucket(item)
    buckets[str(bucket)] += 1
    kind = str(item.get("sourceKind") or item.get("sourceType") or "unknown")
    kinds[kind] += 1
    scopes[str(item.get("sourceScope") or item.get("scope") or "unknown")] += 1
  total = sum(buckets.values())
  private_value = buckets["venue_primary"] + buckets["local_curated"]
  generic = buckets["official_baseline"] + buckets["event_platform"] + buckets["national_discovery"]
  return {
    "byBucket": buckets.most_common(),
    "byKind": kinds.most_common(12),
    "byScope": scopes.most_common(),
    "privateValueShare": round(private_value / total, 3) if total else 0,
    "genericShare": round(generic / total, 3) if total else 0,
    "sourceMixStatus": source_mix_status(buckets),
  }


def extraction_quality_gate_status(*, accepted_count: int, selected_mix: dict, accepted_mix: dict) -> str:
  if accepted_count == 0:
    return "no_accepted_events"
  if selected_mix.get("sourceMixStatus") in {"weak_no_private_value_sources", "generic_heavy"}:
    return "source_catalog_gap"
  if accepted_mix.get("sourceMixStatus") in {"weak_no_private_value_sources", "generic_heavy"}:
    return "accepted_results_too_generic"
  if accepted_count < 3:
    return "thin_results"
  return "passed"


def city_source_readiness_status(*, accepted_count: int, source_mix: dict, min_sources: int = 2) -> str:
  if accepted_count == 0:
    return "no_validated_sources"
  if accepted_count < min_sources:
    return "too_few_validated_sources"
  mix_status = str(source_mix.get("sourceMixStatus") or "")
  if mix_status == "weak_no_private_value_sources":
    return "no_private_value_sources"
  if mix_status == "generic_heavy":
    return "generic_source_mix"
  kinds = [kind for kind, _count in source_mix.get("byKind", []) if str(kind).strip()]
  if len(kinds) < 2:
    return "thin_source_variety"
  return "ready"


def source_mix_status(buckets: Counter[str]) -> str:
  total = sum(buckets.values())
  if not total:
    return "empty"
  private_value = buckets["venue_primary"] + buckets["local_curated"]
  generic = buckets["official_baseline"] + buckets["event_platform"] + buckets["national_discovery"]
  if private_value == 0:
    return "weak_no_private_value_sources"
  if generic / total > 0.65:
    return "generic_heavy"
  if private_value / total >= 0.55:
    return "private_value_led"
  return "mixed"
