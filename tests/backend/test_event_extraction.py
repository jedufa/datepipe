from __future__ import annotations

import server


def test_extracted_events_endpoint_uses_mocked_pages(api, monkeypatch, fixture_text):
  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    if "jsonld" in url:
      return fixture_text("sample_jsonld_event_page.html")
    return fixture_text("sample_links_page.html")

  monkeypatch.setattr(server, "discovery_sources_for_city", lambda city, limit=24: [
    {"id": "fixture-jsonld", "name": "Fixture JSON-LD", "url": "https://example.test/jsonld", "sourceType": "venue-calendar", "categories": ["music-venue"], "confidence": "high"},
    {"id": "fixture-links", "name": "Fixture Links", "url": "https://example.test/events", "sourceType": "venue-calendar", "categories": ["workshop-provider"], "confidence": "high"},
  ])
  monkeypatch.setattr(server, "fetch_text", fake_fetch)

  _status, payload = api.query("/api/extracted-events", {"city": "Vienna", "date": "2026-06-12", "force": "true"}, expected=200)

  assert payload["schema"] == "datepipe.event_extraction.v1"
  assert payload["eventCount"] >= 2
  assert payload["insertedEventCount"] >= 2
  assert payload["qualityGateStatus"] in {"passed", "thin_results", "source_catalog_gap", "accepted_results_too_generic"}
  assert payload["qualitySummary"]["selectedSourceMix"]["byBucket"]
  assert payload["qualitySummary"]["acceptedSourceMix"]["byBucket"]
  assert any("Jazz" in item["title"] or "jazz" in item["title"].lower() for item in payload["events"])

  _status, stored = api.query("/api/research/events", {"city": "Vienna", "date": "2026-06-12"}, expected=200)

  assert stored["schema"] == "datepipe.research_events.v1"
  assert stored["eventCount"] >= 2
  assert all(item["researchState"]["reviewStatus"] == "new" for item in stored["events"])

  _status, cached = api.query("/api/extracted-events", {"city": "Vienna", "date": "2026-06-12"}, expected=200)

  assert cached["cacheHit"] is True
  assert cached["liveFetch"] is False
  assert cached["fetchedSourceCount"] == 0


def test_event_extraction_handles_empty_invalid_and_network_failure(monkeypatch, isolated_runtime, fixture_text):
  source = {"id": "empty", "name": "Empty", "url": "https://example.test/empty", "sourceType": "venue-calendar", "categories": []}

  monkeypatch.setattr(server, "fetch_text", lambda *args, **kwargs: fixture_text("sample_empty_page.html"))
  assert server.extract_events_from_source(source, "Vienna", "2026-06-12") == []

  monkeypatch.setattr(server, "fetch_text", lambda *args, **kwargs: "<html><a")
  assert server.extract_events_from_source(source, "Vienna", "2026-06-12") == []

  monkeypatch.setattr(server, "fetch_text", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
  assert server.extract_events_from_source(source, "Vienna", "2026-06-12") == []


def test_event_extraction_skips_sources_until_due(monkeypatch, isolated_runtime, fixture_text):
  calls = {"count": 0}

  monkeypatch.setattr(server, "discovery_sources_for_city", lambda city, limit=24: [
    {"id": "fixture-jsonld", "name": "Fixture JSON-LD", "url": "https://example.test/jsonld", "sourceKind": "official-city-calendar", "trustLevel": "high"},
  ])

  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    calls["count"] += 1
    return fixture_text("sample_jsonld_event_page.html")

  monkeypatch.setattr(server, "fetch_text", fake_fetch)

  first = server.extract_city_events("Vienna", "2026-06-12", force=True)
  calls_after_first = calls["count"]
  second = server.extract_city_events("Vienna", "2026-06-13", force=False)

  assert calls["count"] == calls_after_first
  assert calls_after_first >= 1
  assert first["sourceCount"] == 1
  assert first["insertedEventCount"] >= 1
  assert second["sourceCount"] == 0
  assert second["newlyExtractedEventCount"] == 0


def test_event_store_handles_colliding_extractor_ids(isolated_runtime):
  store = server.PipelineStore(server.PIPELINE_DB_PATH)
  events = [
    {
      "id": "event-123",
      "title": "First",
      "city": "Vienna",
      "date": "2026-06-12",
      "category": "social",
      "eventUrl": "https://example.test/events/first",
      "sourceId": "source-a",
      "sourceName": "Source A",
    },
    {
      "id": "event-123",
      "title": "Second",
      "city": "Vienna",
      "date": "2026-06-12",
      "category": "cultural",
      "eventUrl": "https://example.test/events/second",
      "sourceId": "source-a",
      "sourceName": "Source A",
    },
  ]

  result = store.upsert_events(events, city="Vienna", target_date="2026-06-12")
  stored = store.events_for_city("Vienna", target_date="2026-06-12")

  assert result["inserted"] == 2
  assert stored["eventCount"] == 2
