from __future__ import annotations

from pathlib import Path


def test_static_shell_references_app_and_styles():
  html = Path("static/index.html").read_text(encoding="utf-8")

  assert "styles.css" in html
  assert "app.js" in html
  assert "DatePipe" in html
  assert 'name="color-scheme" content="light dark"' in html


def test_web_prototype_contains_core_api_paths():
  app = Path("static/app.js").read_text(encoding="utf-8")

  for path in ("/api/state", "/api/settings", "/api/generate", "/api/research-review", "/api/developer/weekly-event-research/latest"):
    assert path in app


def test_review_queue_has_phone_first_filters():
  app = Path("static/app.js").read_text(encoding="utf-8")
  css = Path("static/styles.css").read_text(encoding="utf-8")

  assert "reviewFilters" in app
  assert "qualityReportMarkup" in app
  assert "no-user quality meter" in app
  assert "data-review-filter" in app
  assert "filteredReviewSources" in app
  assert ".review-filterbar" in css
  assert ".quality-card" in css


def test_release_ui_uses_calendar_weeks_and_conflict_signals():
  html = Path("static/index.html").read_text(encoding="utf-8")
  app = Path("static/app.js").read_text(encoding="utf-8")
  css = Path("static/styles.css").read_text(encoding="utf-8")

  assert "Pick a calendar week" in html
  assert "Product score" in html
  assert "upcomingWeeks" in app
  assert "weekConflictSummary" in app
  assert "scheduleWarnings" in app
  assert "/api/developer/release-evaluation" in app
  assert "renderReleaseEvaluation" in app
  assert "releaseGate" in app
  assert "blockingIssues" in app
  assert "readyForRelease" in app
  assert ".weekend-card.has-conflict" in css
  assert ".warning-line" in css
  assert ".success-line" in css
  assert ".release-issue" in css
  assert ".release-row" in css


def test_weekly_research_ui_shows_latest_report_status():
  html = Path("static/index.html").read_text(encoding="utf-8")
  app = Path("static/app.js").read_text(encoding="utf-8")
  css = Path("static/styles.css").read_text(encoding="utf-8")

  assert "Weekly research" in html
  assert "weeklyResearchPanel" in html
  assert "loadWeeklyEventResearch" in app
  assert "renderWeeklyEventResearch" in app
  assert "weakCities" in app
  assert ".weekly-row" in css
  assert ".weekly-row.needs-review" in css


def test_dark_theme_uses_real_dark_surfaces():
  css = Path("static/styles.css").read_text(encoding="utf-8")

  assert "@media (prefers-color-scheme: dark)" in css
  assert "--bg: #12100d" in css
  assert "--surface: #211b15" in css
  assert "--surface-glass: rgba(33, 27, 21, 0.92)" in css
  assert "background: var(--sticky-bg)" in css
  assert "background: var(--surface-glass)" in css
  assert "background: var(--answer-bg)" in css
  assert "color: var(--answer-ink)" in css
