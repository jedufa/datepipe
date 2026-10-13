from __future__ import annotations

import json

from urllib.parse import urlparse

import server


def test_city_digest_returns_stable_candidate_structure(api):
  _status, digest = api.query("/api/city-digest", {"city": "Vienna", "date": "2026-06-12"}, expected=200)

  assert digest["schema"] == "datepipe.city_digest.v1"
  assert digest["city"] == "Vienna"
  assert digest["candidates"]
  candidate = digest["candidates"][0]
  assert {"id", "title", "eventUrl", "sourceName", "specialnessScore", "matchingReady"} <= set(candidate)


def test_digest_refresh_writes_manifest(api):
  _status, manifest = api.post(
    "/api/city-digests/refresh",
    {"cities": ["Vienna"], "date": "2026-06-12", "force": True, "extractEvents": False},
    expected=200,
  )

  assert manifest["schema"] == "datepipe.city_digest_manifest.v1"
  assert manifest["cityCount"] == 1
  assert manifest["errorCount"] == 0
  assert manifest["digests"][0]["city"] == "Vienna"


def test_discovery_sources_are_available_without_network(api):
  _status, payload = api.query("/api/discovery-sources", {"city": "Vienna"}, expected=200)

  assert payload["schema"] == "datepipe.discovery_sources_for_city.v1"
  assert payload["sources"]
  assert all(item.get("verificationRule") for item in payload["sources"])


def test_demo_source_catalog_is_scoped_and_never_points_at_real_venues():
  catalog = server.load_discovery_source_catalog()
  assert catalog["synthetic"] is True
  for source in catalog["sources"]:
    parsed = urlparse(source["url"])
    assert parsed.scheme == "https"
    assert parsed.hostname.endswith(".example.invalid")
    assert parsed.path.strip("/")
    assert source["verificationRule"]
    assert source["name"].startswith("Fictional")


def test_fictional_city_packs_retain_venue_and_editorial_variety(api):
  for city in ("Vienna", "Graz", "Linz", "Salzburg", "Innsbruck"):
    _status, payload = api.query("/api/discovery-sources", {"city": city}, expected=200)
    sources = payload["sources"]
    assert len({urlparse(item["url"]).hostname for item in sources}) >= 3
    buckets = {item["sourceQualityBucket"] for item in sources}
    assert {"venue_primary", "local_curated"} <= buckets
    assert len({item["id"] for item in sources}) == len(sources)


def test_discovery_sources_do_not_promote_regional_city_pack_sources(api):
  _status, payload = api.query("/api/discovery-sources", {"city": "Innsbruck"}, expected=200)
  ids = {item["id"] for item in payload["sources"]}

  assert "city-pack.at.innsbruck.telfs-calendar" not in ids
  assert "city-pack.at.innsbruck.hall-wattens-calendar" not in ids


def test_city_source_status_requires_ready_local_source_mix(monkeypatch):
  monkeypatch.setattr(server, "discovery_sources_for_city", lambda city, limit=24: [
    {
      "id": "thin-official",
      "name": "Thin Official Calendar",
      "scope": "city",
      "cities": [city],
      "sourceKind": "official-city-calendar",
      "trustLevel": "high",
      "url": "https://example.test/events",
    }
  ])

  status = server.city_source_status("Bregenz")

  assert status["known"] is False
  assert status["localSourceCount"] == 1
  assert status["sourceReadinessStatus"] == "too_few_validated_sources"


def test_unknown_city_starts_city_research_instead_of_fake_suggestions(api):
  _status, payload = api.post(
    "/api/generate",
    {"searchDate": "2026-06-12", "profile": {"homeBase": "Bregenz"}},
    expected=202,
  )

  assert payload["options"] == []
  assert payload["source"] == "city-research:source_validation_failed"
  assert payload["cityResearch"]["city"] == "Bregenz"
  assert payload["cityResearch"]["remainingSeconds"] > 0
  assert payload["cityResearch"]["sourceStatus"]["known"] is False
  assert payload["cityResearch"]["provider"] == "local-web-scout"
  assert payload["cityResearch"]["status"] == "source_validation_failed"

  _status, status = api.query("/api/city-research", {"city": "Bregenz"}, expected=200)
  assert status["sourceStatus"]["known"] is False
  assert status["job"]["id"] == payload["cityResearch"]["id"]


