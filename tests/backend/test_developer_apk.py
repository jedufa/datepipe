from __future__ import annotations

from datepipe_backend.mobile_apk import DEBUG_APK_DOWNLOAD_PATH, MobileApkService


def test_mobile_apk_service_reports_missing_artifact(tmp_path):
  manifest = MobileApkService(tmp_path / "missing.apk").manifest()
  job = MobileApkService(tmp_path / "missing.apk").build_job()

  assert manifest["available"] is False
  assert manifest["download_path"] == ""
  assert job["status"] == "missing"
  assert job["download_path"] == ""


def test_developer_apk_manifest_and_download(api, isolated_runtime):
  apk = isolated_runtime / "runtime" / "artifacts" / "datepipe-debug.apk"
  apk.parent.mkdir(parents=True, exist_ok=True)
  apk.write_bytes(b"fake-apk")

  _status, manifest = api.get("/api/developer/mobile-apk/manifest", expected=200)
  status, payload = api.raw_get("/api/developer/mobile-apk/download")

  assert manifest["available"] is True
  assert manifest["size_bytes"] == 8
  assert manifest["download_path"] == DEBUG_APK_DOWNLOAD_PATH
  assert status == 200
  assert payload == b"fake-apk"
