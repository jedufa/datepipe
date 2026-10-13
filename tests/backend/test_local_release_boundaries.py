from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

import server
from datepipe_backend import local_release


def test_non_loopback_binding_is_rejected():
  with pytest.raises(ValueError, match="127.0.0.1"):
    server.DatePipeServer(("0.0.0.0", 0))


@pytest.mark.parametrize("headers", [
  {"Host": "foreign.example.invalid"},
  {"Origin": "https://foreign.example.invalid"},
  {"Sec-Fetch-Site": "cross-site"},
  {"Origin": "null"},
])
def test_foreign_browser_request_cannot_change_profile(api, headers):
  _, before = api.get("/api/state", expected=200)
  request = Request(api.base_url + "/api/settings",
                    data=json.dumps({"homeBase": "Rejected mutation"}).encode(),
                    headers={"Content-Type": "application/json", **headers}, method="POST")
  with pytest.raises(HTTPError) as error:
    urlopen(request, timeout=5)
  assert error.value.code == 403
  _, after = api.get("/api/state", expected=200)
  assert after["profile"] == before["profile"]


def test_default_network_policy_blocks_before_transport(monkeypatch):
  monkeypatch.delenv("DATEPIPE_ALLOW_NETWORK", raising=False)
  calls = []
  monkeypatch.setattr(local_release, "_urlopen", lambda *args, **kwargs: calls.append(args))
  with pytest.raises(URLError, match="disabled"):
    local_release.outbound_urlopen("https://example.invalid")
  assert calls == []


def test_explicit_network_policy_uses_transport_without_real_request(monkeypatch):
  monkeypatch.setenv("DATEPIPE_ALLOW_NETWORK", "1")
  marker = object()
  monkeypatch.setattr(local_release, "_urlopen", lambda *args, **kwargs: marker)
  assert local_release.outbound_urlopen("https://example.invalid") is marker


def test_private_key_is_stable_in_a_separate_process(isolated_runtime):
  path = server.STATE_PATH
  digest = hashlib.sha256(local_release.local_auth_secret(path, "DATEPIPE_SESSION_SECRET")).hexdigest()
  script = ("import hashlib; from pathlib import Path; "
            "from datepipe_backend.local_release import local_auth_secret; "
            "import sys; print(hashlib.sha256(local_auth_secret(Path(sys.argv[1]), "
            "'DATEPIPE_SESSION_SECRET')).hexdigest())")
  result = subprocess.check_output([sys.executable, "-c", script, str(path)], text=True).strip()
  assert result == digest
  key = path.with_name(path.name + ".auth.key")
  assert key.stat().st_mode & 0o777 == 0o600
  assert key.stat().st_size == 32


def test_local_key_refuses_a_symlink(tmp_path):
  target = tmp_path / "unrelated"
  target.write_bytes(b"x" * 32)
  state = tmp_path / "state.json"
  state.with_name(state.name + ".auth.key").symlink_to(target)
  with pytest.raises(OSError):
    local_release.local_auth_secret(state, "DATEPIPE_SESSION_SECRET")
  assert target.read_bytes() == b"x" * 32