def test_unknown_city_does_not_start_deep_research_without_explicit_opt_in(isolated_runtime, monkeypatch):
  monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
  monkeypatch.setattr(server, "preflight_openai_key", lambda api_key: (_ for _ in ()).throw(AssertionError("preflight should not run")))

  job = server.start_city_research("Bregenz", "2026-06-12", {}, force=True)
  public = server.public_city_research_job(job)

  assert job["provider"] == "local-web-scout"
  assert job["status"] == "source_validation_failed"
  assert "responseId" not in job
  assert public["costPolicy"]["deepResearchEnabled"] is False
  assert public["costPolicy"]["maxOutputTokens"] == 2500


def test_unknown_city_local_scout_promotes_validated_sources(isolated_runtime, monkeypatch, tmp_path):
  catalog_path = tmp_path / "data" / "discovery_sources.json"
  catalog_path.parent.mkdir(parents=True, exist_ok=True)
  catalog_path.write_text(json.dumps({"schema": "datepipe.discovery_sources.v1", "sources": []}), encoding="utf-8")
  monkeypatch.setattr(server, "DISCOVERY_SOURCES_PATH", catalog_path)

  def fake_search(query: str, limit: int = 8):
    return [
      {"title": "Bregenz Jazz Club Programm", "url": "https://example.test/bregenz/jazz-programm"},
      {"title": "Bregenz Atelier Workshop Kalender", "url": "https://example.test/bregenz/workshop-kalender"},
      {"title": "Bregenz Datenschutz", "url": "https://example.test/datenschutz"},
    ]

  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    return (
      "<html><title>Bregenz Programm</title><body>"
      "Bregenz Veranstaltungen Programm Kalender 2026 Konzert Jazz Workshop Tickets Buchung "
      "Ausstellung Kino Theater current event details address registration city centre."
      "</body></html>"
    )

  def fake_extract(city: str, target_date: str | None = None, force: bool = False, max_sources: int = 48, max_events: int = 360):
    return {
      "schema": "datepipe.event_extraction.v1",
      "city": city,
      "date": target_date,
      "qualityGateStatus": "thin_results",
      "eventCount": 1,
      "events": [],
    }

  monkeypatch.setattr(server, "search_duckduckgo_results", fake_search)
  monkeypatch.setattr(server, "fetch_text", fake_fetch)
  monkeypatch.setattr(server, "extract_city_events", fake_extract)

  job = server.start_city_research("Bregenz", "2026-06-12", {}, force=True)

  assert job["provider"] == "local-web-scout"
  assert job["status"] == "ready"
  assert job["integration"]["stagedSourceCount"] == 2
  assert job["integration"]["validatedSourceCount"] == 2
  assert job["integration"]["promotedSourceCount"] == 2
  assert server.discovery_sources_for_city("Bregenz", limit=1)[0]["researchProvenance"] == "local-web-scout"
  assert server.city_has_local_source("Bregenz") is True


def test_deep_research_opt_in_falls_back_to_local_scout_when_key_is_missing(isolated_runtime, monkeypatch):
  monkeypatch.setenv("DATEPIPE_ENABLE_DEEP_RESEARCH", "1")
  monkeypatch.delenv("OPENAI_API_KEY", raising=False)

  job = server.start_city_research("Bregenz", "2026-06-12", {}, force=True)

  assert job["provider"] == "local-web-scout"
  assert job["status"] == "source_validation_failed"
  assert job["deepResearchFallbackReason"] == "missing"
  assert "responseId" not in job
  assert job["costPolicy"]["deepResearchEnabled"] is True


