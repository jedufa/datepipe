from __future__ import annotations

import importlib.util
from pathlib import Path

import server


SCRIPT_PATH = Path("developer/tools/run_weekly_event_research.py")


def load_weekly_tool():
  spec = importlib.util.spec_from_file_location("datepipe_weekly_event_research", SCRIPT_PATH)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_weekly_event_research_report_and_latest_endpoint(api, monkeypatch, fixture_text):
  weekly = load_weekly_tool()

  monkeypatch.setattr(server, "discovery_sources_for_city", lambda city, limit=48: [
    {
      "id": "source.vienna.venue",
      "name": "Vienna Venue",
      "url": "https://example.test/jsonld",
      "scope": "city",
      "cities": [city],
      "sourceKind": "venue-calendar",
      "trustLevel": "high",
      "sourceQualityBucket": "venue_primary",
    },
    {
      "id": "source.vienna.editorial",
      "name": "Vienna Editorial",
      "url": "https://example.test/links",
      "scope": "city",
      "cities": [city],
      "sourceKind": "local-editorial-guide",
      "trustLevel": "medium",
      "sourceQualityBucket": "local_curated",
    },
  ])

  def fake_fetch(url: str, timeout: float = 4.0) -> str:
    if "jsonld" in url:
      return fixture_text("sample_jsonld_event_page.html")
    return fixture_text("sample_links_page.html")

  monkeypatch.setattr(server, "fetch_text", fake_fetch)

  report = weekly.build_report(["Vienna"], 1, force=True, anchor=server.date.fromisoformat("2026-06-09"))
  json_path, markdown_path, latest_path = weekly.write_reports(report, server.WEEKLY_REPORT_DIR)

  assert report["schema"] == "datepipe.weekly_event_research_report.v1"
  assert report["cityCount"] == 1
  assert report["weekCount"] == 1
  assert report["rows"][0]["city"] == "Vienna"
  assert report["rows"][0]["acceptedEventCount"] >= 1
  assert report["sourceSummary"]["citySources"][0]["sourceCount"] == 2
  assert json_path.exists()
  assert markdown_path.exists()
  assert latest_path.exists()

  _status, latest = api.get("/api/developer/weekly-event-research/latest", expected=200)

  assert latest["available"] is True
  assert latest["schema"] == "datepipe.weekly_event_research_report.v1"
  assert latest["rows"][0]["city"] == "Vienna"
  assert "markdownPath" in latest


def test_latest_weekly_event_research_endpoint_reports_missing(api):
  _status, payload = api.get("/api/developer/weekly-event-research/latest", expected=200)

  assert payload["available"] is False
  assert payload["status"] == "missing_report"
