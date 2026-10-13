from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class RateLimitDecision:
  allowed: bool
  count: int
  limit: int
  windowSeconds: int
  reason: str

  def as_dict(self) -> dict[str, Any]:
    return {
      "schema": "datepipe.rate_limit_decision.v1",
      "allowed": self.allowed,
      "count": self.count,
      "limit": self.limit,
      "windowSeconds": self.windowSeconds,
      "reason": self.reason,
    }


def parse_utc(value: object) -> datetime | None:
  text = str(value or "").strip()
  if not text:
    return None
  try:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
  except ValueError:
    return None


def auth_failure_decision(attempts: list[dict], email_lookup: str, *, now: datetime | None = None, limit: int = 5, window_seconds: int = 15 * 60) -> RateLimitDecision:
  current = now or datetime.now(timezone.utc)
  cutoff = current - timedelta(seconds=window_seconds)
  count = 0
  for row in attempts:
    if row.get("emailLookup") != email_lookup:
      continue
    if row.get("ok") is True:
      continue
    created = parse_utc(row.get("createdAt"))
    if created and created >= cutoff:
      count += 1
  allowed = count < limit
  return RateLimitDecision(
    allowed=allowed,
    count=count,
    limit=limit,
    windowSeconds=window_seconds,
    reason="within_limit" if allowed else "rate_limited",
  )