def test_local_city_scout_caps_duplicate_hosts_and_rejects_travel_directories(monkeypatch):
  def fake_search(query: str, limit: int = 8):
    return [
      {"title": "Bregenz Venue One", "url": "https://same.example.test/bregenz/events/one"},
      {"title": "Bregenz Venue Two", "url": "https://same.example.test/bregenz/events/two"},
      {"title": "Bregenz Venue Three", "url": "https://same.example.test/bregenz/events/three"},
      {"title": "Bregenz Tripadvisor Things To Do", "url": "https://www.tripadvisor.com/Attractions-g190428-Bregenz.html"},
      {"title": "Bregenz Workshop Calendar", "url": "https://workshop.example.test/bregenz/calendar"},
      {"title": "Bregenz Jazz Program", "url": "https://jazz.example.test/bregenz/program"},
    ]

  monkeypatch.setattr(server, "search_duckduckgo_results", fake_search)

  report = server.local_city_scout_report("Bregenz", "2026-06-12", {})
  urls = [source["url"] for source in report["selectedSources"] + report["candidateSources"]]
  rejection_reasons = {row["rejectionReason"] for row in report["rejectedSources"]}

  assert sum(1 for url in urls if server.source_host_key(url) == "same.example.test") == 2
  assert "duplicate_host_cap" in rejection_reasons
  assert "generic_travel_directory" in rejection_reasons


def test_deep_research_opt_in_uses_configured_output_cap(isolated_runtime, monkeypatch):
  captured = {}

  class FakeResponse:
    status = 200

    def __enter__(self):
      return self

    def __exit__(self, exc_type, exc, tb):
      return False

    def read(self):
      return json.dumps({"id": "resp_city_123", "status": "in_progress"}).encode("utf-8")

  def fake_urlopen(request, timeout=0):
    captured["payload"] = json.loads(request.data.decode("utf-8"))
    captured["timeout"] = timeout
    return FakeResponse()

  monkeypatch.setenv("DATEPIPE_ENABLE_DEEP_RESEARCH", "1")
  monkeypatch.setenv("DATEPIPE_DEEP_RESEARCH_MAX_OUTPUT_TOKENS", "1200")
  monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
  monkeypatch.setattr(server, "preflight_openai_key", lambda api_key: (True, "ok"))
  monkeypatch.setattr(server, "urlopen", fake_urlopen)

  job = server.start_city_research("Bregenz", "2026-06-12", {}, force=True)

  assert job["provider"] == "openai-deep-research"
  assert job["responseId"] == "resp_city_123"
  assert captured["payload"]["max_output_tokens"] == 1200
  assert job["costPolicy"]["deepResearchEnabled"] is True
  assert job["costPolicy"]["maxOutputTokens"] == 1200


