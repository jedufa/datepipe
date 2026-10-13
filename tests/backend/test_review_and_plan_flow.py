from __future__ import annotations

import server
from datepipe_backend.pipeline import PipelineStore


def test_research_review_endpoint_exposes_sources_events_and_updates(api):
  store = PipelineStore(server.PIPELINE_DB_PATH)
  store.register_sources("Vienna", [{
    "id": "source.vienna.cellar",
    "name": "Vienna Cellar",
    "url": "https://example.test/cellar/events",
    "sourceKind": "jazz-venue",
    "trustLevel": "high",
    "sourceQualityBucket": "venue_primary",
  }])
  store.upsert_events([{
    "title": "Cellar Jazz Night",
    "city": "Vienna",
    "category": "cultural",
    "eventUrl": "https://example.test/cellar/events/jazz-night",
    "sourceId": "source.vienna.cellar",
    "sourceName": "Vienna Cellar",
    "targetDate": "2026-06-12",
    "specialnessScore": 80,
    "extractionScore": 70,
  }], city="Vienna", target_date="2026-06-12")

  _status, review = api.query("/api/research-review", {"city": "Vienna", "date": "2026-06-12"}, expected=200)

  assert review["schema"] == "datepipe.research_review.v1"
  assert review["qualityReport"]["schema"] == "datepipe.quality_report.v1"
  assert review["qualityReport"]["scope"] == "research"
  assert review["qualityReport"]["score"] > 0
  assert review["sources"][0]["id"] == "source.vienna.cellar"
  assert review["events"][0]["researchState"]["reviewStatus"] == "new"

  event_id = review["events"][0]["researchState"]["id"]
  _status, updated_event = api.request(
    "PATCH",
    f"/api/research/events/{event_id}",
    {"reviewStatus": "approved"},
    expected=200,
  )
  assert updated_event["event"]["reviewStatus"] == "approved"

  _status, updated_source = api.request(
    "PATCH",
    "/api/research/sources/source.vienna.cellar",
    {"reviewStatus": "approved"},
    expected=200,
  )
  assert updated_source["source"]["reviewStatus"] == "approved"


def test_quality_report_endpoint_and_generate_payload_do_not_need_user_feedback(api):
  _status, report = api.query("/api/quality-report", {"city": "Vienna", "date": "2026-06-12"}, expected=200)
  assert report["schema"] == "datepipe.quality_report.v1"
  assert report["scope"] == "research"
  assert "dimensions" in report

  _status, catalog = api.query("/api/quality-report", {"city": "Vienna", "scope": "catalog"}, expected=200)
  assert catalog["scope"] == "catalog"
  assert catalog["knownOptionCount"] > 0

  _status, planning = api.query("/api/quality-report", {"scope": "planning"}, expected=200)
  assert planning["scope"] == "planning_readiness"
  assert "same_week_guardrail" in {item["name"] for item in planning["dimensions"]}

  _status, payload = api.post("/api/generate", {"searchDate": "2026-06-12", "profile": {"homeBase": "Vienna"}}, expected=200)
  assert payload["qualityReport"]["schema"] == "datepipe.quality_report.v1"
  assert payload["qualityReport"]["scope"] == "suggestions"
  assert payload["qualityReport"]["optionCount"] == len(payload["options"])
  assert "flags" in payload["qualityReport"]


def test_plan_from_proposal_includes_itinerary_accomplice_and_fallback(api):
  proposal = {
    "id": "proposal-test",
    "slotId": "slot-1",
    "title": "Hidden courtyard concert",
    "mode": "solo-secret",
    "dateChoice": "2026-06-12",
    "duration": "normal evening",
    "timing": "precise",
    "visibleHint": "Wear something easy.",
    "why": "Small venue, current listing, good surprise fit.",
    "eventUrl": "https://example.test/concert",
    "sourceName": "Venue",
  }

  _status, plan = api.post("/api/plans", {"proposal": proposal}, expected=201)

  assert plan["itinerary"][1]["kind"] == "main"
  assert plan["accompliceBrief"]["enabled"] is True
  assert plan["weatherFallback"]
  assert len(plan["todos"]) >= 5


