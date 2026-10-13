from __future__ import annotations

import json
from pathlib import Path


def test_couple_memory_persists_user_added_and_profile_derived_memory(api, registered):
  memory = json.loads(Path("tests/fixtures/sample_couple_memory.json").read_text(encoding="utf-8"))
  api.post("/api/settings", {"likes": ["art"], "avoid": ["clubs"], "coupleVibe": "quiet and curious"}, token=registered["sessionToken"], expected=200)
  api.post("/api/couple-memory", memory, token=registered["sessionToken"], expected=201)

  _status, payload = api.get("/api/couple-memory", token=registered["sessionToken"], expected=200)
  texts = " ".join(item["text"] for item in payload["memories"])

  assert "quiet surprises" in texts
  assert "Avoid clubs" in texts
  assert "Together they are" in texts
