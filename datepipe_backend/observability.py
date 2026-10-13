from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from datepipe_backend.fsio import append_jsonl, read_jsonl


FORBIDDEN_EVENT_KEYS = {
  "authorization",
  "backup_key",
  "ciphertext",
  "diary",
  "password",
  "payload",
  "private_history",
  "prompt",
  "recovery_key",
  "session_token",
  "token",
}

EVENT_CATEGORIES = {
  "auth_failure",
  "auth_success",
  "rate_limit_hit",
  "settings_update",
  "digest_refresh",
  "pipeline_run",
  "server_error",
}


def hash_subject(value: str) -> str:
  if not value:
    return ""
  return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def safe_label(value: object) -> str:
  text = str(value or "").strip().lower()
  return "".join(char if char.isalnum() or char == "_" else "_" for char in text)[:80]


class OperationalEventSink:
  def __init__(self, path: Path, now_iso) -> None:
    self.path = Path(path)
    self.now_iso = now_iso

  def record(
    self,
    *,
    category: str,
    request_id: str,
    status_code: int,
    account_id: str = "",
    redacted_error_category: str = "",
    extra: Mapping[str, Any] | None = None,
  ) -> dict[str, Any]:
    if extra:
      forbidden = {str(key).lower() for key in extra} & FORBIDDEN_EVENT_KEYS
      if forbidden:
        raise ValueError("operational event extra contains forbidden sensitive field")
    event = {
      "schema": "datepipe.operational_event.v1",
      "category": safe_label(category) if safe_label(category) in EVENT_CATEGORIES else "server_error",
      "requestId": safe_label(request_id),
      "statusCode": int(status_code),
      "accountHash": hash_subject(account_id),
      "redactedErrorCategory": safe_label(redacted_error_category),
      "createdAt": self.now_iso(),
    }
    append_jsonl(self.path, event)
    return event

  def read_events(self) -> list[dict[str, Any]]:
    return read_jsonl(self.path)

  def summary(self) -> dict[str, Any]:
    events = self.read_events()
    categories = Counter(str(event.get("category") or "") for event in events)
    errors = Counter(str(event.get("redactedErrorCategory") or "") for event in events if event.get("redactedErrorCategory"))
    text = str(events).lower()
    forbidden = sorted(term for term in FORBIDDEN_EVENT_KEYS if term in text and term not in {"account_hash"})
    return {
      "schema": "datepipe.operational_summary.v1",
      "eventCount": len(events),
      "categoryCounts": dict(sorted(categories.items())),
      "errorCounts": dict(errors.most_common(20)),
      "privacyScan": {
        "ok": not forbidden,
        "forbiddenTermCount": len(forbidden),
        "redacted": True,
      },
    }
