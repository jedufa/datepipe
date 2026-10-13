from __future__ import annotations

from datepipe_backend.source_quality import (
  city_source_readiness_status,
  extraction_quality_gate_status,
  select_sources_for_research,
  source_mix_summary,
  source_quality_bucket,
)

import server


def test_source_quality_prefers_primary_venues_over_official_baselines():
  sources = [
    {
      "id": "official",
      "name": "Official City Events",
      "scope": "city",
      "sourceKind": "official-city-calendar",
      "trustLevel": "high",
    },
    {
      "id": "venue",
      "name": "Small Jazz Cellar",
      "scope": "city",
      "sourceKind": "jazz-venue",
      "sourceRole": "anchor-venue|evening-compatible|local-secret",
      "trustLevel": "medium",
    },
    {
      "id": "national",
      "name": "National Listings",
      "scope": "national",
      "sourceKind": "event-platform",
      "trustLevel": "medium",
    },
  ]

  selected = select_sources_for_research(sources, 3)

  assert selected[0]["id"] == "venue"
  assert selected[0]["sourceQualityBucket"] == "venue_primary"
  assert source_quality_bucket(sources[0]) == "official_baseline"
  assert source_quality_bucket(sources[2]) == "national_discovery"


def test_source_quality_treats_workshop_and_tasting_providers_as_private_value():
  assert source_quality_bucket({"sourceKind": "workshop-provider", "scope": "city"}) == "venue_primary"
  assert source_quality_bucket({"sourceKind": "tasting-provider", "scope": "city"}) == "venue_primary"
  assert source_quality_bucket({"sourceKind": "atelier-class-provider", "scope": "city"}) == "venue_primary"


def test_discovery_sources_for_vienna_are_not_official_first():
  sources = server.discovery_sources_for_city("Vienna", limit=8)
  buckets = [source["sourceQualityBucket"] for source in sources]

  assert sources[0]["sourceQualityBucket"] in {"venue_primary", "local_curated"}
  assert buckets.count("official_baseline") <= 2
  assert "venue_primary" in buckets
  assert "local_curated" in buckets


def test_source_mix_summary_flags_generic_heavy_sets():
  summary = source_mix_summary([
    {"sourceQualityBucket": "official_baseline"},
    {"sourceQualityBucket": "event_platform"},
    {"sourceQualityBucket": "national_discovery"},
    {"sourceQualityBucket": "venue_primary"},
  ])

  assert summary["genericShare"] == 0.75
  assert summary["privateValueShare"] == 0.25
  assert summary["sourceMixStatus"] == "generic_heavy"


def test_source_selection_does_not_backfill_with_capped_national_sources():
  sources = [
    {"id": "venue", "scope": "city", "sourceKind": "venue-calendar"},
    {"id": "official", "scope": "city", "sourceKind": "official-city-calendar"},
    {"id": "platform", "scope": "city", "sourceKind": "event-platform"},
  ] + [
    {"id": f"national-{index}", "scope": "national", "sourceKind": "event-platform"}
    for index in range(6)
  ]

  selected = select_sources_for_research(sources, 8)

  assert len(selected) == 4
  assert sum(1 for source in selected if source["sourceQualityBucket"] == "national_discovery") == 1


def test_extraction_quality_gate_flags_thin_or_generic_results():
  generic_mix = {"sourceMixStatus": "generic_heavy"}
  private_mix = {"sourceMixStatus": "private_value_led"}

  assert extraction_quality_gate_status(accepted_count=0, selected_mix=private_mix, accepted_mix=private_mix) == "no_accepted_events"
  assert extraction_quality_gate_status(accepted_count=2, selected_mix=private_mix, accepted_mix=private_mix) == "thin_results"
  assert extraction_quality_gate_status(accepted_count=5, selected_mix=generic_mix, accepted_mix=private_mix) == "source_catalog_gap"
  assert extraction_quality_gate_status(accepted_count=5, selected_mix=private_mix, accepted_mix=generic_mix) == "accepted_results_too_generic"
  assert extraction_quality_gate_status(accepted_count=5, selected_mix=private_mix, accepted_mix=private_mix) == "passed"


def test_city_source_readiness_requires_private_value_and_variety():
  generic_mix = source_mix_summary([
    {"sourceQualityBucket": "official_baseline", "sourceKind": "official-city-calendar"},
    {"sourceQualityBucket": "event_platform", "sourceKind": "event-platform"},
  ])
  one_kind_mix = source_mix_summary([
    {"sourceQualityBucket": "venue_primary", "sourceKind": "jazz-venue"},
    {"sourceQualityBucket": "venue_primary", "sourceKind": "jazz-venue"},
  ])
  varied_mix = source_mix_summary([
    {"sourceQualityBucket": "venue_primary", "sourceKind": "jazz-venue"},
    {"sourceQualityBucket": "local_curated", "sourceKind": "local-guide-editorial"},
  ])

  assert city_source_readiness_status(accepted_count=0, source_mix=varied_mix) == "no_validated_sources"
  assert city_source_readiness_status(accepted_count=1, source_mix=varied_mix) == "too_few_validated_sources"
  assert city_source_readiness_status(accepted_count=2, source_mix=generic_mix) == "no_private_value_sources"
  assert city_source_readiness_status(accepted_count=2, source_mix=one_kind_mix) == "thin_source_variety"
  assert city_source_readiness_status(accepted_count=2, source_mix=varied_mix) == "ready"