def test_plan_creation_warns_about_same_week_conflicts(api):
  first = {
    "id": "proposal-first",
    "slotId": "slot-1",
    "title": "Tuesday gallery",
    "dateChoice": "2026-09-08",
    "eventUrl": "https://example.test/gallery",
  }
  second = {
    "id": "proposal-second",
    "slotId": "slot-1",
    "title": "Saturday concert",
    "dateChoice": "2026-09-12",
    "eventUrl": "https://example.test/concert",
  }

  _status, first_plan = api.post("/api/plans", {"proposal": first}, expected=201)
  _status, second_plan = api.post("/api/plans", {"proposal": second}, expected=201)

  assert first_plan["scheduleWarnings"] == []
  assert second_plan["scheduleWarnings"][0]["type"] == "same_week_plan"
  assert second_plan["scheduleWarnings"][0]["title"] == "Tuesday gallery"


def test_release_evaluation_scores_city_week_matrix(api):
  _status, payload = api.query(
    "/api/developer/release-evaluation",
    {"cities": "Vienna,Graz", "weeks": "2"},
    expected=200,
  )

  assert payload["schema"] == "datepipe.release_evaluation.v1"
  assert payload["scope"] == "city_week_matrix"
  assert payload["caseCount"] == 4
  assert payload["rows"][0]["weekStart"] <= payload["rows"][0]["weekEnd"]
  assert "suggestionScore" in payload["rows"][0]
  assert "catalogScore" in payload["rows"][0]
  assert "retrievalScore" in payload["rows"][0]
  assert "planningScore" in payload["rows"][0]
  assert payload["components"]["planning"]["scope"] == "planning_readiness"
  assert payload["releaseGate"] in {"ready", "needs_attention", "blocked"}
  assert isinstance(payload["readyForRelease"], bool)
  assert "blockingIssues" in payload


def test_release_gate_promotes_actionable_blockers():
  issues = server.release_blocking_issues(
    [{
      "city": "Teststadt",
      "weekStart": "2026-06-08",
      "weekEnd": "2026-06-14",
      "label": "Jun 8-14",
      "score": 92,
      "flags": ["weak_provenance", "thin_event_leads"],
    }],
    {"score": 100, "flags": []},
  )

  assert issues[0]["code"] == "weak_provenance"
  assert issues[0]["severity"] == "blocker"
  assert "source links" in issues[0]["action"]
  assert server.release_gate_status(92, issues) == "blocked"
  assert server.release_gate_status(92, [issue for issue in issues if issue["severity"] != "blocker"]) == "needs_attention"
  assert server.release_gate_status(88, []) == "ready"


def test_release_evaluation_counts_research_events_across_calendar_week(api, monkeypatch):
  class FixedDate(server.date):
    @classmethod
    def today(cls):
      return cls(2026, 5, 25)

  monkeypatch.setattr(server, "date", FixedDate)
  store = PipelineStore(server.PIPELINE_DB_PATH)
  store.register_sources("Graz", [
    {
      "id": "source.graz.weekly",
      "name": "Graz Weekly",
      "url": "https://example.test/graz/events",
      "sourceKind": "venue-calendar",
      "trustLevel": "high",
      "sourceQualityBucket": "venue_primary",
    },
    {
      "id": "source.graz.workshop",
      "name": "Graz Workshop",
      "url": "https://example.test/graz/workshops",
      "sourceKind": "workshop-provider",
      "trustLevel": "high",
      "sourceQualityBucket": "venue_primary",
    },
  ])
  for index in range(3):
    store.upsert_events([{
      "title": f"Graz courtyard session {index}",
      "city": "Graz",
      "category": "cultural",
      "eventUrl": f"https://example.test/graz/events/session-{index}",
      "sourceId": "source.graz.weekly",
      "sourceName": "Graz Weekly",
      "targetDate": "2026-05-26",
      "specialnessScore": 70,
      "extractionScore": 70,
    }], city="Graz", target_date="2026-05-26")

  report = server.release_evaluation_report(server.load_state(), cities=["Graz"], weeks=1)

  assert report["rows"][0]["weekStart"] == "2026-05-25"
  assert report["rows"][0]["weekEnd"] == "2026-05-31"
  assert report["rows"][0]["source"] == "local-release-evaluation"
  assert "thin_event_leads" not in report["rows"][0]["flags"]
  assert report["rows"][0]["researchScore"] >= 60


def test_release_evaluation_target_date_prefers_remaining_weekend():
  monday = server.date.fromisoformat("2026-05-18")
  sunday = server.date.fromisoformat("2026-05-24")

  assert server.release_evaluation_target_date(server.date.fromisoformat("2026-05-18"), monday, sunday).isoformat() == "2026-05-22"
  assert server.release_evaluation_target_date(server.date.fromisoformat("2026-05-23"), monday, sunday).isoformat() == "2026-05-23"
  assert server.release_evaluation_target_date(server.date.fromisoformat("2026-05-24"), monday, sunday).isoformat() == "2026-05-24"