def test_completed_city_research_promotes_valid_sources_and_runs_extraction(isolated_runtime, monkeypatch, tmp_path):
  catalog_path = tmp_path / "data" / "discovery_sources.json"
  catalog_path.parent.mkdir(parents=True, exist_ok=True)
  catalog_path.write_text(json.dumps({"schema": "datepipe.discovery_sources.v1", "sources": []}), encoding="utf-8")
  monkeypatch.setattr(server, "DISCOVERY_SOURCES_PATH", catalog_path)

  report = {
    "selectedSources": [
      {
        "id": "research.bregenz.jazzclub",
        "name": "Bregenz Jazz Keller",
        "url": "https://example.test/bregenz/jazz",
        "sourceType": "jazz-venue",
        "locationScope": "city-core",
        "confidence": "high",
        "privacyFit": "high",
        "categories": ["music-venue", "quiet-intimate"],
        "evidenceSummary": "Bregenz city-core venue calendar with current concerts.",
        "liveVerificationRule": "Verify concert date, door time, and booking before reveal.",
      },
      {
        "id": "research.bregenz.workshop",
        "name": "Bregenz Werkstatt Abende",
        "url": "https://example.test/bregenz/workshops",
        "sourceType": "workshop-provider",
        "locationScope": "city-core",
        "confidence": "medium",
        "privacyFit": "medium",
        "categories": ["workshops", "different"],
        "evidenceSummary": "Bregenz workshop calendar for small evening classes.",
        "liveVerificationRule": "Verify registration and group size before reveal.",
      },
    ],
    "candidateSources": [
      {
        "name": "Broken Bregenz Source",
        "url": "https://example.test/bregenz/broken",
        "sourceType": "event-platform",
      }
    ],
    "rejectedSources": [{"name": "National homepage", "rejectionReason": "too generic"}],
    "qualityNotes": ["Prefer primary venue calendars over city homepages."],
  }

  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    if "broken" in url:
      raise RuntimeError("offline")
    return (
      "<html><title>Bregenz events</title><body>"
      "Bregenz calendar with concerts, workshops, tickets, booking details, city-core venues, evening culture, "
      "small groups, registration notes, address details, current programme listings, and organiser contacts."
      "</body></html>"
    )

  def fake_extract(city: str, target_date: str | None = None, force: bool = False, max_sources: int = 48, max_events: int = 360):
    return {
      "schema": "datepipe.event_extraction.v1",
      "city": city,
      "date": target_date,
      "qualityGateStatus": "thin_results",
      "eventCount": 1,
      "events": [
        {
          "id": "event-bregenz-jazz",
          "title": "Late Jazz Listening Room",
          "city": city,
          "date": target_date,
          "targetDate": target_date,
          "category": "social",
          "eventUrl": "https://example.test/bregenz/jazz/late",
          "sourceId": "research.bregenz.jazzclub",
          "sourceName": "Bregenz Jazz Keller",
          "sourceQualityBucket": "venue_primary",
          "specialnessScore": 62,
        }
      ],
    }

  monkeypatch.setattr(server, "fetch_text", fake_fetch)
  monkeypatch.setattr(server, "extract_city_events", fake_extract)
  monkeypatch.setattr(server, "resolve_page_image", lambda *args, **kwargs: "https://example.test/image.jpg")

  job = {
    "schema": "datepipe.city_research_job.v1",
    "id": "city-research-test",
    "city": "Bregenz",
    "targetDate": "2026-06-12",
    "status": "completed",
    "provider": "openai-deep-research",
    "startedAt": "2026-05-24T10:00:00Z",
    "deadlineAt": "2026-05-24T11:00:00Z",
    "completedAt": "2026-05-24T10:20:00Z",
    "report": json.dumps(report),
  }

  integrated = server.process_completed_city_research_job(job)

  assert integrated["status"] == "ready"
  assert integrated["qualityGateStatus"] == "thin_results"
  assert integrated["integration"]["stagedSourceCount"] == 3
  assert integrated["integration"]["validatedSourceCount"] == 2
  assert integrated["integration"]["rejectedSourceCount"] == 1
  assert integrated["integration"]["promotedSourceCount"] == 2
  assert integrated["integration"]["eventExtraction"]["imageResolvedCount"] == 1
  assert server.city_has_local_source("Bregenz") is True
  assert server.discovery_sources_for_city("Bregenz", limit=4)[0]["sourceQualityBucket"] == "venue_primary"


def test_completed_city_research_needs_review_for_weak_source_mix(isolated_runtime, monkeypatch, tmp_path):
  catalog_path = tmp_path / "data" / "discovery_sources.json"
  catalog_path.parent.mkdir(parents=True, exist_ok=True)
  catalog_path.write_text(json.dumps({"schema": "datepipe.discovery_sources.v1", "sources": []}), encoding="utf-8")
  monkeypatch.setattr(server, "DISCOVERY_SOURCES_PATH", catalog_path)

  report = {
    "selectedSources": [
      {
        "id": "research.bregenz.official",
        "name": "Bregenz Official Events",
        "url": "https://example.test/bregenz/events",
        "sourceType": "official-city-calendar",
        "locationScope": "city-core",
        "confidence": "high",
        "categories": ["events"],
      },
      {
        "id": "research.bregenz.tickets",
        "name": "Bregenz Tickets",
        "url": "https://example.test/bregenz/tickets",
        "sourceType": "event-platform",
        "locationScope": "city-core",
        "confidence": "medium",
        "categories": ["tickets"],
      },
    ],
  }

  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    return (
      "<html><body>"
      "Bregenz Veranstaltungen Programm Kalender 2026 Tickets Booking Concert Theater events city centre. "
      "Current programme pages include dates, times, ticket links, booking notes, venue addresses, organiser details, "
      "and evening listings for the city calendar so the validation pass has enough page content to inspect."
      "<a href='/bregenz/events/jazz-night-2026-06-12'>Jazz Night 12.06.2026</a>"
      "<a href='/bregenz/events/theater-evening-2026-06-13'>Theater Evening 13.06.2026</a>"
      "</body></html>"
    )

  monkeypatch.setattr(server, "fetch_text", fake_fetch)
  monkeypatch.setattr(server, "extract_city_events", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("weak source mix should not extract events")))

  job = {
    "schema": "datepipe.city_research_job.v1",
    "id": "city-research-weak-mix",
    "city": "Bregenz",
    "targetDate": "2026-06-12",
    "status": "completed",
    "provider": "local-web-scout",
    "startedAt": "2026-05-24T10:00:00Z",
    "deadlineAt": "2026-05-24T11:00:00Z",
    "completedAt": "2026-05-24T10:20:00Z",
    "report": json.dumps(report),
  }

  integrated = server.process_completed_city_research_job(job)

  assert integrated["status"] == "ready_needs_review"
  assert integrated["qualityGateStatus"] == "no_private_value_sources"
  assert integrated["integration"]["promotedSourceCount"] == 2
  assert integrated["integration"]["sourceReadinessStatus"] == "no_private_value_sources"
  assert integrated["integration"]["eventExtraction"]["qualityGateStatus"] == "not_run"


