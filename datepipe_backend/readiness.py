from __future__ import annotations

from pathlib import Path
from typing import Any


def path_status(path: Path, *, must_exist: bool = True, writable_parent: bool = False) -> dict[str, Any]:
  exists = path.exists()
  parent = path if path.is_dir() else path.parent
  nearest = parent
  while not nearest.exists() and nearest != nearest.parent:
    nearest = nearest.parent
  parent_writable = nearest.exists() and nearest.is_dir()
  return {
    "path": str(path),
    "exists": exists,
    "ok": (exists or not must_exist) and (not writable_parent or parent_writable),
    "sizeBytes": path.stat().st_size if exists and path.is_file() else 0,
    "parentWritable": parent_writable,
  }


def readiness_payload(
  *,
  state_path: Path,
  city_pack_dir: Path,
  digest_dir: Path,
  extraction_dir: Path,
  apk_path: Path,
  operational_log_path: Path,
) -> dict[str, Any]:
  checks = {
    "state": path_status(state_path, must_exist=False, writable_parent=True),
    "cityPackDir": path_status(city_pack_dir),
    "sourcesSelected": path_status(city_pack_dir / "sources_selected.csv"),
    "sourcesAll": path_status(city_pack_dir / "sources_all.csv"),
    "citySummary": path_status(city_pack_dir / "city_summary.csv"),
    "digestDir": path_status(digest_dir, must_exist=False, writable_parent=True),
    "eventExtractionDir": path_status(extraction_dir, must_exist=False, writable_parent=True),
    "mobileApk": path_status(apk_path, must_exist=False, writable_parent=True),
    "operationalLog": path_status(operational_log_path, must_exist=False, writable_parent=True),
  }
  return {
    "schema": "datepipe.readiness.v1",
    "ok": all(check.get("ok") for check in checks.values()),
    "checks": checks,
  }
