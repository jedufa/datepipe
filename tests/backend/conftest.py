from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

import server as datepipe_server

datepipe_server._pytest_original_resolve_page_image = datepipe_server.resolve_page_image


@pytest.fixture()
def isolated_runtime(tmp_path, monkeypatch):
  monkeypatch.setattr(datepipe_server, "STATE_PATH", tmp_path / "runtime" / "state.json")
  monkeypatch.setattr(datepipe_server, "CITY_DIGEST_DIR", tmp_path / "runtime" / "city_digests")
  monkeypatch.setattr(datepipe_server, "EVENT_EXTRACTION_DIR", tmp_path / "runtime" / "event_extractions")
  monkeypatch.setattr(datepipe_server, "CITY_RESEARCH_DIR", tmp_path / "runtime" / "city_research")
  monkeypatch.setattr(datepipe_server, "WEEKLY_REPORT_DIR", tmp_path / "runtime" / "reports")
  monkeypatch.setattr(datepipe_server, "WEEKLY_REPORT_LATEST_PATH", tmp_path / "runtime" / "reports" / "weekly-event-research-latest.json")
  monkeypatch.setattr(datepipe_server, "CITY_RESEARCH_SECONDS", 3600)
  monkeypatch.setattr(datepipe_server, "DATEPIPE_APK_PATH", tmp_path / "runtime" / "artifacts" / "datepipe-debug.apk")
  monkeypatch.setattr(datepipe_server, "PIPELINE_DB_PATH", tmp_path / "runtime" / "datepipe_pipeline.sqlite3")
  monkeypatch.setattr(datepipe_server, "OPERATIONAL_LOG_PATH", tmp_path / "runtime" / "operational_events.jsonl")
  monkeypatch.setenv("DATEPIPE_AI_PROVIDER", "none")
  monkeypatch.delenv("OPENAI_API_KEY", raising=False)
  monkeypatch.delenv("DATEPIPE_ENABLE_DEEP_RESEARCH", raising=False)
  monkeypatch.delenv("DATEPIPE_DEEP_RESEARCH_MAX_OUTPUT_TOKENS", raising=False)
  monkeypatch.delenv("GEMINI_API_KEY", raising=False)
  monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
  monkeypatch.setattr(datepipe_server, "search_duckduckgo_results", lambda *args, **kwargs: [])
  monkeypatch.setattr(datepipe_server, "search_web_image", lambda *args, **kwargs: "")
  monkeypatch.setattr(datepipe_server, "resolve_page_image", lambda *args, **kwargs: "")
  monkeypatch.setattr(
    datepipe_server,
    "llm_generate_text",
    lambda *args, **kwargs: {"ok": False, "provider": "", "model": "", "text": "", "status": "test-fallback"},
  )
  datepipe_server.IMAGE_CACHE.clear()
  datepipe_server.SEARCH_IMAGE_CACHE.clear()
  return tmp_path


@pytest.fixture()
def api(isolated_runtime):
  httpd = datepipe_server.DatePipeServer(("127.0.0.1", 0))
  thread = threading.Thread(target=httpd.serve_forever, daemon=True)
  thread.start()
  base_url = f"http://127.0.0.1:{httpd.server_address[1]}"

  class Client:
    def request(self, method: str, path: str, body: dict | None = None, token: str = "", expected: int | None = None):
      data = None if body is None else json.dumps(body).encode("utf-8")
      headers = {}
      if body is not None:
        headers["Content-Type"] = "application/json"
      if token:
        headers["Authorization"] = f"Bearer {token}"
      req = Request(base_url + path, data=data, headers=headers, method=method)
      try:
        with urlopen(req, timeout=20) as response:
          payload = response.read().decode("utf-8")
          status = response.status
      except HTTPError as exc:
        payload = exc.read().decode("utf-8")
        status = exc.code
      if expected is not None:
        assert status == expected, payload
      if not payload:
        return status, {}
      return status, json.loads(payload)

    def get(self, path: str, token: str = "", expected: int | None = None):
      return self.request("GET", path, token=token, expected=expected)

    def post(self, path: str, body: dict, token: str = "", expected: int | None = None):
      return self.request("POST", path, body=body, token=token, expected=expected)

    def query(self, path: str, params: dict, token: str = "", expected: int | None = None):
      return self.get(path + "?" + urlencode(params), token=token, expected=expected)

    def raw_get(self, path: str, token: str = ""):
      headers = {}
      if token:
        headers["Authorization"] = f"Bearer {token}"
      req = Request(base_url + path, headers=headers, method="GET")
      with urlopen(req, timeout=20) as response:
        return response.status, response.read()

  Client.base_url = base_url
  try:
    yield Client()
  finally:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


@pytest.fixture()
def fixture_text():
  def read(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "fixtures" / name).read_text(encoding="utf-8")

  return read


@pytest.fixture()
def registered(api):
  _status, account = api.post(
    "/api/auth/register",
    {"email": "owner@example.test", "password": "correct horse battery staple", "deviceLabel": "Test", "platform": "pytest"},
    expected=201,
  )
  return account
