from __future__ import annotations

from datepipe_backend.event_quality import filter_event_leads

import server


def lead(title: str, url: str, source: str = "MAK Calendar", score: int = 50) -> dict:
  return {
    "id": f"event-{abs(hash(url)) % 10000}",
    "title": title,
    "city": "Vienna",
    "date": "2026-05-24",
    "category": server.category_from_event_text(f"{title} {url}"),
    "eventUrl": url,
    "sourceName": source,
    "sourceId": "fixture",
    "sourceKind": "official-city-calendar",
    "candidateKind": "extracted-event-lead",
    "specialnessScore": score,
    "extractionScore": score,
  }


def test_event_quality_rejects_navigation_and_share_links():
  events = [
    lead("Share", "https://www.facebook.com/share.php?u=https://www.mak.at/en/education/guided_tour_2026-05-24_1500"),
    lead("Opening hours, Tickets & prices", "https://www.jmw.at/opening-hours"),
    lead("Kids & Families", "https://www.jmw.at/en/guided_tours/kids__families"),
    lead("School Groups & More", "https://www.jmw.at/en/guided_tours/school__more"),
    lead("Calendar - MAK Museum Vienna", "https://www.mak.at/en/program/calendar_1"),
    lead("Guided Tour VIENNA 1900-Everyday. A Total Work of Art", "https://www.mak.at/en/education/guided_tour_vienna_1900everyday_a_total_work_of_art_2026-05-24_1500"),
  ]

  accepted, rejected = filter_event_leads(events)

  assert [item["title"] for item in accepted] == ["Guided Tour VIENNA 1900-Everyday. A Total Work of Art"]
  assert len(rejected) == 5
  assert {reason for item in rejected for reason in item["reasons"]} >= {"generic_title", "non_event_title", "share_or_utility_url"}


def test_event_quality_caps_duplicate_series():
  events = [
    lead("Guided Tour VIENNA 1900-Everyday. A Total Work of Art", f"https://www.mak.at/en/education/guided_tour_vienna_1900_2026-05-{day:02d}_1500")
    for day in range(24, 30)
  ]

  accepted, rejected = filter_event_leads(events, per_series_limit=3)

  assert len(accepted) == 3
  assert len(rejected) == 3
  assert all(item["reasons"] == ["duplicate_series"] for item in rejected)


def test_event_quality_caps_weekday_prefixed_series():
  events = [
    lead(f"{weekday}.{10 + index}.6. Sommerszene 2026 Same Performance 10:00– 22:00", f"https://example.test/events/sommerszene-{index}")
    for index, weekday in enumerate(["Mi", "Do", "Fr", "Sa", "So"], start=0)
  ]

  accepted, rejected = filter_event_leads(events, per_series_limit=2)

  assert len(accepted) == 2
  assert len(rejected) == 3
  assert all(item["reasons"] == ["duplicate_series"] for item in rejected)


def test_event_category_does_not_treat_markets_word_as_food():
  category = server.category_from_event_text(
    "Photo I Brut Artistic Practices Between Visual Culture and Health Museums Markets and Artists"
  )

  assert category != "culinary"


def test_event_quality_rejects_city_mismatch_past_dates_and_children():
  events = [
    lead("Neues Festival auf der Donauinsel: Das Isle of Summer Vienna Line-up", "https://example.test/vienna-festival-2026"),
    lead("15.04.2026 DIAMETRALE Film Festival 2026 Fotogalerie", "https://example.test/diametrale-2026-04-15"),
    lead("24.05.2026 Tolia Curriculum for Children Bedtime Stories", "https://example.test/children-2026-05-24"),
    lead("28.05.2026 Evening concert by the lake", "https://example.test/klagenfurt-2026-05-28"),
  ]
  for event in events:
    event["city"] = "Klagenfurt"
    event["date"] = "2026-05-26"

  accepted, rejected = filter_event_leads(events)

  assert [item["title"] for item in accepted] == ["28.05.2026 Evening concert by the lake"]
  reasons = {reason for item in rejected for reason in item["reasons"]}
  assert {"city_mismatch", "past_date", "non_event_title"} <= reasons


def test_event_quality_caps_source_daily_volume():
  events = [
    lead(f"28.05.2026 Specific Event {index}", f"https://example.test/events/{index}")
    for index in range(5)
  ]

  accepted, rejected = filter_event_leads(events, per_source_limit=3)

  assert len(accepted) == 3
  assert len(rejected) == 2
  assert all(item["reasons"] == ["source_daily_cap"] for item in rejected)


