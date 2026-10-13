from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path


DEBUG_APK_NAME = "datepipe-debug.apk"
DEBUG_APK_DOWNLOAD_PATH = "/api/developer/mobile-apk/download?channel=debug"


@dataclass(frozen=True)
class MobileApkService:
  apk_path: Path

  def latest_apk(self) -> Path:
    versioned_dir = self.apk_path.parent / "mobile_apk" / "debug"
    if versioned_dir.exists():
      candidates = sorted(versioned_dir.glob("datepipe-debug-*.apk"), key=lambda path: path.stat().st_mtime, reverse=True)
      if candidates:
        return candidates[0]
    return self.apk_path

  def manifest(self) -> dict[str, object]:
    latest = self.latest_apk()
    available = latest.exists()
    payload: dict[str, object] = {
      "schema_version": "datepipe_mobile_apk_manifest_v1",
      "channel": "debug",
      "apk_name": latest.name if available else DEBUG_APK_NAME,
      "available": available,
      "release_available": available,
      "download_path": DEBUG_APK_DOWNLOAD_PATH if available else "",
    }
    if available:
      payload.update(
        {
          "path": str(latest),
          "size_bytes": latest.stat().st_size,
        }
      )
      metadata_path = latest.with_suffix(latest.suffix + ".meta.json")
      if metadata_path.exists():
        payload["metadata"] = json.loads(metadata_path.read_text(encoding="utf-8"))
    return payload

  def build_job(self) -> dict[str, object]:
    available = self.apk_path.exists()
    return {
      "job_id": "datepipe-local-debug-apk",
      "status": "succeeded" if available else "missing",
      "running": False,
      "error": "" if available else "APK has not been built yet.",
      "output": "DatePipe Android debug APK is available." if available else "Run ./gradlew :app:assembleDebug in app/mobile/android.",
      "apk_name": DEBUG_APK_NAME if available else "",
      "download_path": DEBUG_APK_DOWNLOAD_PATH if available else "",
    }
