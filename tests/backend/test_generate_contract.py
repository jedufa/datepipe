from __future__ import annotations

import json
from pathlib import Path


def test_generate_uses_deterministic_fallback_without_keys(api):
  body = json.loads((Path("tests/fixtures/sample_generate_request.json")).read_text(encoding="utf-8"))

  _status, payload = api.post("/api/generate", body, expected=200)

  assert payload["source"] in {"local", "openai:missing-key", "openai:missing"}
  assert len(payload["options"]) >= 1
  option = payload["options"][0]
  assert {"title", "category", "eventUrl", "visibleHint"} <= set(option)


def test_generate_can_run_with_authenticated_scoped_profile(api, registered):
  body = {"homeBase": "Vienna", "budgetComfort": "casual"}
  api.post("/api/settings", body, token=registered["sessionToken"], expected=200)

  _status, payload = api.post("/api/generate", {"mood": "quiet art", "searchDate": "2026-06-12"}, token=registered["sessionToken"], expected=200)

  assert payload["options"]
  assert "options" in payload


def test_generate_backfills_when_ai_returns_too_few_options(api, monkeypatch):
  import server

  monkeypatch.setattr(
    server,
    "generate_ai_options",
    lambda *args, **kwargs: ([
      {
        "id": "proposal-ai-one",
        "sourceIdeaId": "ai-artnight-one",
        "slotId": "slot-2026-06-12",
        "title": "ArtNight with drinks after",
        "score": 94,
        "mode": "solo-secret",
        "category": "cultural",
        "duration": "normal evening",
        "timing": "precise",
        "stretch": "safe-stretch",
        "why": "A concrete creative evening that fits a quiet art request and keeps the reveal easy to manage.",
        "eventUrl": "https://www.artnight.com/wien",
        "imageUrl": "",
        "sourceName": "ArtNight Wien",
        "variety": "fresh",
        "visibleHint": "Creative date. I will handle the details.",
        "steps": ["Check a current slot", "Book two seats", "Reserve a nearby drink"],
        "todos": [{"id": "todo-ai-one", "text": "Check current availability", "done": False}],
        "backup": "Pick one nearby low-effort fallback before departure.",
        "dateChoice": "2026-06-12",
        "dateLabel": "Fri 06-12",
      }
    ], "openai-web"),
  )

  _status, payload = api.post(
    "/api/generate",
    {
      "searchDate": "2026-06-12",
      "mood": "quiet art",
      "profile": {"homeBase": "Vienna", "budgetComfort": "casual", "searchKeywords": ["art"]},
    },
    expected=200,
  )

  assert payload["source"] == "openai-web+local-backfill"
  assert len(payload["options"]) >= server.GENERATE_MIN_RELEASE_OPTIONS
  assert payload["options"][0]["title"] == "ArtNight with drinks after"
  assert "too_few_options" not in payload["qualityReport"]["flags"]