def test_collected_deep_research_tsv_report_parses_sources(isolated_runtime):
  report = """
Feldbach DatePipe Source Knowledge Pack

Final CSV-ready table

source_name	source_type	official_url	owner_or_operator	geographic_scope	why_relevant_for_datepipe	recurring_or_durable_pattern	freshness_signal	live_verification_need	trust_level	risk_notes	suggested_datepipe_tags	extraction_notes	whether_it_should_be_included_in_MVP_city_pack	evidence
Stadtgemeinde Feldbach Veranstaltungen	official_city_event_calendar	https://feldbach.gv.at/veranstaltungen/	Stadtgemeinde Feldbach	Feldbach	first-party current event discovery	rolling official event/news feed	upcoming dated city items	medium	primary	mixed civic + leisure categories	official_calendar;citywide;events	crawl list and detail pages	yes	1
Feldbach Gemeindezeitung and Lebenskultur	municipal_magazine_archive	https://feldbach.gv.at/category/gemeindezeitung/	Stadtgemeinde Feldbach	Feldbach	context source for recurring campaigns	ongoing archive	2026 issues in archive	low	curated_secondary	editorial, not normalized	local_magazine;editorial_context	use for historical pattern mining only	maybe	14
Rejected Legacy Cinema	legacy_cinema	https://example.test/closed	Legacy	Feldbach	not durable	closed	no fresh signal	high	weak	closed	cinema	reject	no	99
"""

  staging = server.parse_city_research_sources(report, "Feldbach")

  assert staging["parseStatus"] == "parsed"
  assert staging["selectedSourceCount"] == 2
  assert [source["name"] for source in staging["sources"]] == [
    "Stadtgemeinde Feldbach Veranstaltungen",
    "Feldbach Gemeindezeitung and Lebenskultur",
  ]
  assert staging["sources"][0]["trustLevel"] == "high"
  assert staging["sources"][0]["bestFor"] == ["official_calendar", "citywide", "events"]
  assert staging["sources"][1]["trustLevel"] == "medium"
  assert staging["qualityNotes"][0] == "Parsed from collected Deep Research final CSV-ready table."


def test_city_research_validation_rejects_generic_calendar_without_detail_links(monkeypatch):
  source = {
    "id": "research.bregenz.official",
    "name": "Bregenz Official Events",
    "url": "https://example.test/bregenz/events",
    "scope": "city",
    "cities": ["Bregenz"],
    "sourceKind": "official-city-calendar",
    "trustLevel": "high",
    "bestFor": ["events"],
  }

  monkeypatch.setattr(server, "fetch_text", lambda *args, **kwargs: (
    "<html><body>"
    "Bregenz Veranstaltungen Programm Kalender 2026 Tickets Booking Concert Theater events city centre. "
    "This page describes the current event calendar, booking flow, venue address details, organiser contacts, "
    "and city programme coverage, but it exposes no concrete event-detail links."
    "</body></html>"
  ))

  validation = server.validate_city_research_sources("Bregenz", [source], "2026-06-12")

  assert validation["validatedCount"] == 0
  assert validation["rejectedCount"] == 1
  assert validation["rejectedSources"][0]["rejectionReason"] == "no_event_detail_links"