def test_event_quality_caps_official_baseline_floods_but_keeps_venue_depth():
  official = [
    lead(f"28.05.2026 Official city plaza event {index}", f"https://example.test/city/events/{index}")
    for index in range(10)
  ]
  venue = [
    lead(f"28.05.2026 Cellar jazz night {index}", f"https://example.test/jazz/events/{index}", source="Small Jazz Cellar")
    for index in range(10)
  ]
  for event in venue:
    event["sourceKind"] = "jazz-venue"
    event["sourceQualityBucket"] = "venue_primary"

  accepted, rejected = filter_event_leads(official + venue, per_source_limit=12)

  assert sum(1 for item in accepted if item["sourceName"] == "MAK Calendar") == 8
  assert sum(1 for item in accepted if item["sourceName"] == "Small Jazz Cellar") == 10
  assert any(item["reasons"] == ["source_kind_daily_cap"] for item in rejected)


def test_event_quality_requires_city_evidence_for_national_sources():
  events = [
    lead("ImPulsTanz 2026: Das erwartet euch bei Europas größtem Tanzfestival", "https://example.test/impulstanz-2026"),
    lead("28.05.2026 Konzert in Klagenfurt am Wörthersee", "https://example.test/klagenfurt-konzert-2026-05-28"),
  ]
  for event in events:
    event["city"] = "Klagenfurt"
    event["date"] = "2026-05-27"
    event["sourceId"] = "at.national.1000things"
    event["sourceScope"] = "national"

  accepted, rejected = filter_event_leads(events)

  assert [item["title"] for item in accepted] == ["28.05.2026 Konzert in Klagenfurt am Wörthersee"]
  assert "missing_city_evidence" in rejected[0]["reasons"]


def test_event_quality_rejects_page_sections_and_editorial_without_dates():
  events = [
    lead("Workshop Office at MQ Our workshop office opens in April", "https://example.test/workshops/office"),
    lead("Calendar Our full workshop schedule featuring 239 workshops", "https://example.test/calendar/workshops-schedule"),
    lead("Vienna Spanish Film Festival: Wenn Netflix-Stars ins Gartenbaukino spazieren", "https://example.test/a/spanish-film-festival-vienna"),
    lead("28.05.2026 Courtyard concert in Vienna", "https://example.test/events/courtyard-concert-2026-05-28"),
  ]
  events[2]["sourceKind"] = "editorial-activity-guide"

  accepted, rejected = filter_event_leads(events)

  assert [item["title"] for item in accepted] == ["28.05.2026 Courtyard concert in Vienna"]
  reasons = {reason for item in rejected for reason in item["reasons"]}
  assert {"page_or_section_title", "editorial_without_date", "missing_detail_page"} <= reasons


def test_event_quality_uses_research_target_date_for_extracted_occurrence():
  event = lead("Lars Eidinger - Anti Disco - Klagenfurt Festival 2026-05-24", "https://example.test/klagenfurt/lars-eidinger-2026-05-24")
  event["city"] = "Klagenfurt"
  event["date"] = "2026-05-24"
  event["targetDate"] = "2026-05-28"

  accepted, rejected = filter_event_leads([event])

  assert accepted == []
  assert rejected[0]["reasons"] == ["past_date"]


def test_event_quality_parses_german_month_name_dates():
  event = lead("22. Mai 2026 Sponsor news from a festival", "https://example.test/news/mai-2026")
  event["targetDate"] = "2026-05-28"

  accepted, rejected = filter_event_leads([event])

  assert accepted == []
  assert "past_date" in rejected[0]["reasons"]


def test_event_quality_rejects_caption_url_titles_and_region_indexes():
  events = [
    lead(
      "Tolia Astakhishvili, Tolia Curriculum, 2026, courtesy of the artist",
      "https://example.test/wien/tolia-curriculum-2026",
    ),
    lead("https://www.oeticket.com/artist/wirtschaftsball-tirol/", "https://www.oeticket.com/artist/wirtschaftsball-tirol/"),
    lead("Events in the Hall-Wattens Region", "https://example.test/hall-wattens/events-2026-06-01"),
    lead("Link zur Applied Theatre Webseite", "https://example.test/events/applied-theatre-2026-06-01"),
    lead("Culinary offers in the region. Fall Find the right culinary offer for you", "https://example.test/events/culinary-2026-06-01"),
  ]
  events[2]["city"] = "Innsbruck"

  accepted, rejected = filter_event_leads(events)

  assert accepted == []
  reasons = {reason for item in rejected for reason in item["reasons"]}
  assert "non_event_title" in reasons
