from __future__ import annotations


def test_pipeline_run_records_digest_manifest(api):
  _status, manifest = api.post(
    "/api/developer/pipeline/run",
    {"cities": ["Vienna"], "date": "2026-06-12", "force": True, "extractEvents": False},
    expected=200,
  )
  _status, status = api.get("/api/developer/pipeline/status", expected=200)

  assert manifest["pipelineRun"]["status"] == "succeeded"
  assert manifest["pipelineRun"]["cities"] == ["Vienna"]
  assert status["runCount"] == 1
  assert status["runs"][0]["targetDate"] == "2026-06-12"
  assert status["runs"][0]["cities"] == ["Vienna"]

