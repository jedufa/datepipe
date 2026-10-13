from __future__ import annotations

import argparse
import csv
import hashlib
import html
import hmac
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import uuid
import zipfile
from copy import deepcopy
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse
from urllib.request import Request
from datepipe_backend.local_release import outbound_urlopen as urlopen, local_auth_secret

from datepipe_backend.fsio import write_json_atomic
from datepipe_backend.event_quality import filter_event_leads
from datepipe_backend.matching_model import rank_candidates, score_candidate
from datepipe_backend.mobile_apk import MobileApkService
from datepipe_backend.observability import OperationalEventSink
from datepipe_backend.pipeline import PipelineStore
from datepipe_backend.rate_limit import auth_failure_decision
from datepipe_backend.readiness import readiness_payload
from datepipe_backend.source_quality import (
  city_source_readiness_status,
  extraction_quality_gate_status,
  select_sources_for_research,
  source_mix_summary,
  source_quality_bucket,
  source_quality_profile,
  source_quality_score,
)
from datepipe_backend.specialness_model import evaluate_specialness as evaluate_specialness_v2


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
SEED_PATH = ROOT / "data" / "seed_dates.json"
DISCOVERY_SOURCES_PATH = ROOT / "data" / "discovery_sources.json"
CITY_PACK_DIR = ROOT / "data" / "city_packs"
STATE_PATH = Path(os.environ.get("DATEPIPE_STATE_PATH", ROOT / "runtime" / "datepipe_state.json")).expanduser()
CITY_DIGEST_DIR = ROOT / "runtime" / "city_digests"
EVENT_EXTRACTION_DIR = ROOT / "runtime" / "event_extractions"
CITY_RESEARCH_DIR = ROOT / "runtime" / "city_research"
WEEKLY_REPORT_DIR = Path(os.environ.get("DATEPIPE_WEEKLY_REPORT_DIR", ROOT / "runtime" / "reports")).expanduser()
WEEKLY_REPORT_LATEST_PATH = Path(
  os.environ.get("DATEPIPE_WEEKLY_REPORT_LATEST_PATH", WEEKLY_REPORT_DIR / "weekly-event-research-latest.json")
).expanduser()
DATEPIPE_APK_PATH = ROOT / "runtime" / "artifacts" / "datepipe-debug.apk"
PIPELINE_DB_PATH = ROOT / "runtime" / "datepipe_pipeline.sqlite3"
OPERATIONAL_LOG_PATH = Path(os.environ.get("DATEPIPE_OPERATIONAL_LOG_PATH", ROOT / "runtime" / "operational_events.jsonl")).expanduser()
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8812
MAX_BODY_BYTES = 512 * 1024
OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
CITY_RESEARCH_SECONDS = int(os.environ.get("DATEPIPE_CITY_RESEARCH_SECONDS", "3600"))
CITY_RESEARCH_MIN_PROMOTED_SOURCES = int(os.environ.get("DATEPIPE_CITY_RESEARCH_MIN_PROMOTED_SOURCES", "2"))
CITY_RESEARCH_DEFAULT_MAX_OUTPUT_TOKENS = 2500
STATE_LOCK = threading.Lock()
IMAGE_CACHE: dict[str, str] = {}
SEARCH_IMAGE_CACHE: dict[str, str] = {}
IMAGE_CACHE_LOCK = threading.Lock()
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".avif")
IMAGE_SKIP_TOKENS = ("logo", "icon", "favicon", "sprite", "avatar", "placeholder", "loader")


def now_iso() -> str:
  return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> dict:
  with path.open("r", encoding="utf-8") as fh:
    return json.load(fh)


def write_json(path: Path, payload: dict) -> None:
  write_json_atomic(path, payload)


def display_runtime_path(path: Path) -> str:
  try:
    return str(path.relative_to(ROOT))
  except ValueError:
    return str(path)


def load_latest_weekly_event_research_report() -> dict:
  if not WEEKLY_REPORT_LATEST_PATH.exists():
    return {
      "schema": "datepipe.weekly_event_research_report.v1",
      "available": False,
      "status": "missing_report",
      "message": "No weekly event research report has been generated yet.",
      "path": display_runtime_path(WEEKLY_REPORT_LATEST_PATH),
    }
  try:
    payload = load_json(WEEKLY_REPORT_LATEST_PATH)
  except Exception as exc:
    return {
      "schema": "datepipe.weekly_event_research_report.v1",
      "available": False,
      "status": "unreadable_report",
      "message": f"Latest weekly event research report could not be read: {type(exc).__name__}",
      "path": display_runtime_path(WEEKLY_REPORT_LATEST_PATH),
    }
  if isinstance(payload, dict):
    payload.setdefault("schema", "datepipe.weekly_event_research_report.v1")
    payload["available"] = True
    payload.setdefault("status", "available")
    payload.setdefault("path", display_runtime_path(WEEKLY_REPORT_LATEST_PATH))
    return payload
  return {
    "schema": "datepipe.weekly_event_research_report.v1",
    "available": False,
    "status": "invalid_report",
    "message": "Latest weekly event research report is not a JSON object.",
    "path": display_runtime_path(WEEKLY_REPORT_LATEST_PATH),
  }


def default_state() -> dict:
  seed = load_json(SEED_PATH)
  seed["meta"] = {
    "createdAt": now_iso(),
    "updatedAt": now_iso(),
    "schema": "datepipe_local_state_v1",
  }
  return seed


def load_state() -> dict:
  with STATE_LOCK:
    if not STATE_PATH.exists():
      state = default_state()
      write_json(STATE_PATH, state)
      return state
    state = load_json(STATE_PATH)
    if merge_seed_library(state):
      write_json(STATE_PATH, state)
    return state


def save_state(state: dict) -> dict:
  with STATE_LOCK:
    state.setdefault("meta", {})
    state["meta"]["updatedAt"] = now_iso()
    write_json(STATE_PATH, state)
  return state


def datepipe_secret(name: str, fallback: str) -> bytes:
  return local_auth_secret(STATE_PATH, name)


def stable_hmac(value: str, *, secret_name: str = "DATEPIPE_SESSION_SECRET") -> str:
  secret = local_auth_secret(STATE_PATH, secret_name)
  return hmac.new(secret, value.encode("utf-8"), hashlib.sha256).hexdigest()


def email_lookup(value: object) -> str:
  return stable_hmac(str(value or "").strip().lower(), secret_name="DATEPIPE_ACCOUNT_SECRET")


def password_hash(password: str, salt: str | None = None) -> str:
  salt = salt or secrets.token_hex(16)
  pepper = os.environ.get("DATEPIPE_PASSWORD_PEPPER", "")
  digest = hashlib.pbkdf2_hmac("sha256", f"{password}{pepper}".encode("utf-8"), bytes.fromhex(salt), 180_000)
  return f"pbkdf2_sha256$180000${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
  try:
    algorithm, iterations, salt, expected = encoded.split("$", 3)
    if algorithm != "pbkdf2_sha256":
      return False
    pepper = os.environ.get("DATEPIPE_PASSWORD_PEPPER", "")
    digest = hashlib.pbkdf2_hmac("sha256", f"{password}{pepper}".encode("utf-8"), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(digest.hex(), expected)
  except Exception:
    return False


def session_token() -> str:
  return f"dtp_{secrets.token_urlsafe(32)}"


def token_hash(token: str) -> str:
  return stable_hmac(token, secret_name="DATEPIPE_SESSION_SECRET")


def operational_sink() -> OperationalEventSink:
  return OperationalEventSink(OPERATIONAL_LOG_PATH, now_iso)


def record_operational_event(
  category: str,
  *,
  status_code: int,
  account_id: str = "",
  redacted_error_category: str = "",
) -> None:
  try:
    operational_sink().record(
      category=category,
      request_id=new_id("req"),
      status_code=status_code,
      account_id=account_id,
      redacted_error_category=redacted_error_category,
    )
  except Exception:
    # Observability must never break local-first planning.
    return


def default_subscription() -> dict:
  return {
    "status": "free",
    "plan": "local-free",
    "source": "local",
    "currentPeriodEnd": "",
    "entitlements": {
      "anonymousLocalPlanning": True,
      "dailyCityDigest": True,
      "coupleMemory": True,
      "cloudSync": False,
      "heavyBackendAi": False,
    },
  }


def account_public_summary(account: dict, device_id: str = "") -> dict:
  subscription = account.get("subscription") if isinstance(account.get("subscription"), dict) else default_subscription()
  return {
    "schema": "datepipe.account.v1",
    "accountId": account.get("id", ""),
    "namespaceId": account.get("namespaceId", ""),
    "deviceId": device_id,
    "createdAt": account.get("createdAt", ""),
    "subscription": subscription,
    "supportCanReadPrivateHistory": False,
    "plaintextPasswordStored": False,
  }


def accounts_container(state: dict) -> dict:
  return state.setdefault("_accounts", {"schema": "datepipe.accounts.v1", "accounts": {}, "emailIndex": {}, "authEvents": [], "loginAttempts": []})


def account_state_seed() -> dict:
  state = default_state()
  state["meta"]["schema"] = "datepipe_account_state_v1"
  return state


def create_session_record(account: dict, device_id: str) -> tuple[str, dict]:
  token = session_token()
  session = {
    "id": new_id("ses"),
    "deviceId": device_id,
    "tokenHash": token_hash(token),
    "createdAt": now_iso(),
    "lastSeenAt": now_iso(),
    "revokedAt": "",
  }
  account.setdefault("sessions", []).insert(0, session)
  del account["sessions"][30:]
  return token, session


def append_auth_event(state: dict, event_type: str, account_id: str = "", detail: dict | None = None) -> None:
  container = accounts_container(state)
  row = {
    "id": new_id("auth"),
    "createdAt": now_iso(),
    "type": event_type,
    "accountId": account_id,
    "detail": detail or {},
  }
  container.setdefault("authEvents", []).insert(0, row)
  del container["authEvents"][200:]


def register_account(body: dict) -> tuple[HTTPStatus, dict]:
  email = str(body.get("email") or "").strip().lower()
  password = str(body.get("password") or "")
  if "@" not in email or len(password) < 8:
    return HTTPStatus.BAD_REQUEST, {"error": "bad_request", "requestId": new_id("req")}
  state = load_state()
  container = accounts_container(state)
  lookup = email_lookup(email)
  if lookup in container.setdefault("emailIndex", {}):
    return HTTPStatus.CONFLICT, {"error": "account_exists", "requestId": new_id("req")}
  account_id = new_id("acc")
  device_id = new_id("dev")
  account = {
    "id": account_id,
    "namespaceId": new_id("ns"),
    "emailLookup": lookup,
    "passwordHash": password_hash(password),
    "createdAt": now_iso(),
    "disabledAt": "",
    "devices": [{
      "id": device_id,
      "label": str(body.get("deviceLabel") or body.get("device_label") or "Device")[:80],
      "platform": str(body.get("platform") or "unknown")[:80],
      "createdAt": now_iso(),
      "revokedAt": "",
    }],
    "sessions": [],
    "subscription": default_subscription(),
    "state": account_state_seed(),
  }
  token, _session = create_session_record(account, device_id)
  container["accounts"][account_id] = account
  container["emailIndex"][lookup] = account_id
  append_auth_event(state, "register", account_id, {"deviceId": device_id})
  save_state(state)
  record_operational_event("auth_success", status_code=HTTPStatus.CREATED, account_id=account_id)
  summary = account_public_summary(account, device_id)
  summary.update({"sessionToken": token, "rotatesRefreshToken": True})
  return HTTPStatus.CREATED, summary


def login_account(body: dict) -> tuple[HTTPStatus, dict]:
  email = str(body.get("email") or "").strip().lower()
  password = str(body.get("password") or "")
  state = load_state()
  container = accounts_container(state)
  lookup = email_lookup(email)
  rate = auth_failure_decision(container.setdefault("loginAttempts", []), lookup)
  if not rate.allowed:
    container.setdefault("loginAttempts", []).insert(0, {
      "id": new_id("login"),
      "createdAt": now_iso(),
      "ok": False,
      "emailLookup": lookup,
      "redactedCategory": "rate_limited",
    })
    del container["loginAttempts"][200:]
    save_state(state)
    record_operational_event("rate_limit_hit", status_code=HTTPStatus.TOO_MANY_REQUESTS, redacted_error_category="rate_limited")
    return HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited", "requestId": new_id("req"), "rateLimit": rate.as_dict()}
  account_id = container.get("emailIndex", {}).get(email_lookup(email), "")
  account = container.get("accounts", {}).get(account_id)
  if not account or account.get("disabledAt") or not verify_password(password, str(account.get("passwordHash") or "")):
    container.setdefault("loginAttempts", []).insert(0, {"id": new_id("login"), "createdAt": now_iso(), "ok": False, "emailLookup": lookup, "redactedCategory": "invalid_credentials"})
    del container["loginAttempts"][200:]
    save_state(state)
    record_operational_event("auth_failure", status_code=HTTPStatus.UNAUTHORIZED, account_id=account_id, redacted_error_category="invalid_credentials")
    return HTTPStatus.UNAUTHORIZED, {"error": "invalid_credentials", "requestId": new_id("req")}
  device_id = str(body.get("deviceId") or body.get("device_id") or "").strip()
  devices = account.setdefault("devices", [])
  if not any(device.get("id") == device_id and not device.get("revokedAt") for device in devices):
    device_id = new_id("dev")
    devices.append({
      "id": device_id,
      "label": str(body.get("deviceLabel") or body.get("device_label") or "Device")[:80],
      "platform": str(body.get("platform") or "unknown")[:80],
      "createdAt": now_iso(),
      "revokedAt": "",
    })
  token, _session = create_session_record(account, device_id)
  append_auth_event(state, "login", account_id, {"deviceId": device_id})
  save_state(state)
  record_operational_event("auth_success", status_code=HTTPStatus.OK, account_id=account_id)
  summary = account_public_summary(account, device_id)
  summary.update({"sessionToken": token, "rotatesRefreshToken": True})
  return HTTPStatus.OK, summary


def auth_context_from_headers(headers: object) -> dict | None:
  auth = str(getattr(headers, "get", lambda _key, _default=None: "")("Authorization", "") or getattr(headers, "get", lambda _key, _default=None: "")("authorization", ""))
  if not auth.lower().startswith("bearer "):
    return None
  hashed = token_hash(auth.split(" ", 1)[1].strip())
  state = load_state()
  container = accounts_container(state)
  for account in container.get("accounts", {}).values():
    if account.get("disabledAt"):
      continue
    devices = {device.get("id"): device for device in account.get("devices", [])}
    for session in account.get("sessions", []):
      if session.get("tokenHash") != hashed or session.get("revokedAt"):
        continue
      device = devices.get(session.get("deviceId"), {})
      if device.get("revokedAt"):
        return None
      session["lastSeenAt"] = now_iso()
      save_state(state)
      return {
        "authenticated": True,
        "accountId": account.get("id", ""),
        "deviceId": session.get("deviceId", ""),
        "sessionId": session.get("id", ""),
      }
  return None


def account_for_context(state: dict, context: dict) -> dict | None:
  if not context:
    return None
  return accounts_container(state).get("accounts", {}).get(context.get("accountId"))


def load_scoped_state(context: dict | None = None) -> dict:
  state = load_state()
  account = account_for_context(state, context or {})
  if account is not None:
    account.setdefault("state", account_state_seed())
    return deepcopy(account["state"])
  return state


def save_scoped_state(local_state: dict, context: dict | None = None) -> dict:
  if context:
    root_state = load_state()
    account = account_for_context(root_state, context)
    if account is not None:
      local_state.setdefault("meta", {})
      local_state["meta"]["updatedAt"] = now_iso()
      account["state"] = local_state
      save_state(root_state)
      return local_state
  return save_state(local_state)


def refresh_account_session(context: dict, body: dict) -> tuple[HTTPStatus, dict]:
  state = load_state()
  account = account_for_context(state, context)
  if not account or str(body.get("deviceId") or body.get("device_id") or "") != context.get("deviceId"):
    return HTTPStatus.FORBIDDEN, {"error": "forbidden", "requestId": new_id("req")}
  for session in account.get("sessions", []):
    if session.get("id") == context.get("sessionId"):
      session["revokedAt"] = now_iso()
  token, _session = create_session_record(account, context["deviceId"])
  append_auth_event(state, "refresh", account.get("id", ""), {"deviceId": context["deviceId"]})
  save_state(state)
  summary = account_public_summary(account, context["deviceId"])
  summary.update({"sessionToken": token, "rotatesRefreshToken": True, "oldTokenRevoked": True})
  return HTTPStatus.OK, summary


def logout_account(context: dict) -> tuple[HTTPStatus, dict]:
  state = load_state()
  account = account_for_context(state, context)
  if not account:
    return HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "requestId": new_id("req")}
  for session in account.get("sessions", []):
    if session.get("id") == context.get("sessionId"):
      session["revokedAt"] = now_iso()
  append_auth_event(state, "logout", account.get("id", ""), {"deviceId": context.get("deviceId", "")})
  save_state(state)
  return HTTPStatus.OK, {"ok": True, "revoked": True}


def get_account_summary(context: dict) -> tuple[HTTPStatus, dict]:
  state = load_state()
  account = account_for_context(state, context)
  if not account:
    return HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "requestId": new_id("req")}
  scoped = account.get("state", {})
  summary = account_public_summary(account, context.get("deviceId", ""))
  summary["stateSummary"] = {
    "planCount": len(scoped.get("plans", [])),
    "ideaCount": len(scoped.get("ideas", [])),
    "memoryCount": len(scoped.get("coupleMemory", [])),
    "signalCount": len(scoped.get("matchingSignals", [])),
  }
  return HTTPStatus.OK, summary


def get_subscription_summary(context: dict | None = None) -> dict:
  state = load_state()
  account = account_for_context(state, context or {})
  subscription = account.get("subscription") if account else default_subscription()
  return {"schema": "datepipe.subscription.v1", **subscription}


def activate_dev_subscription(context: dict, body: dict) -> tuple[HTTPStatus, dict]:
  state = load_state()
  account = account_for_context(state, context)
  if not account:
    return HTTPStatus.UNAUTHORIZED, {"error": "unauthorized", "requestId": new_id("req")}
  plan = str(body.get("plan") or "monthly-dev").strip()[:60]
  account["subscription"] = {
    "status": "monthly_active",
    "plan": plan,
    "source": "local-dev",
    "currentPeriodEnd": (datetime.now(UTC) + timedelta(days=31)).date().isoformat(),
    "entitlements": {
      "anonymousLocalPlanning": True,
      "dailyCityDigest": True,
      "coupleMemory": True,
      "cloudSync": True,
      "heavyBackendAi": True,
    },
  }
  append_auth_event(state, "subscription_activate_dev", account.get("id", ""), {"plan": plan})
  save_state(state)
  return HTTPStatus.OK, get_subscription_summary(context)


def merge_seed_library(state: dict) -> bool:
  seed = load_json(SEED_PATH)
  changed = False
  existing_ideas = {item.get("id") for item in state.get("ideas", [])}
  ideas_by_id = {item.get("id"): item for item in state.get("ideas", [])}
  for idea in seed.get("ideas", []):
    if idea.get("id") not in existing_ideas:
      state.setdefault("ideas", []).append(idea)
      changed = True
    else:
      existing = ideas_by_id.get(idea.get("id"), {})
      for key in ("eventUrl", "imageUrl", "sourceName"):
        if idea.get(key) and existing.get(key) != idea.get(key):
          existing[key] = idea[key]
          changed = True
  plans_by_id = {item.get("id"): item for item in state.get("plans", [])}
  for plan in seed.get("plans", []):
    existing = plans_by_id.get(plan.get("id"))
    if not existing:
      continue
    for key in ("eventUrl", "imageUrl", "sourceName"):
      if plan.get(key) and not existing.get(key):
        existing[key] = plan[key]
        changed = True
    existing_attachments = {item.get("id") for item in existing.get("attachments", [])}
    for attachment in plan.get("attachments", []):
      if attachment.get("id") not in existing_attachments:
        existing.setdefault("attachments", []).append(attachment)
        changed = True
  return changed


def new_id(prefix: str) -> str:
  return f"{prefix}-{uuid.uuid4().hex[:10]}"


def parse_date(value: str) -> date:
  return datetime.strptime(value, "%Y-%m-%d").date()


def parse_date_or_none(value: object) -> date | None:
  if not value:
    return None
  try:
    return parse_date(str(value))
  except ValueError:
    return None


def week_bounds(value: object = None) -> tuple[date, date]:
  day = parse_date_or_none(value) or date.today()
  start = day - timedelta(days=day.weekday())
  return start, start + timedelta(days=6)


def week_label(start: date, end: date) -> str:
  return f"{start.strftime('%b %-d')} to {end.strftime('%b %-d')}"


def next_week_slots(count: int = 8) -> list[dict]:
  today = date.today()
  first_monday = today - timedelta(days=today.weekday())
  slots = []
  for index in range(count):
    start = first_monday + timedelta(days=7 * index)
    end = start + timedelta(days=6)
    slots.append({
      "id": f"slot-auto-{start.isoformat()}",
      "start": start.isoformat(),
      "end": end.isoformat(),
      "week": f"auto-{index + 1}",
      "status": "open",
      "title": "Open planning week",
      "notes": "Generated as a Monday to Sunday planning window.",
      "tags": ["open", "needs-plan"],
    })
  return slots


def next_weekend_slots(count: int = 8) -> list[dict]:
  return next_week_slots(count)


def effective_slots(state: dict) -> list[dict]:
  slots = list(state.get("slots", []))
  existing = {slot.get("start") for slot in slots}
  for slot in next_week_slots(10):
    if slot["start"] not in existing:
      slots.append(slot)
  today = date.today()
  future_slots = [
    slot for slot in slots
    if (parse_date_or_none(slot.get("end") or slot.get("start")) or today) >= today
  ]
  return sorted(future_slots, key=lambda item: item.get("start", "9999-99-99"))


def status_counts(state: dict) -> dict:
  counts: dict[str, int] = {}
  for slot in effective_slots(state):
    status = slot.get("status", "open")
    counts[status] = counts.get(status, 0) + 1
  return counts


def normalize_tags(value: object) -> list[str]:
  if isinstance(value, list):
    return [str(item).strip().lower() for item in value if str(item).strip()]
  if isinstance(value, str):
    return [item.strip().lower() for item in re.split(r"[,|]", value) if item.strip()]
  return []


def normalize_key(value: object) -> str:
  return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def city_alias_key(value: object) -> str:
  key = normalize_key(value)
  aliases = {
    "wien": "vienna",
    "klagenfurt": "klagenfurt am worthersee",
    "klagenfurt am worthersee": "klagenfurt am worthersee",
    "klagenfurt am woerthersee": "klagenfurt am worthersee",
  }
  return aliases.get(key, key)


def city_slug(value: object) -> str:
  slug = re.sub(r"[^a-z0-9]+", "-", city_alias_key(value)).strip("-")
  return slug or "unknown"


def parse_iso_datetime(value: object) -> datetime | None:
  if not value:
    return None
  try:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
  except ValueError:
    return None


def normalize_url_key(value: object) -> str:
  raw = str(value or "").strip()
  if not raw:
    return ""
  parsed = urlparse(raw)
  host = parsed.netloc.lower().removeprefix("www.")
  path = re.sub(r"/+", "/", parsed.path.rstrip("/").lower())
  return f"{host}{path}" if host else normalize_key(raw)


def city_has_local_source(city: str) -> bool:
  sources = discovery_sources_for_city(city)
  local_sources = [source for source in sources if str(source.get("scope") or "").lower() != "national"]
  readiness = city_source_readiness_status(
    accepted_count=len(local_sources),
    source_mix=source_mix_summary(local_sources),
    min_sources=CITY_RESEARCH_MIN_PROMOTED_SOURCES,
  )
  return readiness == "ready"


def city_source_status(city: str) -> dict:
  sources = discovery_sources_for_city(city)
  local_sources = [source for source in sources if str(source.get("scope") or "").lower() != "national"]
  source_mix = source_mix_summary(sources)
  local_mix = source_mix_summary(local_sources)
  readiness = city_source_readiness_status(
    accepted_count=len(local_sources),
    source_mix=local_mix,
    min_sources=CITY_RESEARCH_MIN_PROMOTED_SOURCES,
  )
  return {
    "schema": "datepipe.city_source_status.v1",
    "city": city,
    "known": readiness == "ready",
    "sourceCount": len(sources),
    "localSourceCount": len(local_sources),
    "sourceMix": source_mix,
    "localSourceMix": local_mix,
    "sourceReadinessStatus": readiness,
  }


def load_city_pack_sources(city: str, selected_only: bool = True) -> list[dict]:
  filename = "sources_selected.csv" if selected_only else "sources_all.csv"
  path = CITY_PACK_DIR / filename
  if not path.exists():
    return []
  wanted = city_alias_key(city)
  with path.open("r", encoding="utf-8", newline="") as fh:
    rows = list(csv.DictReader(fh))
  return [row for row in rows if city_alias_key(row.get("city")) == wanted]


def load_discovery_source_catalog() -> dict:
  if not DISCOVERY_SOURCES_PATH.exists():
    return {"schema": "datepipe.discovery_sources.v1", "sources": []}
  try:
    data = load_json(DISCOVERY_SOURCES_PATH)
  except Exception:
    return {"schema": "datepipe.discovery_sources.v1", "sources": []}
  if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
    return {"schema": "datepipe.discovery_sources.v1", "sources": []}
  return data


def discovery_source_matches_city(source: dict, city: str) -> bool:
  scope = str(source.get("scope") or "").lower()
  if scope == "national":
    return True
  wanted = city_alias_key(city)
  cities = source.get("cities") if isinstance(source.get("cities"), list) else []
  return any(city_alias_key(item) == wanted for item in cities)


def discovery_search_hints(source: dict, city: str) -> list[str]:
  hints = source.get("searchHints") if isinstance(source.get("searchHints"), list) else []
  return [str(item).replace("{city}", city).strip() for item in hints if str(item).strip()]


def discovery_source_score(source: dict) -> int:
  return source_quality_score(source)


def discovery_sources_for_city(city: str, limit: int = 24) -> list[dict]:
  catalog = load_discovery_source_catalog()
  rows = []
  for raw in catalog.get("sources", []):
    if not isinstance(raw, dict) or not discovery_source_matches_city(raw, city):
      continue
    row = deepcopy(raw)
    row["searchHints"] = discovery_search_hints(row, city)
    row.update(source_quality_profile(row))
    rows.append(row)
  for source in load_city_pack_sources(city, selected_only=True):
    url = str(source.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
      continue
    location_scope = str(source.get("locationScope") or source.get("location_scope") or "").strip().lower()
    if location_scope and location_scope != "city-core":
      continue
    source_type = str(source.get("sourceType") or "")
    role_text = " ".join(normalize_tags(source.get("sourceRole")))
    if not any(token in f"{source_type} {role_text}".lower() for token in ("calendar", "guide", "festival", "market", "venue")):
      continue
    city_pack_source = {
      "id": f"city-pack.{source.get('id')}",
      "name": source.get("name", ""),
      "url": url,
      "scope": "city-pack",
      "cities": [source.get("city", city)],
      "sourceKind": source_type or "city-pack-source",
      "sourceRole": source.get("sourceRole", ""),
      "variety": source.get("variety", ""),
      "datePipeUse": source.get("datePipeUse", ""),
      "locationScope": source.get("locationScope", ""),
      "typicalCadence": source.get("typicalCadence", ""),
      "confidence": source.get("confidence", ""),
      "trustLevel": "high" if source.get("confidence") == "high" else "medium",
      "bestFor": normalize_tags(source.get("categories")),
      "searchHints": [url],
      "verificationRule": source.get("liveVerificationRule") or "Verify current dates, opening hours, booking, and closure status on the primary source.",
    }
    city_pack_source.update(source_quality_profile(city_pack_source))
    rows.append(city_pack_source)
  deduped: dict[str, dict] = {}
  for row in rows:
    key = normalize_url_key(str(row.get("url") or row.get("id") or ""))
    if not key:
      continue
    existing = deduped.get(key)
    if not existing or discovery_source_score(row) > discovery_source_score(existing):
      deduped[key] = row
  external = [row for row in deduped.values() if row.get("scope") != "city-pack"]
  city_pack = [row for row in deduped.values() if row.get("scope") == "city-pack"]
  return select_sources_for_research(external + city_pack, limit)


def city_research_runtime_path(city: str, suffix: str) -> Path:
  return CITY_RESEARCH_DIR / f"{city_slug(city)}-{suffix}.json"


def city_research_path(city: str) -> Path:
  return CITY_RESEARCH_DIR / f"{city_slug(city)}.json"


def city_research_prompt(city: str, target_date: str, profile: dict | None = None) -> str:
  safe_profile = {
    "budgetComfort": (profile or {}).get("budgetComfort", ""),
    "freeTime": (profile or {}).get("freeTime", ""),
    "radiusKm": (profile or {}).get("radiusKm", ""),
    "searchKeywords": (profile or {}).get("searchKeywords", []),
  }
  return (
    "Research a DatePipe city source pack. DatePipe is a local-first private surprise-date planner, "
    "not a generic restaurant or event search app.\n\n"
    f"City: {city}\n"
    f"Planning target date: {target_date}\n"
    f"Couple constraints: {json.dumps(safe_profile, ensure_ascii=False)}\n\n"
    "Find high-quality public web sources for this city. Prefer venue-owned calendars, small cultural venues, "
    "workshop providers, local curated magazines, independent cinemas, galleries, jazz/music rooms, unusual activity "
    "operators, date-friendly food or tasting providers, and official calendars only as baseline coverage. Avoid generic "
    "national homepages, SEO listicles, bare ticketing front pages, regional pages that are not city-core, and sources "
    "that cannot verify current date, location, and booking details.\n\n"
    "Return a concise JSON object with arrays named selectedSources, candidateSources, rejectedSources, and qualityNotes. "
    "Each source should include id, name, url, sourceType, locationScope, confidence, privacyFit, categories, evidenceSummary, "
    "liveVerificationRule, and rejectionReason when rejected."
  )


def local_city_scout_queries(city: str, target_date: str, profile: dict | None = None) -> list[str]:
  keywords = normalize_tags((profile or {}).get("searchKeywords"))[:2]
  base = [
    f"{city} veranstaltungen kultur kalender",
    f"{city} events programme calendar",
    f"{city} theater kino konzert programm",
    f"{city} workshop kurs tasting veranstaltungen",
    f"{city} museum galerie ausstellung programm",
    f"{city} local magazine events",
  ]
  if target_date[:4]:
    base.insert(1, f"{city} events {target_date[:4]} date night")
  for keyword in keywords:
    base.append(f"{city} {keyword} veranstaltungen")
  seen: set[str] = set()
  queries = []
  for query in base:
    key = normalize_key(query)
    if key and key not in seen:
      seen.add(key)
      queries.append(query)
  return queries[:8]


def local_scout_source_type(title: str, url: str) -> str:
  text = normalize_key(f"{title} {url}")
  if any(token in text for token in ("stadt", "city", "gemeinde", "tourismus", "visit", "official")):
    return "city-events"
  if any(token in text for token in ("magazin", "magazine", "guide", "blog", "tips", "1000things")):
    return "local-guide-editorial"
  if any(token in text for token in ("theater", "theatre", "kino", "cinema", "museum", "galerie", "gallery")):
    return "anchor-venue-calendar"
  if any(token in text for token in ("jazz", "club", "konzert", "concert", "musik", "music", "kulturhaus")):
    return "music-venue-calendar"
  if any(token in text for token in ("workshop", "kurs", "atelier", "tasting", "verkostung")):
    return "workshop-provider"
  if any(token in text for token in ("eventbrite", "oeticket", "ticket", "ntry")):
    return "event-platform"
  return "local-event-calendar"


def local_scout_categories(title: str, url: str) -> list[str]:
  text = normalize_key(f"{title} {url}")
  categories = []
  if any(token in text for token in ("theater", "theatre", "museum", "galerie", "gallery", "ausstellung", "kino", "cinema")):
    categories.append("cultural")
  if any(token in text for token in ("jazz", "konzert", "concert", "musik", "music", "club")):
    categories.append("music")
  if any(token in text for token in ("workshop", "kurs", "atelier", "ceramic", "photo")):
    categories.append("workshops")
  if any(token in text for token in ("food", "wine", "tasting", "markt", "kulinar", "verkostung")):
    categories.append("culinary")
  if not categories:
    categories.append("events")
  return categories[:5]


def local_scout_rejection_reason(title: str, url: str) -> str:
  parsed = urlparse(url)
  text = normalize_key(f"{title} {url}")
  host = parsed.netloc.lower().removeprefix("www.")
  if parsed.scheme not in {"http", "https"} or not parsed.netloc:
    return "invalid_url"
  if looks_like_generic_search_page(url):
    return "search_result_page"
  if any(host.endswith(domain) for domain in ("facebook.com", "instagram.com", "youtube.com", "tiktok.com", "linkedin.com")):
    return "social_platform"
  if any(host.endswith(domain) for domain in ("tripadvisor.com", "tripadvisor.at", "yelp.com", "foursquare.com")):
    return "generic_travel_directory"
  if any(token in text for token in ("jobs", "login", "privacy", "datenschutz", "impressum", "kontakt")):
    return "admin_or_meta_page"
  if host in {"eventbrite.at", "eventbrite.com", "oeticket.com", "ntry.at"} and parsed.path.strip("/") in {"", "d", "events"}:
    return "generic_ticketing_homepage"
  return ""


def source_host_key(value: object) -> str:
  return urlparse(str(value or "")).netloc.lower().removeprefix("www.")


def local_city_scout_source(result: dict, city: str, query: str, index: int) -> dict:
  title = strip_tags(str(result.get("title") or ""))[:120]
  url = str(result.get("url") or "").strip()
  source_type = local_scout_source_type(title, url)
  trust_level = "high" if source_type in {"anchor-venue-calendar", "music-venue-calendar"} else "medium"
  source = {
    "id": f"local-scout.{city_slug(city)}.{stable_rotation(url or title, index)}",
    "name": title or urlparse(url).netloc.replace("www.", ""),
    "url": url,
    "sourceType": source_type,
    "sourceKind": source_type,
    "locationScope": "city-core",
    "confidence": trust_level,
    "trustLevel": trust_level,
    "privacyFit": "medium",
    "categories": local_scout_categories(title, url),
    "bestFor": local_scout_categories(title, url),
    "evidenceSummary": f"Found by local city scout query: {query}",
    "liveVerificationRule": "Verify current dates, event detail pages, location, booking, and closure status on the source before reveal.",
    "researchProvenance": "local-web-scout",
  }
  source.update(source_quality_profile(source))
  return source


def local_city_scout_report(city: str, target_date: str, profile: dict | None = None) -> dict:
  selected = []
  rejected = []
  seen: set[str] = set()
  host_counts: Counter[str] = Counter()
  queries = local_city_scout_queries(city, target_date, profile)
  for query_index, query in enumerate(queries):
    for result_index, result in enumerate(search_duckduckgo_results(query, limit=7)):
      url = str(result.get("url") or "").strip()
      title = str(result.get("title") or "").strip()
      key = normalize_url_key(url)
      if not key or key in seen:
        continue
      seen.add(key)
      host = source_host_key(url)
      rejection = local_scout_rejection_reason(title, url)
      if rejection:
        rejected.append({"name": title[:120], "url": url, "rejectionReason": rejection, "query": query})
        continue
      if host and host_counts[host] >= 2:
        rejected.append({"name": title[:120], "url": url, "rejectionReason": "duplicate_host_cap", "query": query})
        continue
      host_counts[host] += 1
      selected.append(local_city_scout_source(result, city, query, query_index * 20 + result_index))
      if len(selected) >= 28:
        break
    if len(selected) >= 28:
      break
  selected = select_sources_for_research(selected, min(18, len(selected)))
  return {
    "selectedSources": selected[:12],
    "candidateSources": selected[12:],
    "rejectedSources": rejected[:40],
    "qualityNotes": [
      "Generated by DatePipe local city scout using a small public web-search budget.",
      "Deep research was not used; every source still needs DatePipe live validation before promotion.",
    ],
    "scoutQueries": queries,
  }


def extract_json_object_from_text(value: object) -> dict:
  if isinstance(value, dict):
    return value
  text = str(value or "").strip()
  if not text:
    return {}
  text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
  try:
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}
  except json.JSONDecodeError:
    pass
  start = text.find("{")
  end = text.rfind("}")
  if start < 0 or end <= start:
    return {}
  try:
    parsed = json.loads(text[start:end + 1])
  except json.JSONDecodeError:
    return {}
  return parsed if isinstance(parsed, dict) else {}


def normalize_researched_source(raw: dict, city: str, index: int) -> dict:
  url = str(raw.get("url") or raw.get("sourceUrl") or raw.get("link") or "").strip()
  name = str(raw.get("name") or raw.get("title") or raw.get("sourceName") or "").strip()
  source_kind = str(raw.get("sourceKind") or raw.get("sourceType") or raw.get("kind") or "local-curated-source").strip()
  categories = normalize_tags(raw.get("categories") or raw.get("bestFor"))
  confidence = normalize_key(raw.get("confidence") or raw.get("trustLevel") or "medium") or "medium"
  trust_level = "high" if confidence in {"high", "primary"} else "medium" if confidence in {"medium", "verified", "close to primary", "curated secondary"} else "low"
  provenance = str(raw.get("researchProvenance") or raw.get("provider") or "openai-deep-research").strip()
  source = {
    "id": str(raw.get("id") or f"research.{city_slug(city)}.{stable_rotation(url or name, index)}"),
    "name": name[:120],
    "url": url,
    "scope": "city",
    "cities": [city.strip()],
    "sourceKind": source_kind[:80],
    "trustLevel": trust_level,
    "bestFor": categories[:8],
    "searchHints": [url] if url else [],
    "verificationRule": str(raw.get("liveVerificationRule") or raw.get("verificationRule") or "Verify current date, time, location, booking, and closure status before reveal.").strip()[:260],
    "evidenceSummary": str(raw.get("evidenceSummary") or "").strip()[:360],
    "privacyFit": str(raw.get("privacyFit") or "").strip()[:40],
    "locationScope": str(raw.get("locationScope") or "city-core").strip()[:60],
    "researchProvenance": provenance[:80] or "openai-deep-research",
    "addedAt": now_iso(),
  }
  source.update(source_quality_profile(source))
  return source


def parse_city_research_tsv_sources(text: str, city: str) -> tuple[list[dict], list[str]]:
  marker = "source_name\tsource_type\tofficial_url"
  start = text.rfind(marker)
  if start < 0:
    return [], []
  lines = []
  for raw_line in text[start:].splitlines():
    line = raw_line.strip()
    if not line:
      continue
    if line.isdigit():
      continue
    if "\t" not in line and lines:
      break
    lines.append(raw_line)
  if len(lines) < 2:
    return [], []
  reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter="\t")
  sources = []
  notes = ["Parsed from collected Deep Research final CSV-ready table."]
  for index, row in enumerate(reader):
    raw = {normalize_key(key).replace(" ", "_"): value for key, value in row.items() if key}
    mvp = normalize_key(raw.get("whether_it_should_be_included_in_mvp_city_pack"))
    if mvp == "no":
      continue
    tags = [item.strip() for item in str(raw.get("suggested_datepipe_tags") or "").split(";") if item.strip()]
    trust = normalize_key(raw.get("trust_level"))
    confidence = "high" if trust == "primary" else "medium" if trust in {"close to primary", "curated secondary"} else "low"
    sources.append({
      "name": raw.get("source_name", ""),
      "sourceType": raw.get("source_type", ""),
      "url": raw.get("official_url", ""),
      "provider": "chatgpt-deep-research-collector",
      "confidence": confidence,
      "trustLevel": trust or confidence,
      "categories": tags,
      "bestFor": tags,
      "evidenceSummary": " ".join(
        item
        for item in (
          raw.get("why_relevant_for_datepipe", ""),
          raw.get("recurring_or_durable_pattern", ""),
          raw.get("freshness_signal", ""),
        )
        if item
      ),
      "liveVerificationRule": raw.get("extraction_notes") or raw.get("risk_notes") or "",
      "locationScope": raw.get("geographic_scope") or "city-core",
      "ownerOrOperator": raw.get("owner_or_operator") or "",
      "mvpRecommendation": mvp or "maybe",
    })
  return sources, notes


def parse_city_research_sources(report: object, city: str) -> dict:
  parsed = extract_json_object_from_text(report)
  selected = parsed.get("selectedSources") if isinstance(parsed.get("selectedSources"), list) else []
  candidate = parsed.get("candidateSources") if isinstance(parsed.get("candidateSources"), list) else []
  rejected = parsed.get("rejectedSources") if isinstance(parsed.get("rejectedSources"), list) else []
  markdown_sources = []
  markdown_notes = []
  if not selected and not candidate and isinstance(report, str):
    markdown_sources, markdown_notes = parse_city_research_tsv_sources(report, city)
    selected = markdown_sources
  normalized = []
  seen = set()
  for index, raw in enumerate(selected + candidate):
    if not isinstance(raw, dict):
      continue
    source = normalize_researched_source(raw, city, index)
    key = normalize_url_key(source.get("url"))
    if not key or key in seen:
      continue
    seen.add(key)
    normalized.append(source)
  notes = parsed.get("qualityNotes") if isinstance(parsed.get("qualityNotes"), list) else []
  if markdown_notes:
    notes = [*markdown_notes, *notes]
  return {
    "schema": "datepipe.city_research_staging.v1",
    "city": city,
    "generatedAt": now_iso(),
    "selectedSourceCount": len(selected),
    "candidateSourceCount": len(candidate),
    "rejectedSourceCount": len(rejected),
    "sources": normalized,
    "researchRejectedSources": rejected[:40],
    "qualityNotes": [str(item)[:300] for item in notes[:20]],
    "parseStatus": "parsed" if normalized else "no_sources_parsed",
  }


def source_document_has_event_signal(document: str) -> bool:
  text = normalize_key(strip_markup(document[:80000]))
  event_tokens = (
    "event", "events", "veranstaltung", "veranstaltungen", "programm", "programme", "calendar", "kalender",
    "ticket", "tickets", "booking", "buchen", "reservieren", "konzert", "concert", "theater", "kino",
    "cinema", "museum", "ausstellung", "exhibition", "workshop", "kurs", "jazz", "festival", "markt",
  )
  token_hits = sum(1 for token in event_tokens if token in text)
  date_signal = bool(
    re.search(r"\b20\d{2}\b", text)
    or re.search(r"\b\d{1,2}\s*(januar|februar|marz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember)\b", text)
    or re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text)
  )
  return token_hits >= 2 or (token_hits >= 1 and date_signal)


def source_document_event_evidence(document: str, base_url: str, city: str, target_date: str = "") -> dict:
  links = []
  try:
    links = json_ld_event_links(document, base_url) + iter_html_links(document, base_url)
  except Exception:
    links = []
  seen: set[str] = set()
  detail_links = []
  dated_links = []
  city_key = city_alias_key(city).replace(" ", "")
  source_stub = {"name": "", "sourceKind": "venue-calendar", "trustLevel": "medium", "scope": "city"}
  for link in links:
    url = str(link.get("url") or "")
    key = normalize_url_key(url)
    if not key or key in seen:
      continue
    seen.add(key)
    text = normalize_key(f"{link.get('title', '')} {url}")
    has_city = not city_key or city_key in text.replace(" ", "")
    has_detail = link_has_detail_shape(url) or str(link.get("candidateKind") or "") == "jsonld-event"
    has_date = bool(link.get("startDate")) or event_text_has_date_signal(f"{link.get('title', '')} {url}")
    score = event_link_score(link, source_stub, city, target_date or date.today().isoformat())
    if has_detail and (has_city or has_date or score >= 28):
      detail_links.append({"title": str(link.get("title") or "")[:100], "url": url, "score": score})
    if has_date:
      dated_links.append({"title": str(link.get("title") or "")[:100], "url": url})
  detail_links.sort(key=lambda item: item.get("score", 0), reverse=True)
  return {
    "linkCount": len(seen),
    "eventDetailLinkCount": len(detail_links),
    "datedLinkCount": len(dated_links),
    "sampleEventLinks": detail_links[:5],
  }


def source_validation_rejection(source: dict, city: str, document: str, evidence: dict | None = None) -> str:
  url = str(source.get("url") or "")
  parsed = urlparse(url)
  if parsed.scheme not in {"http", "https"} or not parsed.netloc:
    return "invalid_url"
  if str(source.get("scope") or "").lower() == "national":
    return "national_scope"
  if city_alias_key(city) not in {city_alias_key(item) for item in source.get("cities", []) if str(item).strip()}:
    return "wrong_city"
  if not source.get("name"):
    return "missing_name"
  path_key = normalize_key(parsed.path)
  if path_key in {"", "home", "start", "index"} and source_quality_bucket(source) in {"event_platform", "national_discovery", "other"}:
    return "generic_homepage"
  if any(token in path_key for token in ("impressum", "datenschutz", "privacy", "login", "jobs", "kontakt")):
    return "admin_page"
  if not document or len(document) < 160:
    return "empty_or_tiny_page"
  if not source_document_has_event_signal(document):
    return "no_event_or_program_signal"
  bucket = source_quality_bucket(source)
  if bucket in {"official_baseline", "event_platform", "local_curated"}:
    signal = evidence or {}
    if int(signal.get("eventDetailLinkCount") or 0) <= 0 and int(signal.get("datedLinkCount") or 0) <= 0:
      return "no_event_detail_links"
  text = normalize_key(re.sub(r"<[^>]+>", " ", document[:60000]))
  source_text_value = normalize_key(f"{source.get('name')} {source.get('evidenceSummary')} {source.get('sourceKind')}")
  city_key = city_alias_key(city)
  if city_key and city_key not in text and city_key not in source_text_value:
    location_scope = normalize_key(source.get("locationScope"))
    if location_scope not in {"city core", "city-core"}:
      return "city_scope_not_evident"
  return ""


def validate_city_research_sources(city: str, sources: list[dict], target_date: str = "") -> dict:
  accepted = []
  rejected = []
  for source in sources:
    try:
      document = fetch_text(str(source.get("url") or ""), timeout=4.0)
      evidence = source_document_event_evidence(document, str(source.get("url") or ""), city, target_date)
      reason = source_validation_rejection(source, city, document, evidence)
    except Exception as exc:
      document = ""
      evidence = {}
      reason = f"fetch_failed:{type(exc).__name__}"
    if reason:
      row = dict(source)
      row["rejectionReason"] = reason
      if evidence:
        row["validationSignals"] = evidence
      rejected.append(row)
      continue
    row = dict(source)
    row["validatedAt"] = now_iso()
    row["validationEvidence"] = "Fetched source page and confirmed city-scoped event/program signals."
    row["validationSignals"] = evidence
    row.update(source_quality_profile(row))
    accepted.append(row)
  selected = select_sources_for_research(accepted, len(accepted))
  return {
    "schema": "datepipe.city_research_validation.v1",
    "city": city,
    "generatedAt": now_iso(),
    "validatedCount": len(accepted),
    "rejectedCount": len(rejected),
    "acceptedSources": selected,
    "rejectedSources": rejected,
    "sourceMix": source_mix_summary(selected),
  }


def upsert_discovery_sources(city: str, sources: list[dict]) -> dict:
  if not sources:
    return {"promoted": 0, "updated": 0, "skipped": 0}
  catalog = load_discovery_source_catalog()
  rows = [row for row in catalog.get("sources", []) if isinstance(row, dict)]
  existing_by_key = {normalize_url_key(row.get("url")): index for index, row in enumerate(rows)}
  promoted = 0
  updated = 0
  skipped = 0
  for source in sources:
    key = normalize_url_key(source.get("url"))
    if not key:
      skipped += 1
      continue
    row = deepcopy(source)
    row.pop("sourceQualityBucket", None)
    row.pop("sourceQualityScore", None)
    row["cities"] = sorted({city, *[str(item) for item in row.get("cities", []) if str(item).strip()]})
    if key in existing_by_key:
      index = existing_by_key[key]
      merged = {**rows[index], **row}
      rows[index] = merged
      updated += 1
    else:
      existing_by_key[key] = len(rows)
      rows.append(row)
      promoted += 1
  catalog["schema"] = catalog.get("schema") or "datepipe.discovery_sources.v1"
  catalog["updatedAt"] = date.today().isoformat()
  catalog["sources"] = rows
  write_json(DISCOVERY_SOURCES_PATH, catalog)
  return {"promoted": promoted, "updated": updated, "skipped": skipped}


def enrich_event_images(events: list[dict], limit: int = 48) -> int:
  resolved = 0
  for event in events[:limit]:
    if event.get("imageUrl") or not event.get("eventUrl"):
      continue
    image_url = resolve_page_image(
      str(event.get("eventUrl") or ""),
      "",
      f"{event.get('title', '')} {event.get('city', '')} {event.get('sourceName', '')}",
    )
    if image_url:
      event["imageUrl"] = image_url
      resolved += 1
  return resolved


def extract_city_events_with_sources(city: str, target_date: str, promoted_sources: list[dict]) -> dict:
  try:
    return extract_city_events(city, target_date, force=True, source_override=promoted_sources)
  except TypeError as exc:
    if "source_override" not in str(exc):
      raise
    return extract_city_events(city, target_date, force=True)


def process_completed_city_research_job(job: dict, *, force: bool = False) -> dict:
  if not job or job.get("status") != "completed":
    return job
  if job.get("integration") and not force:
    return job
  city = str(job.get("city") or "").strip()
  target_date = str(job.get("targetDate") or date.today().isoformat())[:10]
  staging = parse_city_research_sources(job.get("report") or job.get("structuredReport") or {}, city)
  write_json(city_research_runtime_path(city, "staging"), staging)
  validation = validate_city_research_sources(city, staging.get("sources", []), target_date)
  write_json(city_research_runtime_path(city, "validation"), validation)
  promoted_sources = validation.get("acceptedSources", [])
  promotion = upsert_discovery_sources(city, promoted_sources)
  PipelineStore(PIPELINE_DB_PATH).register_sources(city, promoted_sources)
  promoted_count = int(promotion.get("promoted", 0)) + int(promotion.get("updated", 0))
  source_mix = validation.get("sourceMix", {})
  source_readiness = city_source_readiness_status(
    accepted_count=promoted_count,
    source_mix=source_mix,
    min_sources=CITY_RESEARCH_MIN_PROMOTED_SOURCES,
  )
  extraction = {"qualityGateStatus": "not_run", "eventCount": 0}
  image_count = 0
  if source_readiness == "ready":
    extraction = extract_city_events_with_sources(city, target_date, promoted_sources)
    image_count = enrich_event_images(extraction.get("events", []))
    if image_count:
      PipelineStore(PIPELINE_DB_PATH).upsert_events(extraction.get("events", []), city=city, target_date=target_date)
      write_json(event_extraction_path(city, target_date), extraction)
  extraction_status = str(extraction.get("qualityGateStatus") or "")
  if source_readiness != "ready":
    gate_status = "source_validation_failed" if promoted_count == 0 else source_readiness
    status = "source_validation_failed" if promoted_count == 0 else "ready_needs_review"
  elif extraction_status in {"passed", "thin_results"}:
    gate_status = extraction_status
    status = "ready"
  else:
    gate_status = extraction_status or "ready_needs_review"
    status = "ready_needs_review"
  job["status"] = status
  job["integratedAt"] = now_iso()
  job["qualityGateStatus"] = gate_status
  job["integration"] = {
    "schema": "datepipe.city_research_integration.v1",
    "city": city,
    "targetDate": target_date,
    "stagedSourceCount": len(staging.get("sources", [])),
    "validatedSourceCount": validation.get("validatedCount", 0),
    "rejectedSourceCount": validation.get("rejectedCount", 0),
    "promotedSourceCount": promoted_count,
    "promotion": promotion,
    "sourceMix": source_mix,
    "sourceReadinessStatus": source_readiness,
    "eventExtraction": {
      "qualityGateStatus": extraction.get("qualityGateStatus"),
      "eventCount": extraction.get("eventCount", 0),
      "imageResolvedCount": image_count,
    },
    "stagingPath": display_runtime_path(city_research_runtime_path(city, "staging")),
    "validationPath": display_runtime_path(city_research_runtime_path(city, "validation")),
  }
  if promoted_count == 0:
    job["message"] = "City research finished, but no sources passed DatePipe validation."
  elif status == "ready":
    job["message"] = "City research is integrated. DatePipe has promoted sources and built the first event digest."
  elif source_readiness != "ready":
    job["message"] = "City research found sources, but the source mix is not strong enough for automatic readiness."
  else:
    job["message"] = "City research is integrated, but the first digest still needs review before it is strong enough."
  return save_city_research_job(job)


def load_city_research_job(city: str) -> dict | None:
  path = city_research_path(city)
  if not path.exists():
    return None
  try:
    payload = load_json(path)
  except Exception:
    return None
  return payload if isinstance(payload, dict) else None


def save_city_research_job(job: dict) -> dict:
  CITY_RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
  write_json_atomic(city_research_path(str(job.get("city") or "")), job)
  try:
    PipelineStore(PIPELINE_DB_PATH).record_research_job(job)
  except Exception:
    pass
  return job


def public_city_research_job(job: dict | None) -> dict | None:
  if not job:
    return None
  deadline = parse_iso_datetime(job.get("deadlineAt"))
  remaining = max(0, int((deadline - datetime.now(UTC)).total_seconds())) if deadline else 0
  public = {key: job.get(key) for key in (
    "schema", "id", "city", "status", "provider", "model", "startedAt", "deadlineAt",
    "completedAt", "targetDate", "sourceStatus", "responseId", "message", "qualityGateStatus",
  ) if key in job}
  public["remainingSeconds"] = remaining
  public["etaMinutes"] = max(0, (remaining + 59) // 60)
  if job.get("errorType"):
    public["errorType"] = job.get("errorType")
  if job.get("deepResearchFallbackReason"):
    public["deepResearchFallbackReason"] = job.get("deepResearchFallbackReason")
  if isinstance(job.get("costPolicy"), dict):
    public["costPolicy"] = {
      "deepResearchEnabled": bool(job["costPolicy"].get("deepResearchEnabled")),
      "maxOutputTokens": int(job["costPolicy"].get("maxOutputTokens") or 0),
    }
  if job.get("report"):
    public["reportReady"] = True
  if isinstance(job.get("integration"), dict):
    integration = job["integration"]
    public["integration"] = {
      "stagedSourceCount": integration.get("stagedSourceCount", 0),
      "validatedSourceCount": integration.get("validatedSourceCount", 0),
      "rejectedSourceCount": integration.get("rejectedSourceCount", 0),
      "promotedSourceCount": integration.get("promotedSourceCount", 0),
      "sourceReadinessStatus": integration.get("sourceReadinessStatus"),
      "eventExtraction": integration.get("eventExtraction", {}),
    }
  if job.get("integratedAt"):
    public["integratedAt"] = job.get("integratedAt")
  return public


def clamp_score(value: float | int) -> int:
  return max(0, min(100, int(round(float(value)))))


def quality_band(score: int) -> str:
  if score >= 85:
    return "excellent"
  if score >= 75:
    return "strong"
  if score >= 60:
    return "usable"
  if score >= 40:
    return "thin"
  return "poor"


def quality_dimension(name: str, score: float | int, note: str, *, weight: int = 1) -> dict:
  clean_score = clamp_score(score)
  return {
    "name": name,
    "score": clean_score,
    "band": quality_band(clean_score),
    "weight": weight,
    "note": note,
  }


def ratio_score(count: int, target: int) -> int:
  if target <= 0:
    return 100
  return clamp_score((max(0, count) / target) * 100)


def option_quality_report(
  options: list[dict],
  *,
  city: str = "",
  target_date: str = "",
  source: str = "",
  digest: dict | None = None,
) -> dict:
  rows = [item for item in options if isinstance(item, dict)]
  count = len(rows)
  valid_urls = 0
  relevant_urls = 0
  complete_rows = 0
  actionable_rows = 0
  special_total = 0
  special_seen = 0
  generic_penalties = 0
  categories = set()
  stretches = set()
  source_names = set()
  date_matches = 0
  for row in rows:
    categories.add(str(row.get("category") or ""))
    stretches.add(str(row.get("stretch") or ""))
    source_names.add(str(row.get("sourceName") or row.get("sourceIdeaId") or ""))
    event_url = str(row.get("eventUrl") or "")
    source_name = str(row.get("sourceName") or "")
    if event_url.startswith(("http://", "https://")):
      valid_urls += 1
      ok, _reason = proposal_link_relevance(str(row.get("title") or ""), event_url, source_name, city)
      if ok:
        relevant_urls += 1
    if target_date:
      requested = parse_date_or_none(target_date)
      proposed = parse_date_or_none(row.get("dateChoice"))
      if requested and proposed and abs((proposed - requested).days) <= 2:
        date_matches += 1
    present = 0
    for key in ("title", "category", "duration", "timing", "stretch", "budget", "why", "visibleHint", "dateChoice", "eventUrl", "sourceName"):
      value = row.get(key)
      if isinstance(value, str):
        present += 1 if value.strip() else 0
      elif value:
        present += 1
    if present >= 9 and len(str(row.get("why") or "")) >= 35:
      complete_rows += 1
    if row.get("eventUrl") and row.get("visibleHint") and (row.get("steps") or row.get("backup") or row.get("sourceName")):
      actionable_rows += 1
    raw_special = row.get("specialnessScore")
    if isinstance(raw_special, (int, float)) or str(raw_special or "").isdigit():
      special_total += int(raw_special)
      special_seen += 1
    elif row.get("specialness") or row.get("whySpecial") or row.get("whyNotGeneric"):
      special_total += 70
      special_seen += 1
    elif str(row.get("variety") or "") in {"obscure", "fresh"} or str(row.get("mode") or "") == "solo-secret" or str(row.get("stretch") or "") in {"safe-stretch", "outside-zone", "spicy"}:
      special_total += 72
      special_seen += 1
    if row.get("genericRisk") in {"high", "generic", "too-generic"}:
      generic_penalties += 1
  matching_review = digest.get("matchingReview") if isinstance(digest, dict) else {}
  dimensions = [
    quality_dimension("enough_choices", ratio_score(count, 6), f"{count} suggestions returned", weight=2),
    quality_dimension("specificity", ratio_score(complete_rows, max(1, count)), f"{complete_rows}/{count} suggestions have concrete planning fields", weight=2),
    quality_dimension("provenance", ratio_score(relevant_urls or valid_urls, max(1, count)), f"{valid_urls}/{count} have real links; {relevant_urls}/{count} match city/source/title", weight=2),
    quality_dimension("actionability", ratio_score(actionable_rows, max(1, count)), f"{actionable_rows}/{count} include enough to act without browsing", weight=2),
    quality_dimension("specialness", (special_total / special_seen if special_seen else 45) - generic_penalties * 8, f"{special_seen}/{count} carry non-generic signals", weight=2),
    quality_dimension("variety", min(100, (len(categories) * 20) + (len(stretches) * 15) + (len(source_names) * 8)), f"{len(categories)} styles, {len(stretches)} surprise levels, {len(source_names)} sources", weight=1),
  ]
  if target_date:
    dimensions.append(quality_dimension("date_window_fit", ratio_score(date_matches, max(1, count)), f"{date_matches}/{count} suggestions land in the requested planning window", weight=1))
  if isinstance(matching_review, dict) and matching_review:
    digest_score = (float(matching_review.get("averageReadinessScore") or 0) * 0.65) + (float(matching_review.get("readyRatio") or 0) * 100 * 0.35)
    dimensions.append(quality_dimension("catalog_readiness", digest_score, f"{matching_review.get('readyCount', 0)}/{matching_review.get('candidateCount', 0)} digest candidates ready", weight=2))
  weighted_total = sum(item["score"] * item["weight"] for item in dimensions)
  total_weight = sum(item["weight"] for item in dimensions) or 1
  score = clamp_score(weighted_total / total_weight)
  flags = []
  if count < 4:
    flags.append("too_few_options")
  if valid_urls < max(1, count // 2):
    flags.append("weak_provenance")
  if complete_rows < max(1, count // 2):
    flags.append("thin_specificity")
  if generic_penalties:
    flags.append("generic_risk")
  if target_date and date_matches < max(1, count // 2):
    flags.append("weak_date_fit")
  return {
    "schema": "datepipe.quality_report.v1",
    "generatedAt": now_iso(),
    "scope": "suggestions",
    "city": city,
    "date": target_date,
    "source": source,
    "score": score,
    "band": quality_band(score),
    "optionCount": count,
    "dimensions": dimensions,
    "flags": flags,
    "summary": f"{quality_band(score)} result quality from {count} suggestions",
  }


def research_quality_report(review: dict, *, city: str = "", target_date: str = "") -> dict:
  sources = [item for item in review.get("sources", []) if isinstance(item, dict)]
  events = [item for item in review.get("events", []) if isinstance(item, dict)]
  validation = review.get("validation") if isinstance(review.get("validation"), dict) else {}
  source_status = review.get("sourceStatus") if isinstance(review.get("sourceStatus"), dict) else {}
  good_buckets = {"venue_primary", "city_calendar", "local_editorial", "official_local", "workshop_provider"}
  strong_sources = [
    source for source in sources
    if str(source.get("sourceQualityBucket") or "") in good_buckets
    or str(source.get("trustLevel") or "") == "high"
  ]
  usable_sources = [source for source in sources if source.get("reviewStatus") != "rejected"]
  dated_events = [event for event in events if str(event.get("targetDate") or event.get("date") or target_date)[:10]]
  special_events = [
    event for event in events
    if int(event.get("specialnessScore") or event.get("extractionScore") or 0) >= 55
  ]
  rejected_count = int(validation.get("rejectedCount") or 0)
  validated_count = int(validation.get("validatedCount") or len(sources))
  dimensions = [
    quality_dimension("source_readiness", 100 if source_status.get("sourceReadinessStatus") == "ready" else ratio_score(len(usable_sources), 6), f"{len(usable_sources)} usable sources", weight=2),
    quality_dimension("source_depth", ratio_score(len(strong_sources), 4), f"{len(strong_sources)} strong local sources", weight=2),
    quality_dimension("event_depth", ratio_score(len(events), 6), f"{len(events)} event leads in queue", weight=2),
    quality_dimension("event_specificity", ratio_score(len(dated_events), max(1, len(events))), f"{len(dated_events)}/{len(events)} leads have date context", weight=1),
    quality_dimension("specialness", ratio_score(len(special_events), max(1, len(events))), f"{len(special_events)}/{len(events)} leads look non-generic", weight=2),
    quality_dimension("validation_yield", ratio_score(validated_count, max(1, validated_count + rejected_count)), f"{validated_count} validated / {rejected_count} rejected", weight=1),
  ]
  weighted_total = sum(item["score"] * item["weight"] for item in dimensions)
  total_weight = sum(item["weight"] for item in dimensions) or 1
  score = clamp_score(weighted_total / total_weight)
  flags = []
  if len(strong_sources) < 2:
    flags.append("needs_better_sources")
  if len(events) < 3:
    flags.append("thin_event_leads")
  if rejected_count > validated_count:
    flags.append("high_rejection_rate")
  return {
    "schema": "datepipe.quality_report.v1",
    "generatedAt": now_iso(),
    "scope": "research",
    "city": city,
    "date": target_date,
    "score": score,
    "band": quality_band(score),
    "sourceCount": len(sources),
    "eventCount": len(events),
    "dimensions": dimensions,
    "flags": flags,
    "summary": f"{quality_band(score)} research quality from {len(sources)} sources and {len(events)} leads",
  }


def catalog_quality_report(state: dict, *, city: str = "") -> dict:
  clean_city = (city or state.get("profile", {}).get("homeBase") or "Vienna").strip() or "Vienna"
  ideas = [item for item in state.get("ideas", []) if isinstance(item, dict)]
  candidates = []
  categories = set()
  sourced = 0
  actionable = 0
  special = 0
  seen = set()
  for idea in ideas:
    event_date = parse_date_or_none(idea.get("eventDate"))
    if event_date and event_date < date.today():
      continue
    localized = localize_idea_for_city(idea, clean_city)
    title = str(localized.get("title") or idea.get("title") or "").strip()
    event_url = str(localized.get("eventUrl") or idea.get("eventUrl") or "").strip()
    key = (title.lower(), event_url.lower())
    if not title or key in seen:
      continue
    seen.add(key)
    categories.add(str(idea.get("category") or "standard"))
    if event_url.startswith(("http://", "https://")):
      sourced += 1
    if localized.get("steps") or idea.get("steps"):
      actionable += 1
    tags = set(normalize_tags(idea.get("tags", [])) + normalize_tags(idea.get("stretch", "")) + normalize_tags(idea.get("category", "")))
    if tags & {"secret", "ticketed", "outside-zone", "spicy", "different", "accomplice", "seasonal", "romantic"}:
      special += 1
    candidates.append(idea)
  try:
    review = PipelineStore(PIPELINE_DB_PATH).review_queue(clean_city, target_date=None)
  except Exception:
    review = {}
  events = [item for item in review.get("events", []) if isinstance(item, dict)]
  usable_events = [
    event for event in events
    if (event.get("researchState", {}) if isinstance(event.get("researchState"), dict) else {}).get("reviewStatus", event.get("reviewStatus", "new")) != "rejected"
  ]
  dimensions = [
    quality_dimension("known_option_depth", ratio_score(len(candidates), 12), f"{len(candidates)} local catalog ideas available", weight=2),
    quality_dimension("category_coverage", ratio_score(len(categories), 6), f"{len(categories)} date styles represented", weight=2),
    quality_dimension("catalog_provenance", ratio_score(sourced, max(1, len(candidates))), f"{sourced}/{len(candidates)} ideas have public links", weight=2),
    quality_dimension("catalog_actionability", ratio_score(actionable, max(1, len(candidates))), f"{actionable}/{len(candidates)} ideas include planning steps", weight=2),
    quality_dimension("catalog_specialness", ratio_score(special, max(1, len(candidates))), f"{special}/{len(candidates)} ideas carry surprise or specificity signals", weight=2),
    quality_dimension("stored_event_depth", ratio_score(len(usable_events), 8), f"{len(usable_events)} usable researched event leads stored", weight=1),
  ]
  weighted_total = sum(item["score"] * item["weight"] for item in dimensions)
  total_weight = sum(item["weight"] for item in dimensions) or 1
  score = clamp_score(weighted_total / total_weight)
  flags = []
  if len(candidates) < 8:
    flags.append("thin_catalog")
  if len(categories) < 4:
    flags.append("narrow_catalog")
  if sourced < max(1, len(candidates) // 2):
    flags.append("catalog_needs_links")
  if len(usable_events) < 3:
    flags.append("thin_stored_events")
  return {
    "schema": "datepipe.quality_report.v1",
    "generatedAt": now_iso(),
    "scope": "catalog",
    "city": clean_city,
    "score": score,
    "band": quality_band(score),
    "knownOptionCount": len(candidates),
    "storedEventCount": len(usable_events),
    "dimensions": dimensions,
    "flags": flags,
    "summary": f"{quality_band(score)} catalog quality from {len(candidates)} known ideas and {len(usable_events)} stored leads",
  }


def retrieval_quality_report(options: list[dict], catalog: dict, *, city: str = "", target_date: str = "") -> dict:
  rows = [item for item in options if isinstance(item, dict)]
  catalog_count = int(catalog.get("knownOptionCount") or 0)
  stored_event_count = int(catalog.get("storedEventCount") or 0)
  categories = {str(item.get("category") or "") for item in rows if item.get("category")}
  linked = [item for item in rows if str(item.get("eventUrl") or "").startswith(("http://", "https://"))]
  special = [
    item for item in rows
    if str(item.get("variety") or "") in {"fresh", "obscure"}
    or str(item.get("stretch") or "") in {"safe-stretch", "outside-zone", "spicy"}
    or item.get("specialnessScore")
  ]
  dimensions = [
    quality_dimension("catalog_usage", ratio_score(len(rows), min(6, max(1, catalog_count))), f"{len(rows)} options returned from {catalog_count} known ideas", weight=2),
    quality_dimension("retrieved_variety", ratio_score(len(categories), min(5, max(1, catalog_count))), f"{len(categories)} date styles in returned options", weight=2),
    quality_dimension("retrieved_provenance", ratio_score(len(linked), max(1, len(rows))), f"{len(linked)}/{len(rows)} selected options have links", weight=2),
    quality_dimension("retrieved_specialness", ratio_score(len(special), max(1, len(rows))), f"{len(special)}/{len(rows)} selected options feel non-generic", weight=2),
    quality_dimension("research_reuse", ratio_score(stored_event_count, 6), f"{stored_event_count} stored event leads are available for this city", weight=1),
  ]
  weighted_total = sum(item["score"] * item["weight"] for item in dimensions)
  total_weight = sum(item["weight"] for item in dimensions) or 1
  score = clamp_score(weighted_total / total_weight)
  flags = []
  if catalog_count >= 6 and len(rows) < 4:
    flags.append("under_retrieved_catalog")
  if len(categories) < 3:
    flags.append("retrieval_too_narrow")
  if len(special) < max(1, len(rows) // 2):
    flags.append("retrieval_too_generic")
  return {
    "schema": "datepipe.quality_report.v1",
    "generatedAt": now_iso(),
    "scope": "retrieval",
    "city": city,
    "date": target_date,
    "score": score,
    "band": quality_band(score),
    "dimensions": dimensions,
    "flags": flags,
    "summary": f"{quality_band(score)} retrieval quality from {len(rows)} returned options",
  }


def planning_readiness_report(state: dict) -> dict:
  slots = effective_slots(state)
  booked_slots = [slot for slot in slots if slot.get("status") in {"booked", "done"}]
  sample = {
    "id": "release-sample",
    "title": "Release readiness sample",
    "dateChoice": (date.today() + timedelta(days=8)).isoformat(),
    "eventUrl": "https://example.test/date",
  }
  existing = make_plan_from_proposal({**sample, "id": "release-existing"})
  candidate = make_plan_from_proposal({**sample, "id": "release-candidate", "dateChoice": (date.today() + timedelta(days=10)).isoformat()})
  conflict_state = {**state, "plans": [existing]}
  conflict_warnings = schedule_conflicts_for_plan(conflict_state, candidate)
  checklist = booking_checklist_from_proposal(sample)
  attachment_ready = "attachments" in candidate and isinstance(candidate.get("attachments"), list)
  dimensions = [
    quality_dimension("calendar_windows", ratio_score(len(slots), 8), f"{len(slots)} upcoming planning windows visible", weight=2),
    quality_dimension("booked_visibility", ratio_score(len(booked_slots), 1), f"{len(booked_slots)} booked windows visible in tracking", weight=1),
    quality_dimension("booking_checklist", 100 if len(checklist) >= 5 else ratio_score(len(checklist), 5), f"{len(checklist)} default booking tasks generated", weight=2),
    quality_dimension("same_week_guardrail", 100 if conflict_warnings else 0, f"{len(conflict_warnings)} same-week warnings produced in preflight", weight=2),
    quality_dimension("attachment_readiness", 100 if attachment_ready else 0, "plan records can hold tickets, links, and notes", weight=1),
  ]
  weighted_total = sum(item["score"] * item["weight"] for item in dimensions)
  total_weight = sum(item["weight"] for item in dimensions) or 1
  score = clamp_score(weighted_total / total_weight)
  flags = []
  if not conflict_warnings:
    flags.append("missing_conflict_guardrail")
  if not attachment_ready:
    flags.append("missing_attachment_store")
  if len(slots) < 4:
    flags.append("thin_calendar_windows")
  return {
    "schema": "datepipe.quality_report.v1",
    "generatedAt": now_iso(),
    "scope": "planning_readiness",
    "score": score,
    "band": quality_band(score),
    "dimensions": dimensions,
    "flags": flags,
    "summary": f"{quality_band(score)} planning readiness across calendar, conflict, checklist, and attachment support",
  }


def release_evaluation_report(state: dict, *, cities: list[str], weeks: int = 4) -> dict:
  today = date.today()
  evaluation_start = today + timedelta(days=1) if today.weekday() == 6 else today
  first_week, _ = week_bounds(evaluation_start)
  rows = []
  planning = planning_readiness_report(state)
  for city in cities:
    clean_city = city.strip() or "Vienna"
    catalog = catalog_quality_report(state, city=clean_city)
    for index in range(max(1, min(12, weeks))):
      week_start = first_week + timedelta(days=index * 7)
      week_end = week_start + timedelta(days=6)
      target_date = release_evaluation_target_date(today, week_start, week_end).isoformat()
      scoped_state = deepcopy(state)
      scoped_state["profile"] = {**scoped_state.get("profile", {}), "homeBase": clean_city}
      options = release_evaluation_options(scoped_state, search_date=target_date)
      source = "local-release-evaluation"
      suggestions = option_quality_report(options, city=clean_city, target_date=target_date, source=source)
      retrieval = retrieval_quality_report(options, catalog, city=clean_city, target_date=target_date)
      review = city_research_review_payload(clean_city, target_date, date_from=week_start.isoformat(), date_to=week_end.isoformat())
      research = review.get("qualityReport", research_quality_report(review, city=clean_city, target_date=target_date))
      combined_score = clamp_score(
        (float(suggestions.get("score") or 0) * 0.30)
        + (float(retrieval.get("score") or 0) * 0.20)
        + (float(catalog.get("score") or 0) * 0.20)
        + (float(research.get("score") or 0) * 0.20)
        + (float(planning.get("score") or 0) * 0.10)
      )
      flags = sorted(set(
        (suggestions.get("flags") or [])
        + (retrieval.get("flags") or [])
        + (catalog.get("flags") or [])
        + (research.get("flags") or [])
        + (planning.get("flags") or [])
      ))
      rows.append({
        "city": clean_city,
        "weekStart": week_start.isoformat(),
        "weekEnd": week_end.isoformat(),
        "label": week_label(week_start, week_end),
        "score": combined_score,
        "band": quality_band(combined_score),
        "suggestionScore": suggestions.get("score", 0),
        "retrievalScore": retrieval.get("score", 0),
        "catalogScore": catalog.get("score", 0),
        "researchScore": research.get("score", 0),
        "planningScore": planning.get("score", 0),
        "optionCount": suggestions.get("optionCount", 0),
        "source": source,
        "flags": flags,
      })
  score = clamp_score(sum(float(row["score"]) for row in rows) / max(1, len(rows)))
  weak_rows = [row for row in rows if row["score"] < 70 or row["flags"]]
  release_issues = release_blocking_issues(rows, planning)
  release_gate = release_gate_status(score, release_issues)
  return {
    "schema": "datepipe.release_evaluation.v1",
    "generatedAt": now_iso(),
    "scope": "city_week_matrix",
    "score": score,
    "band": quality_band(score),
    "releaseGate": release_gate,
    "readyForRelease": release_gate == "ready",
    "cityCount": len(cities),
    "weekCount": max(1, min(12, weeks)),
    "caseCount": len(rows),
    "summary": f"{quality_band(score)} release quality across {len(rows)} city/week cases",
    "components": {
      "planning": planning,
      "catalogs": [catalog_quality_report(state, city=city.strip() or "Vienna") for city in cities],
    },
    "rows": rows,
    "weakRows": weak_rows[:12],
    "blockingIssues": release_issues[:12],
  }


RELEASE_FLAG_ACTIONS = {
  "too_few_options": ("blocker", "Backfill generated suggestions until each release case returns at least six options."),
  "weak_provenance": ("blocker", "Add or repair source links so most suggestions can be verified quickly."),
  "thin_specificity": ("blocker", "Add concrete timing, budget, why, hint, source, and planning fields to returned options."),
  "weak_date_fit": ("blocker", "Keep generated options inside the requested planning window."),
  "under_retrieved_catalog": ("blocker", "Return enough options from the known local catalog before falling back to web or AI output."),
  "retrieval_too_narrow": ("watch", "Widen returned date styles so a release city is not dominated by one category."),
  "retrieval_too_generic": ("watch", "Prefer fresher, source-grounded, or surprise-fit ideas over generic defaults."),
  "thin_catalog": ("blocker", "Add at least eight localized catalog ideas for this city."),
  "narrow_catalog": ("watch", "Broaden the city's date-style coverage."),
  "catalog_needs_links": ("watch", "Attach public source links to more catalog ideas."),
  "thin_stored_events": ("watch", "Collect or approve more current event leads for this city."),
  "needs_better_sources": ("watch", "Add stronger venue, calendar, editorial, or workshop sources."),
  "thin_event_leads": ("watch", "Collect more event leads for the evaluated week."),
  "high_rejection_rate": ("blocker", "Fix source selection or extraction quality before relying on this city."),
  "missing_conflict_guardrail": ("blocker", "Restore same-week plan conflict warnings."),
  "missing_attachment_store": ("blocker", "Restore plan attachment support for tickets, links, and notes."),
  "thin_calendar_windows": ("watch", "Seed enough upcoming planning windows for release QA."),
}


def release_blocking_issues(rows: list[dict], planning: dict) -> list[dict]:
  issues = []
  seen = set()
  for row in rows:
    row_score = int(row.get("score") or 0)
    if row_score < 70:
      key = (row.get("city"), row.get("weekStart"), "low_release_score")
      seen.add(key)
      issues.append({
        "code": "low_release_score",
        "severity": "blocker",
        "city": row.get("city", ""),
        "weekStart": row.get("weekStart", ""),
        "weekEnd": row.get("weekEnd", ""),
        "score": row_score,
        "message": f"{row.get('city', 'City')} release case scored {row_score}.",
        "action": "Fix the lowest component scores before release.",
      })
    for flag in row.get("flags") or []:
      severity, action = RELEASE_FLAG_ACTIONS.get(str(flag), ("watch", "Review this release flag before launch."))
      key = (row.get("city"), row.get("weekStart"), str(flag))
      if key in seen:
        continue
      seen.add(key)
      issues.append({
        "code": str(flag),
        "severity": severity,
        "city": row.get("city", ""),
        "weekStart": row.get("weekStart", ""),
        "weekEnd": row.get("weekEnd", ""),
        "score": row_score,
        "message": f"{row.get('city', 'City')} {row.get('label', '')}: {str(flag).replace('_', ' ')}.",
        "action": action,
      })
  for flag in planning.get("flags") or []:
    severity, action = RELEASE_FLAG_ACTIONS.get(str(flag), ("watch", "Review planning readiness before launch."))
    key = ("planning", "", str(flag))
    if key in seen:
      continue
    seen.add(key)
    issues.append({
      "code": str(flag),
      "severity": severity,
      "city": "",
      "weekStart": "",
      "weekEnd": "",
      "score": int(planning.get("score") or 0),
      "message": f"Planning readiness: {str(flag).replace('_', ' ')}.",
      "action": action,
    })
  severity_order = {"blocker": 0, "watch": 1}
  return sorted(issues, key=lambda item: (severity_order.get(str(item.get("severity")), 2), int(item.get("score") or 0)))


def release_gate_status(score: int, issues: list[dict]) -> str:
  has_blocker = any(item.get("severity") == "blocker" for item in issues)
  if has_blocker or score < 75:
    return "blocked"
  if issues or score < 85:
    return "needs_attention"
  return "ready"


def release_evaluation_target_date(today: date, week_start: date, week_end: date) -> date:
  friday = week_start + timedelta(days=4)
  saturday = week_start + timedelta(days=5)
  if week_end < today:
    return week_end
  if week_start <= today <= week_end:
    if today <= friday:
      return friday
    if today <= saturday:
      return saturday
    return today
  return friday


def city_research_review_payload(city: str, target_date: str = "", *, date_from: str = "", date_to: str = "") -> dict:
  target_date = target_date[:10] if target_date else ""
  store_payload = PipelineStore(PIPELINE_DB_PATH).review_queue(
    city,
    target_date=None if date_from and date_to else (target_date or None),
    date_from=date_from[:10] if date_from else None,
    date_to=date_to[:10] if date_to else None,
  )
  job = load_city_research_job(city)
  staging = {}
  validation = {}
  for name, path in (
    ("staging", city_research_runtime_path(city, "staging")),
    ("validation", city_research_runtime_path(city, "validation")),
  ):
    if path.exists():
      try:
        if name == "staging":
          staging = load_json(path)
        else:
          validation = load_json(path)
      except Exception:
        pass
  rejected_sources = []
  for source in validation.get("rejectedSources", []) if isinstance(validation, dict) else []:
    rejected_sources.append({
      "id": source.get("id", ""),
      "name": source.get("name", ""),
      "url": source.get("url", ""),
      "rejectionReason": source.get("rejectionReason", ""),
      "sourceKind": source.get("sourceKind", ""),
      "sourceQualityBucket": source.get("sourceQualityBucket", ""),
    })
  payload = {
    **store_payload,
    "job": public_city_research_job(job),
    "sourceStatus": city_source_status(city),
    "staging": {
      "parseStatus": staging.get("parseStatus", ""),
      "sourceCount": len(staging.get("sources", [])) if isinstance(staging, dict) else 0,
      "rejectedSourceCount": staging.get("rejectedSourceCount", 0) if isinstance(staging, dict) else 0,
      "qualityNotes": staging.get("qualityNotes", []) if isinstance(staging, dict) else [],
    },
    "validation": {
      "validatedCount": validation.get("validatedCount", 0) if isinstance(validation, dict) else 0,
      "rejectedCount": validation.get("rejectedCount", 0) if isinstance(validation, dict) else 0,
      "sourceMix": validation.get("sourceMix", {}) if isinstance(validation, dict) else {},
      "rejectedSources": rejected_sources[:80],
    },
  }
  payload["qualityReport"] = research_quality_report(payload, city=city, target_date=target_date)
  return payload


def openai_api_key() -> str:
  return os.environ.get("OPENAI_API_KEY", "").strip()


def env_flag(name: str, default: bool = False) -> bool:
  raw = os.environ.get(name)
  if raw is None:
    return default
  return raw.strip().lower() in {"1", "true", "yes", "on"}


def city_research_deep_research_enabled() -> bool:
  return env_flag("DATEPIPE_ENABLE_DEEP_RESEARCH", False)


def city_research_max_output_tokens() -> int:
  raw = os.environ.get("DATEPIPE_DEEP_RESEARCH_MAX_OUTPUT_TOKENS", "").strip()
  if not raw:
    return CITY_RESEARCH_DEFAULT_MAX_OUTPUT_TOKENS
  try:
    value = int(raw)
  except ValueError:
    return CITY_RESEARCH_DEFAULT_MAX_OUTPUT_TOKENS
  return max(500, min(value, 9000))


def city_research_cost_policy() -> dict:
  return {
    "deepResearchEnabled": city_research_deep_research_enabled(),
    "deepResearchOptInEnv": "DATEPIPE_ENABLE_DEEP_RESEARCH",
    "maxOutputTokens": city_research_max_output_tokens(),
    "maxOutputTokensEnv": "DATEPIPE_DEEP_RESEARCH_MAX_OUTPUT_TOKENS",
  }


def run_local_city_scout_job(job: dict, profile: dict | None = None) -> dict:
  job.update({
    "provider": "local-web-scout",
    "status": "scouting_sources",
    "message": "DatePipe is scouting public city sources without starting paid deep research.",
    "costPolicy": city_research_cost_policy(),
  })
  save_city_research_job(job)
  try:
    report = local_city_scout_report(str(job.get("city") or ""), str(job.get("targetDate") or ""), profile)
    job["completedAt"] = now_iso()
    job["status"] = "completed"
    job["report"] = json.dumps(report, ensure_ascii=False)
    job["qualityGateStatus"] = "needs_source_validation"
    job["message"] = "Local city scout finished. DatePipe is validating sources before promotion."
    return process_completed_city_research_job(job, force=True)
  except Exception as exc:
    job.update({
      "provider": "local-web-scout",
      "status": "queued",
      "message": "City scout is queued locally. The cheap source scout could not complete and no paid research call was started.",
      "errorType": type(exc).__name__,
      "costPolicy": city_research_cost_policy(),
    })
    return job


def start_openai_city_research(job: dict, prompt: str) -> dict:
  if not city_research_deep_research_enabled():
    job.update({
      "provider": "local",
      "status": "queued",
      "message": "City scout queued locally. API deep research is disabled by default to avoid unbounded token spend.",
      "errorType": "deep_research_disabled",
      "costPolicy": city_research_cost_policy(),
    })
    return job
  api_key = openai_api_key()
  ok, preflight = preflight_openai_key(api_key)
  if not ok:
    job.update({
      "provider": "local",
      "status": "queued",
      "message": "City scout queued locally. Deep research will start when an OpenAI key is configured.",
      "errorType": preflight,
      "costPolicy": city_research_cost_policy(),
    })
    return job
  model = os.environ.get("DATEPIPE_DEEP_RESEARCH_MODEL", "o4-mini-deep-research")
  max_output_tokens = city_research_max_output_tokens()
  payload = {
    "model": model,
    "input": prompt,
    "tools": [{"type": "web_search_preview"}],
    "background": True,
    "store": True,
    "max_output_tokens": max_output_tokens,
  }
  request = Request(
    OPENAI_RESPONSES_URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={
      "Authorization": f"Bearer {api_key}",
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urlopen(request, timeout=25) as response:
      data = json.loads(response.read().decode("utf-8"))
    job.update({
      "provider": "openai-deep-research",
      "model": model,
      "status": str(data.get("status") or "in_progress"),
      "responseId": data.get("id"),
      "message": "Deep research started. We are building this city source pack.",
      "rawStatus": data.get("status"),
      "costPolicy": city_research_cost_policy(),
    })
  except Exception as exc:
    job.update({
      "provider": "local",
      "status": "queued",
      "model": model,
      "message": "City scout queued locally. Deep research could not start, so the app will keep the request for retry.",
      "errorType": type(exc).__name__,
      "costPolicy": city_research_cost_policy(),
    })
  return job


def refresh_city_research_job(job: dict) -> dict:
  if not job or job.get("provider") != "openai-deep-research" or not job.get("responseId"):
    return process_completed_city_research_job(job)
  if job.get("status") not in {"queued", "in_progress"}:
    return process_completed_city_research_job(job)
  api_key = openai_api_key()
  if not api_key:
    return job
  request = Request(
    f"{OPENAI_RESPONSES_URL}/{job['responseId']}",
    headers={"Authorization": f"Bearer {api_key}"},
    method="GET",
  )
  try:
    with urlopen(request, timeout=20) as response:
      data = json.loads(response.read().decode("utf-8"))
    status = str(data.get("status") or job.get("status") or "")
    job["status"] = status
    job["rawStatus"] = status
    if status == "completed":
      job["completedAt"] = now_iso()
      job["report"] = extract_response_text(data)
      job["message"] = "City research report is ready for source validation."
      job["qualityGateStatus"] = "needs_source_validation"
      job = process_completed_city_research_job(job)
    elif status in {"failed", "cancelled", "incomplete"}:
      job["message"] = "City research did not complete. The request is preserved for retry."
      job["errorType"] = status
    save_city_research_job(job)
  except Exception as exc:
    job["lastPollErrorType"] = type(exc).__name__
    save_city_research_job(job)
  return job


def start_city_research(city: str, target_date: str, profile: dict | None = None, force: bool = False) -> dict:
  existing = load_city_research_job(city)
  if existing and not force and existing.get("status") in {"queued", "in_progress", "completed", "ready", "ready_needs_review", "source_validation_failed"}:
    return refresh_city_research_job(existing)
  started = datetime.now(UTC)
  job = {
    "schema": "datepipe.city_research_job.v1",
    "id": new_id("city-research"),
    "city": city.strip(),
    "targetDate": target_date[:10],
    "status": "queued",
    "provider": "local",
    "startedAt": started.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "deadlineAt": (started + timedelta(seconds=CITY_RESEARCH_SECONDS)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    "sourceStatus": city_source_status(city),
    "message": "This city is not in DatePipe yet. We are adding it to the scout queue.",
    "prompt": city_research_prompt(city, target_date, profile),
    "costPolicy": city_research_cost_policy(),
  }
  if city_research_deep_research_enabled():
    job = start_openai_city_research(job, job["prompt"])
    if job.get("provider") != "openai-deep-research":
      fallback_reason = str(job.get("errorType") or "deep_research_unavailable")
      job["deepResearchFallbackReason"] = fallback_reason
      job.pop("errorType", None)
      job["message"] = "Deep research is unavailable, so DatePipe is falling back to the local source scout."
      job = run_local_city_scout_job(job, profile)
  else:
    job = run_local_city_scout_job(job, profile)
  return save_city_research_job(job)


def source_text(source: dict) -> str:
  return " ".join(str(source.get(key, "")) for key in (
    "name",
    "sourceType",
    "sourceRole",
    "categories",
    "variety",
    "datePipeUse",
    "evidenceSummary",
    "liveVerificationRule",
  )).lower()


def android_category_from_source(source: dict) -> str:
  text = source_text(source)
  if any(token in text for token in ("food", "market", "culinary", "wine", "cooking", "tasting")):
    return "culinary"
  if any(token in text for token in ("outdoor", "hike", "walk", "river", "lake", "sport", "bike", "mountain")):
    return "sportive"
  if any(token in text for token in ("comedy", "cabaret", "dance", "music", "jazz", "concert")):
    return "social"
  if any(token in text for token in ("museum", "gallery", "cinema", "theatre", "festival", "art", "exhibition")):
    return "cultural"
  if "premium" in text:
    return "premium"
  if any(token in text for token in ("weird", "obscure", "local-secret", "unusual")):
    return "different"
  return "standard"


SPECIALNESS_SIGNALS = [
  {
    "id": "fresh-timing",
    "dimension": "freshness",
    "weight": 16,
    "tokens": ("festival", "seasonal", "calendar", "programme", "program", "today", "tonight", "this week", "current", "limited", "premiere", "opening"),
    "reason": "it is tied to a current programme or limited window",
  },
  {
    "id": "local-specific",
    "dimension": "locality",
    "weight": 15,
    "tokens": ("local-secret", "neighbourhood", "municipal", "stadt", "city", "market", "altstadt", "danube", "donau", "prater", "gürtel", "guertel", "mq", "wien", "vienna", "mur", "inn", "wörthersee", "woerthersee"),
    "reason": "it is anchored in a specific local place or city pattern",
  },
  {
    "id": "routine-breaker",
    "dimension": "novelty",
    "weight": 14,
    "tokens": ("weird", "obscure", "experimental", "workshop", "craft", "ceramic", "photography", "architecture", "contemporary", "guided", "behind the scenes", "special tour", "lab"),
    "reason": "it creates a gentle break from the couple's usual routine",
  },
  {
    "id": "quiet-intimate",
    "dimension": "ambience",
    "weight": 10,
    "tokens": ("quiet", "intimate", "gallery", "museum", "literatur", "literature", "cinema", "jazz", "small", "studio", "reading", "lesung", "listening"),
    "reason": "it supports a more personal pace than a broad public outing",
  },
  {
    "id": "intentional-effort",
    "dimension": "effort",
    "weight": 11,
    "tokens": ("ticket", "reservation", "booking", "sold", "premium", "seat", "guided tour", "registration", "limited seats"),
    "reason": "it rewards intentional planning rather than generic browsing",
  },
  {
    "id": "easy-pairing",
    "dimension": "pairability",
    "weight": 8,
    "tokens": ("food", "market", "wine", "walk", "river", "lake", "old town", "city-core", "courtyard", "garden", "rooftop", "bookshop", "record store"),
    "reason": "it can be paired into a thoughtful date without becoming a generic dinner plan",
  },
  {
    "id": "weather-flexible",
    "dimension": "resilience",
    "weight": 7,
    "tokens": ("rainy-day", "indoor", "museum", "cinema", "theatre", "gallery", "library"),
    "reason": "it gives the planner a resilient backup path",
  },
  {
    "id": "source-grounded",
    "dimension": "verification",
    "weight": 13,
    "tokens": ("official", "primary", "city-pack", "venue", "calendar", "operator", "museum-program", "theatre", "cinema", "ticket"),
    "reason": "it comes from a source DatePipe can re-check before reveal",
  },
  {
    "id": "surprise-friendly",
    "dimension": "privacy",
    "weight": 9,
    "tokens": ("privacyfit high", "privacy high", "discreet", "walk-in", "low-cost", "short-spontaneous", "accomplice"),
    "reason": "it can be planned discreetly without over-explaining the surprise",
  },
]

SPECIALNESS_PENALTIES = [
  {
    "id": "generic-food",
    "weight": -18,
    "tokens": ("restaurant", "restaurants", "dinner", "brunch", "lunch", "bar", "cocktail"),
    "reason": "food-only listings need a stronger hook than simply eating somewhere",
  },
  {
    "id": "broad-index",
    "weight": -14,
    "tokens": ("home", "homepage", "overview", "all events", "alle veranstaltungen", "search", "suche", "category"),
    "reason": "broad index pages are weaker than specific, current activities",
  },
  {
    "id": "admin-page",
    "weight": -35,
    "tokens": ("privacy", "datenschutz", "impressum", "contact", "kontakt", "login", "jobs", "press", "newsletter"),
    "reason": "administrative pages are not date candidates",
  },
  {
    "id": "routine-default",
    "weight": -10,
    "tokens": ("usual", "familiar", "mainstream", "generic"),
    "reason": "routine defaults should not outrank distinctive local options",
  },
]


def candidate_specialness_text(candidate: dict) -> str:
  parts = []
  for key in (
    "title",
    "category",
    "sourceName",
    "sourceType",
    "sourceKind",
    "whyNotGeneric",
    "liveVerificationRule",
    "bookingNeed",
    "privacyFit",
    "eventUrl",
  ):
    parts.append(str(candidate.get(key, "")))
  for key in ("specialness", "surpriseFit", "bestFor", "budgetFit"):
    value = candidate.get(key)
    if isinstance(value, list):
      parts.extend(str(item) for item in value)
    else:
      parts.append(str(value or ""))
  return " ".join(parts).lower()


def evaluate_specialness(candidate: dict, profile: dict | None = None) -> dict:
  return evaluate_specialness_v2(candidate, profile)


MATCHING_REQUIRED_FIELDS = (
  "id",
  "candidateKind",
  "title",
  "category",
  "date",
  "eventUrl",
  "sourceName",
  "liveVerificationRule",
  "whyNotGeneric",
)

MATCHING_RECOMMENDED_FIELDS = (
  "duration",
  "timing",
  "bookingNeed",
  "privacyFit",
  "budgetFit",
  "bestFor",
  "risk",
  "specialness",
  "specialnessScore",
  "specialnessSignals",
  "specialnessDimensions",
  "whySpecial",
)


def field_has_value(candidate: dict, key: str) -> bool:
  value = candidate.get(key)
  if isinstance(value, str):
    return bool(value.strip())
  if isinstance(value, (list, dict)):
    return bool(value)
  if isinstance(value, (int, float)):
    return value != 0
  return value is not None


def matching_readiness(candidate: dict) -> dict:
  missing_required = [key for key in MATCHING_REQUIRED_FIELDS if not field_has_value(candidate, key)]
  missing_recommended = [key for key in MATCHING_RECOMMENDED_FIELDS if not field_has_value(candidate, key)]
  issues = []
  if missing_required:
    issues.append("missing-required-fields")
  if missing_recommended:
    issues.append("missing-recommended-fields")
  url = str(candidate.get("eventUrl") or "")
  if url and not url.startswith(("http://", "https://")):
    issues.append("invalid-event-url")
  if candidate.get("candidateKind") == "extracted-event-lead" and candidate.get("risk") != "needs-verification":
    issues.append("extracted-lead-missing-verification-risk")
  if int(candidate.get("specialnessScore") or 0) < 35:
    issues.append("weak-specialness")
  score = 100 - (len(missing_required) * 14) - (len(missing_recommended) * 4) - (len(issues) * 4)
  if candidate.get("candidateKind") == "extracted-event-lead":
    score -= 4
  return {
    "score": max(0, min(100, score)),
    "readyForMatching": not missing_required and "invalid-event-url" not in issues,
    "issues": issues,
    "missingRequired": missing_required,
    "missingRecommended": missing_recommended[:8],
  }


def apply_specialness_model(candidate: dict, profile: dict | None = None) -> dict:
  enriched = dict(candidate)
  special = evaluate_specialness(enriched, profile)
  enriched.update(special)
  existing_tags = [str(item) for item in enriched.get("specialness", []) if str(item).strip()]
  signal_tags = [item["id"] for item in special["specialnessSignals"]]
  enriched["specialness"] = list(dict.fromkeys(existing_tags + signal_tags))[:8]
  if enriched.get("whyNotGeneric"):
    enriched["whyNotGeneric"] = f"{enriched['whyNotGeneric']} {special['whySpecial']}"[:520]
  else:
    enriched["whyNotGeneric"] = special["whySpecial"]
  readiness = matching_readiness(enriched)
  enriched["matchingReadinessScore"] = readiness["score"]
  enriched["matchingReady"] = readiness["readyForMatching"]
  enriched["matchingReadinessIssues"] = readiness["issues"]
  enriched["matchingMissingRequired"] = readiness["missingRequired"]
  enriched["matchingMissingRecommended"] = readiness["missingRecommended"]
  return enriched


def digest_specialness(source: dict) -> list[str]:
  text = source_text(source)
  tags = []
  checks = [
    ("time-limited", ("seasonal", "festival", "current", "calendar", "programme")),
    ("quiet-intimate", ("quiet", "intimate", "gallery", "museum", "literatur", "cinema")),
    ("local-specific", ("local-secret", "obscure", "city", "municipal", "neighbourhood")),
    ("booking-worthy", ("ticket", "reservation", "booking", "premium")),
    ("weather-flexible", ("rainy-day", "indoor", "museum", "cinema")),
    ("easy-pairing", ("market", "food", "walk", "danube", "river", "old town", "city-core")),
    ("outside-usual", ("weird", "workshop", "experimental", "contemporary", "craft")),
  ]
  for label, needles in checks:
    if any(needle in text for needle in needles):
      tags.append(label)
  return tags[:5] or ["curated-source"]


def digest_surprise_fit(source: dict) -> list[str]:
  text = source_text(source)
  fits = ["safe-stretch"]
  if any(token in text for token in ("obvious", "tourist-but-useful", "anchor-venue")):
    fits.append("familiar")
  if any(token in text for token in ("obscure", "weird", "local-secret", "workshop", "experimental")):
    fits.append("outside-zone")
  if any(token in text for token in ("premium", "ticket-sensitive", "reservation")):
    fits.append("premium-planned")
  return list(dict.fromkeys(fits))


def digest_candidate_from_source(source: dict, target_date: str, index: int) -> dict:
  name = str(source.get("name") or "Local source").strip()
  url = str(source.get("url") or "").strip()
  category = android_category_from_source(source)
  image_url = resolve_page_image(url, "", f"{name} {source.get('city', '')} event image") if index < 8 else ""
  specialness = digest_specialness(source)
  why_not_generic = str(source.get("datePipeUse") or source.get("evidenceSummary") or "").strip()
  if not why_not_generic:
    why_not_generic = f"{name} is in the curated city pack and has a real source page DatePipe can verify."
  candidate = {
    "id": str(source.get("id") or new_id("source")),
    "candidateKind": "city-pack-source",
    "title": name,
    "city": source.get("city", ""),
    "region": source.get("region", ""),
    "date": target_date,
    "category": category,
    "duration": "evening" if category in {"social", "cultural", "premium"} else "half day",
    "timing": "flexible" if source.get("typicalCadence") in {"daily", "weekly"} else "precise",
    "eventUrl": url,
    "imageUrl": image_url,
    "sourceName": name,
    "specialness": specialness,
    "surpriseFit": digest_surprise_fit(source),
    "bookingNeed": source.get("bookingNeed", ""),
    "privacyFit": source.get("privacyFit", ""),
    "risk": "medium" if source.get("bookingNeed") in {"ticket", "reservation"} else "low",
    "whyNotGeneric": why_not_generic[:360],
    "liveVerificationRule": str(source.get("liveVerificationRule") or "Check current hours, date, ticketing, and closures before revealing.").strip(),
    "bestFor": normalize_tags(source.get("bestFor")),
    "budgetFit": normalize_tags(source.get("budgetFit")),
    "sourceType": source.get("sourceType", ""),
  }
  return apply_specialness_model(candidate)


def city_digest_path(city: str, target_date: str) -> Path:
  slug = re.sub(r"[^a-z0-9]+", "-", city_alias_key(city)).strip("-") or "city"
  return CITY_DIGEST_DIR / f"{slug}-{target_date}.json"


def build_city_digest(
  city: str,
  target_date: str | None = None,
  force: bool = False,
  extract_events: bool = False,
  max_sources: int = 48,
  max_events: int = 360,
  expand_pages: int = 3,
) -> dict:
  target_date = target_date or date.today().isoformat()
  path = city_digest_path(city, target_date)
  if path.exists() and not force:
    try:
      cached = load_json(path)
      if not extract_events or cached.get("extractedEvents"):
        return cached
    except Exception:
      pass
  sources = load_city_pack_sources(city, selected_only=True)
  candidates = [
    digest_candidate_from_source(source, target_date, index)
    for index, source in enumerate(sources)
    if str(source.get("url") or "").startswith(("http://", "https://"))
  ]
  candidates.sort(key=lambda item: (
    item.get("specialnessScore", 0),
    item.get("privacyFit") == "high",
    "local-specific" in item.get("specialness", []),
    "time-limited" in item.get("specialness", []),
    item.get("imageUrl") != "",
  ), reverse=True)
  extracted = extract_city_events(
    city,
    target_date,
    force=force,
    max_sources=max_sources,
    max_events=max_events,
    expand_pages=expand_pages,
  ) if extract_events else {"events": []}
  extracted_events = extracted.get("events", []) if isinstance(extracted, dict) else []
  merged_candidates = candidates[:48]
  if extracted_events:
    existing_urls = {normalize_url_key(item.get("eventUrl")) for item in merged_candidates}
    for event in extracted_events:
      key = normalize_url_key(event.get("eventUrl"))
      if key and key not in existing_urls:
        merged_candidates.append(event)
        existing_urls.add(key)
    merged_candidates.sort(key=lambda item: (item.get("specialnessScore", 0), item.get("privacyFit") == "high"), reverse=True)
  digest = {
    "schema": "datepipe.city_digest.v1",
    "city": sources[0].get("city", city) if sources else city,
    "date": target_date,
    "generatedAt": now_iso(),
    "sourceCount": len(sources),
    "discoverySources": discovery_sources_for_city(city),
    "extractedEventCount": len(extracted_events),
    "extractedEvents": extracted_events[:360],
    "candidates": merged_candidates[:60],
  }
  digest["matchingReview"] = review_matching_contract(digest)
  write_json(path, digest)
  return digest


def available_city_pack_cities() -> list[str]:
  summary_path = CITY_PACK_DIR / "city_summary.csv"
  if summary_path.exists():
    with summary_path.open("r", encoding="utf-8", newline="") as fh:
      rows = list(csv.DictReader(fh))
    cities = [str(row.get("city") or "").strip() for row in rows if str(row.get("city") or "").strip()]
    if cities:
      return cities
  path = CITY_PACK_DIR / "sources_selected.csv"
  if not path.exists():
    return []
  with path.open("r", encoding="utf-8", newline="") as fh:
    rows = list(csv.DictReader(fh))
  return sorted({str(row.get("city") or "").strip() for row in rows if str(row.get("city") or "").strip()})


def city_digest_manifest_path(target_date: str | None = None) -> Path:
  target_date = target_date or date.today().isoformat()
  return CITY_DIGEST_DIR / f"manifest-{target_date}.json"


def load_city_digest_manifest(target_date: str | None = None) -> dict:
  path = city_digest_manifest_path(target_date)
  if not path.exists():
    return {
      "schema": "datepipe.city_digest_manifest.v1",
      "date": target_date or date.today().isoformat(),
      "generatedAt": "",
      "cityCount": 0,
      "digests": [],
    }
  return load_json(path)


def review_matching_contract(digest: dict) -> dict:
  candidates = digest.get("candidates", []) if isinstance(digest.get("candidates"), list) else []
  issue_counts: dict[str, int] = {}
  missing_required_counts: dict[str, int] = {}
  missing_recommended_counts: dict[str, int] = {}
  ready_count = 0
  total_score = 0
  weak_examples = []
  for candidate in candidates:
    if not isinstance(candidate, dict):
      continue
    readiness = matching_readiness(candidate)
    total_score += readiness["score"]
    if readiness["readyForMatching"]:
      ready_count += 1
    for issue in readiness["issues"]:
      issue_counts[issue] = issue_counts.get(issue, 0) + 1
    for field in readiness["missingRequired"]:
      missing_required_counts[field] = missing_required_counts.get(field, 0) + 1
    for field in readiness["missingRecommended"]:
      missing_recommended_counts[field] = missing_recommended_counts.get(field, 0) + 1
    if readiness["score"] < 70 and len(weak_examples) < 8:
      weak_examples.append({
        "id": candidate.get("id", ""),
        "title": candidate.get("title", ""),
        "eventUrl": candidate.get("eventUrl", ""),
        "score": readiness["score"],
        "issues": readiness["issues"],
        "missingRequired": readiness["missingRequired"],
        "missingRecommended": readiness["missingRecommended"],
      })
  count = len(candidates)
  return {
    "schema": "datepipe.matching_contract_review.v1",
    "city": digest.get("city", ""),
    "date": digest.get("date", ""),
    "generatedAt": now_iso(),
    "candidateCount": count,
    "readyCount": ready_count,
    "readyRatio": round(ready_count / count, 3) if count else 0,
    "averageReadinessScore": round(total_score / count, 1) if count else 0,
    "issueCounts": dict(sorted(issue_counts.items(), key=lambda item: item[1], reverse=True)),
    "missingRequiredCounts": dict(sorted(missing_required_counts.items(), key=lambda item: item[1], reverse=True)),
    "missingRecommendedCounts": dict(sorted(missing_recommended_counts.items(), key=lambda item: item[1], reverse=True)),
    "weakExamples": weak_examples,
  }


def refresh_city_digests(cities: list[str] | None = None, target_date: str | None = None, force: bool = False, extract_events: bool = False) -> dict:
  target_date = target_date or date.today().isoformat()
  selected_cities = [city for city in (cities or available_city_pack_cities()) if str(city).strip()]
  digests = []
  errors = []
  started = time.time()
  for city in selected_cities:
    try:
      digest = build_city_digest(city, target_date, force=force, extract_events=extract_events)
      digest_path = city_digest_path(city, target_date)
      try:
        display_path = str(digest_path.relative_to(ROOT))
      except ValueError:
        display_path = str(digest_path)
      digests.append({
        "city": digest.get("city", city),
        "date": digest.get("date", target_date),
        "generatedAt": digest.get("generatedAt", ""),
        "sourceCount": digest.get("sourceCount", 0),
        "discoverySourceCount": len(digest.get("discoverySources", [])),
        "extractedEventCount": len(digest.get("extractedEvents", [])),
        "candidateCount": len(digest.get("candidates", [])),
        "matchingReadyCount": review_matching_contract(digest).get("readyCount", 0),
        "path": display_path,
      })
    except Exception as exc:
      errors.append({"city": city, "error": type(exc).__name__, "message": str(exc)})
  manifest = {
    "schema": "datepipe.city_digest_manifest.v1",
    "date": target_date,
    "generatedAt": now_iso(),
    "durationMs": int((time.time() - started) * 1000),
    "cityCount": len(digests),
    "errorCount": len(errors),
    "digests": digests,
    "errors": errors,
  }
  write_json(city_digest_manifest_path(target_date), manifest)
  manifest["pipelineRun"] = PipelineStore(PIPELINE_DB_PATH).record_manifest(
    manifest,
    cities=selected_cities,
    force=force,
    extract_events=extract_events,
    started_at=started,
  )
  return manifest


def pipeline_status(limit: int = 8) -> dict:
  return PipelineStore(PIPELINE_DB_PATH).status(limit=limit)


def couple_profile_from_payload(body: dict, state: dict | None = None) -> dict:
  base = dict((state or {}).get("profile", {}))
  incoming = body.get("coupleProfile") if isinstance(body.get("coupleProfile"), dict) else body.get("profile")
  if isinstance(incoming, dict):
    base.update(incoming)
  for key in ("likes", "avoid", "usualDates", "specialMeans", "recentDates", "constraints", "foodDrink", "specialOccasions"):
    base[key] = normalize_tags(base.get(key, []))
  return base


def signal_event_weight(event_type: str) -> int:
  return {
    "shown": 0,
    "opened": 1,
    "image-opened": 1,
    "link-opened": 2,
    "liked": 4,
    "saved": 5,
    "planned": 7,
    "booked": 8,
    "completed": 9,
    "dismissed": -4,
    "hidden": -6,
    "excluded": -10,
  }.get(event_type, 0)


def record_matching_signal(body: dict, state: dict | None = None, context: dict | None = None) -> dict:
  event_type = normalize_key(body.get("eventType") or body.get("type") or "")
  candidate = body.get("candidate") if isinstance(body.get("candidate"), dict) else {}
  signal = {
    "id": new_id("signal"),
    "createdAt": now_iso(),
    "eventType": event_type or "unknown",
    "weight": signal_event_weight(event_type),
    "candidateId": str(body.get("candidateId") or candidate.get("id") or ""),
    "title": str(body.get("title") or candidate.get("title") or "")[:120],
    "category": str(body.get("category") or candidate.get("category") or ""),
    "sourceName": str(body.get("sourceName") or candidate.get("sourceName") or ""),
    "eventUrl": str(body.get("eventUrl") or candidate.get("eventUrl") or ""),
    "specialness": normalize_tags(body.get("specialness") or candidate.get("specialness") or []),
    "specialnessScore": int(body.get("specialnessScore") or candidate.get("specialnessScore") or 0),
    "context": body.get("context") if isinstance(body.get("context"), dict) else {},
  }
  state = state if state is not None else load_scoped_state(context)
  signals = state.setdefault("matchingSignals", [])
  signals.insert(0, signal)
  del signals[500:]
  save_scoped_state(state, context)
  return signal


def memory_key(memory_type: str, text: str) -> str:
  return f"{normalize_key(memory_type)}:{normalize_key(text)[:120]}"


def upsert_couple_memory(state: dict, memory: dict) -> dict:
  memory_type = normalize_key(memory.get("type") or "note") or "note"
  text = str(memory.get("text") or "").strip()
  if not text:
    raise ValueError("memory text is required")
  key = memory_key(memory_type, text)
  memories = state.setdefault("coupleMemory", [])
  existing = next((item for item in memories if item.get("key") == key), None)
  now = now_iso()
  if existing:
    existing["lastSeenAt"] = now
    existing["confidence"] = max(float(existing.get("confidence") or 0.5), float(memory.get("confidence") or 0.65))
    existing["weight"] = max(int(existing.get("weight") or 1), int(memory.get("weight") or 1))
    existing["source"] = str(memory.get("source") or existing.get("source") or "manual")
    return existing
  row = {
    "id": new_id("memory"),
    "key": key,
    "type": memory_type,
    "text": text[:260],
    "source": str(memory.get("source") or "manual"),
    "confidence": max(0.1, min(1.0, float(memory.get("confidence") or 0.65))),
    "weight": int(memory.get("weight") or 1),
    "createdAt": now,
    "lastSeenAt": now,
  }
  memories.insert(0, row)
  del memories[200:]
  return row


def record_couple_memory(body: dict, state: dict | None = None, context: dict | None = None) -> dict:
  state = state if state is not None else load_scoped_state(context)
  memory = upsert_couple_memory(state, body)
  save_scoped_state(state, context)
  return memory


def derived_profile_memories(profile: dict) -> list[dict]:
  rows = []
  for item in profile.get("likes", []):
    rows.append({"type": "likes", "text": f"They tend to like {item}.", "source": "profile", "confidence": 0.8, "weight": 4})
  for item in profile.get("avoid", []):
    rows.append({"type": "avoid", "text": f"Avoid {item} unless explicitly requested.", "source": "profile", "confidence": 0.85, "weight": 5})
  for item in profile.get("usualDates", []):
    rows.append({"type": "usual-pattern", "text": f"{item} is part of their usual pattern, so it needs an extra hook to feel special.", "source": "profile", "confidence": 0.75, "weight": 3})
  for item in profile.get("specialMeans", []):
    rows.append({"type": "special-meaning", "text": f"Special should feel {item}.", "source": "profile", "confidence": 0.8, "weight": 5})
  if profile.get("coupleVibe"):
    rows.append({"type": "couple-identity", "text": f"Together they are: {profile.get('coupleVibe')}.", "source": "profile", "confidence": 0.75, "weight": 5})
  for item in profile.get("constraints", []):
    rows.append({"type": "constraint", "text": f"Planning constraint: {item}.", "source": "profile", "confidence": 0.85, "weight": 5})
  for item in profile.get("foodDrink", []):
    rows.append({"type": "food-drink-fit", "text": f"Food and drink fit: {item}.", "source": "profile", "confidence": 0.7, "weight": 3})
  for item in profile.get("specialOccasions", []):
    rows.append({"type": "meaningful-date", "text": f"Meaningful date or occasion: {item}.", "source": "profile", "confidence": 0.75, "weight": 4})
  if profile.get("surpriseLevel"):
    rows.append({"type": "surprise-comfort", "text": f"Preferred surprise level is {profile.get('surpriseLevel')}.", "source": "profile", "confidence": 0.75, "weight": 3})
  if profile.get("energy"):
    rows.append({"type": "energy", "text": f"Typical planning energy is {profile.get('energy')}.", "source": "profile", "confidence": 0.65, "weight": 2})
  return rows


def derived_signal_memories(state: dict) -> list[dict]:
  category_scores: dict[str, int] = {}
  source_scores: dict[str, int] = {}
  special_scores: dict[str, int] = {}
  for signal in state.get("matchingSignals", [])[:120]:
    weight = int(signal.get("weight") or 0)
    if not weight:
      continue
    category = normalize_key(signal.get("category"))
    source = str(signal.get("sourceName") or "").strip()
    if category:
      category_scores[category] = category_scores.get(category, 0) + weight
    if source:
      source_scores[source] = source_scores.get(source, 0) + weight
    for tag in normalize_tags(signal.get("specialness")):
      special_scores[tag] = special_scores.get(tag, 0) + weight
  rows = []
  for category, score in sorted(category_scores.items(), key=lambda item: abs(item[1]), reverse=True)[:8]:
    if abs(score) < 4:
      continue
    direction = "respond well to" if score > 0 else "often reject"
    rows.append({"type": "learned-category", "text": f"They {direction} {category} ideas.", "source": "behavior", "confidence": min(0.9, 0.45 + abs(score) / 20), "weight": abs(score)})
  for source, score in sorted(source_scores.items(), key=lambda item: abs(item[1]), reverse=True)[:6]:
    if abs(score) < 4:
      continue
    direction = "has worked for them" if score > 0 else "has not worked for them"
    rows.append({"type": "learned-source", "text": f"{source} {direction}.", "source": "behavior", "confidence": min(0.85, 0.45 + abs(score) / 24), "weight": abs(score)})
  for tag, score in sorted(special_scores.items(), key=lambda item: abs(item[1]), reverse=True)[:8]:
    if abs(score) < 4:
      continue
    direction = "fit them" if score > 0 else "may not fit them"
    rows.append({"type": "learned-specialness", "text": f"{tag} signals {direction}.", "source": "behavior", "confidence": min(0.85, 0.45 + abs(score) / 24), "weight": abs(score)})
  return rows


def couple_memory_profile(state: dict, profile: dict, limit: int = 40) -> dict:
  persisted = [item for item in state.get("coupleMemory", []) if isinstance(item, dict)]
  derived = derived_profile_memories(profile) + derived_signal_memories(state)
  rows = []
  for item in persisted:
    rows.append({
      "id": item.get("id", ""),
      "type": item.get("type", "note"),
      "text": item.get("text", ""),
      "source": item.get("source", "manual"),
      "confidence": float(item.get("confidence") or 0.5),
      "weight": int(item.get("weight") or 1),
      "lastSeenAt": item.get("lastSeenAt", ""),
    })
  rows.extend(derived)
  rows = [row for row in rows if str(row.get("text") or "").strip()]
  rows.sort(key=lambda item: (float(item.get("confidence") or 0), int(item.get("weight") or 0)), reverse=True)
  return {
    "schema": "datepipe.couple_memory_profile.v1",
    "generatedAt": now_iso(),
    "memoryCount": len(rows),
    "memories": rows[:limit],
  }


def matching_taste_profile(state: dict, profile: dict) -> dict:
  category_weights: dict[str, int] = {}
  signal_weights: dict[str, int] = {}
  source_weights: dict[str, int] = {}
  avoid_tokens = set(profile.get("avoid", []))
  usual_tokens = set(profile.get("usualDates", []))
  like_tokens = set(profile.get("likes", []))
  special_tokens = set(profile.get("specialMeans", []))
  for item in like_tokens:
    signal_weights[item] = signal_weights.get(item, 0) + 6
  for item in special_tokens:
    signal_weights[item] = signal_weights.get(item, 0) + 8
  for index, signal in enumerate(state.get("matchingSignals", [])[:500]):
    weight = int(signal.get("weight") or 0)
    if not weight:
      continue
    decay = max(0.35, 1.0 - (index / 650))
    adjusted = round(weight * decay)
    category = normalize_key(signal.get("category"))
    if category:
      category_weights[category] = category_weights.get(category, 0) + adjusted
    source = normalize_key(signal.get("sourceName"))
    if source:
      source_weights[source] = source_weights.get(source, 0) + adjusted
    for tag in normalize_tags(signal.get("specialness")):
      signal_weights[tag] = signal_weights.get(tag, 0) + adjusted
    text = normalize_key(" ".join(str(signal.get(key, "")) for key in ("title", "sourceName", "eventUrl")))
    if weight < -5:
      for token in text.split()[:20]:
        if len(token) > 3:
          avoid_tokens.add(token)
    elif weight > 4:
      for token in text.split()[:20]:
        if len(token) > 3:
          signal_weights[token] = signal_weights.get(token, 0) + 1
  return {
    "schema": "datepipe.matching_taste_profile.v1",
    "generatedAt": now_iso(),
    "signalCount": len(state.get("matchingSignals", [])),
    "categoryWeights": dict(sorted(category_weights.items(), key=lambda item: item[1], reverse=True)[:30]),
    "signalWeights": dict(sorted(signal_weights.items(), key=lambda item: item[1], reverse=True)[:60]),
    "sourceWeights": dict(sorted(source_weights.items(), key=lambda item: item[1], reverse=True)[:30]),
    "avoidTokens": sorted(avoid_tokens)[:80],
    "usualTokens": sorted(usual_tokens)[:80],
  }


def candidate_fit_score(candidate: dict, profile: dict) -> int:
  return score_candidate(candidate, profile).score


def deterministic_personalized_options(digest: dict, profile: dict, limit: int = 8) -> list[dict]:
  ranked = rank_candidates(digest.get("candidates", []), profile)
  options = []
  for index, (candidate, match) in enumerate(ranked[:limit]):
    category = candidate.get("category") or "standard"
    specialness = ", ".join(candidate.get("specialness", [])[:3])
    options.append({
      "id": f"backend-{candidate.get('id', index)}",
      "title": f"{candidate.get('title', 'Local plan')}",
      "category": category,
      "duration": candidate.get("duration", "evening"),
      "timing": candidate.get("timing", "flexible"),
      "stretch": (candidate.get("surpriseFit") or ["safe-stretch"])[0],
      "budget": (candidate.get("budgetFit") or ["casual"])[0],
      "why": f"{candidate.get('whyNotGeneric', '')} Specialness: {specialness}.",
      "visibleHint": candidate.get("liveVerificationRule", "I will check the current details before reveal."),
      "eventUrl": candidate.get("eventUrl", ""),
      "imageUrl": candidate.get("imageUrl", ""),
      "sourceName": candidate.get("sourceName", ""),
      "dateChoice": digest.get("date", date.today().isoformat()),
      "dateLabel": date_label_for(digest.get("date", "")),
      "score": match.score,
      "matchReasons": match.reasons,
      "matchWarnings": match.warnings,
      "matchDimensions": match.dimensions,
      "specialness": candidate.get("specialness", []),
      "specialnessScore": candidate.get("specialnessScore", 0),
      "specialnessSignals": candidate.get("specialnessSignals", []),
      "specialnessPenalties": candidate.get("specialnessPenalties", []),
      "specialnessDimensions": candidate.get("specialnessDimensions", {}),
      "specialnessHooks": candidate.get("specialnessHooks", []),
      "specialDinnerSignals": candidate.get("specialDinnerSignals", []),
      "genericRisk": candidate.get("genericRisk", ""),
      "specialnessLabel": candidate.get("specialnessLabel", ""),
      "whySpecial": candidate.get("whySpecial", ""),
      "whyNotGeneric": candidate.get("whyNotGeneric", ""),
    })
  return options


def backend_personalization_prompt(digest: dict, profile: dict) -> str:
  candidates = digest.get("candidates", [])[:30]
  discovery_sources = digest.get("discoverySources", [])[:16]
  safe_profile = {
    "homeBase": profile.get("homeBase"),
    "budget": profile.get("budget") or profile.get("budgetComfort"),
    "freeTime": profile.get("freeTime"),
    "usualDates": profile.get("usualDates", []),
    "likes": profile.get("likes", []),
    "avoid": profile.get("avoid", []),
    "specialMeans": profile.get("specialMeans", []),
    "coupleVibe": profile.get("coupleVibe"),
    "constraints": profile.get("constraints", []),
    "foodDrink": profile.get("foodDrink", []),
    "specialOccasions": profile.get("specialOccasions", []),
    "surpriseLevel": profile.get("surpriseLevel"),
    "energy": profile.get("energy"),
    "searchKeyword": profile.get("searchKeyword") or profile.get("searchKeywords"),
    "tasteProfile": profile.get("tasteProfile", {}),
    "coupleMemory": profile.get("coupleMemory", {}),
  }
  return (
    "You are DatePipe's backend planner. You receive city-digest candidates from curated real sources. "
    "You also receive discovery-source scouts that explain where future backend research should look first. "
    "Some candidates may be extracted-event-leads; treat those as source-grounded but needing live verification before reveal. "
    "Do not invent venues, URLs, images, dates, or sources. Select and rewrite only from the candidate list. "
    "Explain why each pick is special for this couple and why it is not generic. "
    "Return only valid JSON with {\"options\":[...]} using fields title, category, duration, timing, stretch, budget, why, visibleHint, eventUrl, imageUrl, sourceName, dateChoice.\n\n"
    f"Couple profile:\n{json.dumps(safe_profile, ensure_ascii=False)}\n\n"
    f"City digest:\n{json.dumps({'city': digest.get('city'), 'date': digest.get('date'), 'discoverySources': discovery_sources, 'candidates': candidates}, ensure_ascii=False)}"
  )


def generate_backend_personalized_options(body: dict, state: dict | None = None) -> tuple[list[dict], str, dict]:
  state = state if state is not None else load_state()
  profile = couple_profile_from_payload(body, state)
  profile["tasteProfile"] = matching_taste_profile(state, profile)
  profile["coupleMemory"] = couple_memory_profile(state, profile)
  city = str(body.get("city") or profile.get("homeBase") or "Vienna").strip() or "Vienna"
  target_date = str(body.get("date") or body.get("searchDate") or date.today().isoformat())[:10]
  digest = build_city_digest(city, target_date, force=bool(body.get("forceDigest")), extract_events=bool(body.get("extractEvents")))
  llm_status = "no-candidates"
  if digest.get("candidates"):
    result = llm_generate_text(
      backend_personalization_prompt(digest, profile),
      purpose="backend",
      max_output_tokens=2200,
      temperature=0.55,
      use_web_search=False,
      timeout=30,
    )
    llm_status = str(result.get("status") or "fallback")
    options = normalize_backend_options(str(result.get("text") or ""), digest) if result.get("ok") else []
    if options:
      return options, f"{result.get('provider')}-backend-city-digest", digest
  return deterministic_personalized_options(digest, profile), f"backend-city-digest:{llm_status}", digest


def normalize_backend_options(text: str, digest: dict) -> list[dict]:
  parsed = parse_json_object_from_text(text)
  rows = parsed.get("options") if isinstance(parsed, dict) else []
  if not isinstance(rows, list):
    return []
  by_url = {item.get("eventUrl"): item for item in digest.get("candidates", [])}
  out = []
  for index, row in enumerate(rows[:8]):
    if not isinstance(row, dict):
      continue
    event_url = str(row.get("eventUrl") or "").strip()
    candidate = by_url.get(event_url, {})
    if not event_url.startswith(("http://", "https://")) or not candidate:
      continue
    out.append({
      "id": f"backend-ai-{index}-{stable_rotation(event_url, index)}",
      "title": str(row.get("title") or candidate.get("title") or "DatePipe plan").strip()[:90],
      "category": choice_or_default(row.get("category"), {"standard", "sportive", "different", "cultural", "culinary", "romantic", "social", "premium"}, candidate.get("category", "standard")),
      "duration": str(row.get("duration") or candidate.get("duration") or "evening"),
      "timing": choice_or_default(row.get("timing"), {"precise", "flexible"}, candidate.get("timing", "flexible")),
      "stretch": choice_or_default(row.get("stretch"), {"familiar", "safe-stretch", "outside-zone", "spicy"}, "safe-stretch"),
      "budget": str(row.get("budget") or (candidate.get("budgetFit") or ["casual"])[0]),
      "why": str(row.get("why") or candidate.get("whyNotGeneric") or "").strip(),
      "visibleHint": str(row.get("visibleHint") or candidate.get("liveVerificationRule") or "").strip(),
      "eventUrl": event_url,
      "imageUrl": str(row.get("imageUrl") or candidate.get("imageUrl") or ""),
      "sourceName": str(row.get("sourceName") or candidate.get("sourceName") or ""),
      "dateChoice": str(row.get("dateChoice") or digest.get("date") or date.today().isoformat())[:10],
      "dateLabel": date_label_for(str(row.get("dateChoice") or digest.get("date") or "")),
      "score": int(row.get("score") or 90 - index),
      "specialness": candidate.get("specialness", []),
      "specialnessScore": candidate.get("specialnessScore", 0),
      "specialnessSignals": candidate.get("specialnessSignals", []),
      "specialnessPenalties": candidate.get("specialnessPenalties", []),
      "specialnessDimensions": candidate.get("specialnessDimensions", {}),
      "specialnessHooks": candidate.get("specialnessHooks", []),
      "specialDinnerSignals": candidate.get("specialDinnerSignals", []),
      "genericRisk": candidate.get("genericRisk", ""),
      "specialnessLabel": candidate.get("specialnessLabel", ""),
      "whySpecial": candidate.get("whySpecial", ""),
      "whyNotGeneric": candidate.get("whyNotGeneric", ""),
    })
  return out


def fetch_text(url: str, timeout: float = 4.0) -> str:
  request = Request(
    url,
    headers={
      "User-Agent": "DatePipe prototype image picker",
      "Accept": "text/html,application/xhtml+xml",
    },
  )
  with urlopen(request, timeout=timeout) as response:
    content_type = response.headers.get("Content-Type", "")
    if "text/html" not in content_type and "application/xhtml" not in content_type:
      return ""
    return response.read(500_000).decode("utf-8", errors="ignore")


def strip_markup(value: str) -> str:
  without_scripts = re.sub(r"(?is)<(script|style).*?</\1>", " ", value or "")
  without_tags = re.sub(r"(?s)<[^>]+>", " ", without_scripts)
  return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


def html_page_title(document: str) -> str:
  match = re.search(r"(?is)<title[^>]*>(.*?)</title>", document or "")
  return strip_markup(match.group(1)) if match else ""


def html_meta_description(document: str) -> str:
  for tag in re.findall(r"(?is)<meta\b[^>]*>", document or ""):
    name = attr_value(tag, "name").lower() or attr_value(tag, "property").lower()
    if name in {"description", "og:description", "twitter:description"}:
      return attr_value(tag, "content")
  return ""


def json_objects_from_script(value: object) -> list[dict]:
  if isinstance(value, dict):
    rows = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
      rows.extend(item for item in graph if isinstance(item, dict))
    return rows
  if isinstance(value, list):
    rows = []
    for item in value:
      rows.extend(json_objects_from_script(item))
    return rows
  return []


def json_ld_event_links(document: str, base_url: str) -> list[dict]:
  links = []
  for raw in re.findall(r"(?is)<script\b[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", document or ""):
    try:
      parsed = json.loads(html.unescape(raw.strip()))
    except Exception:
      continue
    for item in json_objects_from_script(parsed):
      raw_type = item.get("@type")
      types = raw_type if isinstance(raw_type, list) else [raw_type]
      if not any(str(row or "").lower() in {"event", "theaterevent", "musicevent", "comedyevent", "educationevent", "exhibitionevent", "festival"} for row in types):
        continue
      name = str(item.get("name") or "").strip()
      url = str(item.get("url") or item.get("@id") or base_url).strip()
      start_date = str(item.get("startDate") or "").strip()
      title = " ".join(part for part in (name, start_date[:10]) if part).strip()
      if title and url:
        links.append({
          "url": urljoin(base_url, url),
          "title": title,
          "candidateKind": "jsonld-event",
          "startDate": start_date,
        })
  return links


def iter_html_links(document: str, base_url: str) -> list[dict]:
  links = []
  for match in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", document or ""):
    attrs, body = match.groups()
    href = attr_value(attrs, "href")
    if not href:
      continue
    url = urljoin(base_url, href)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
      continue
    if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".pdf", ".zip", ".ics")):
      continue
    text = strip_markup(body) or attr_value(attrs, "aria-label") or attr_value(attrs, "title")
    if len(text) < 3 or len(text) > 180:
      continue
    links.append({"url": url.split("#", 1)[0], "title": text})
  return links


WEAK_EVENT_ANCHOR_PATTERNS = (
  r"^\s*(calendar|kalender|events?|veranstaltungen|programme?|programm)\s*$",
  r"\barchive\b",
  r"\bnewsletter\b",
  r"\binstagram\b",
  r"\bfacebook\b",
  r"\byoutube\b",
  r"\btiktok\b",
  r"\bimprint\b",
  r"\bpress\b",
  r"\bjobs?\b",
  r"\bopening hours?\b",
  r"\btickets?\s*&\s*prices?\b",
  r"\bworkshop office\b",
  r"\bworkshop centre\b",
  r"\bworkshops?\s*&\s*research\b",
  r"\bfull workshop schedule\b",
  r"\bwebsite nachlesen\b",
  r"\bzur erweiterten suche\b",
)


def link_has_detail_shape(url: str) -> bool:
  parsed = urlparse(str(url or ""))
  segments = [segment for segment in parsed.path.strip("/").split("/") if segment]
  if not segments:
    return False
  last = normalize_key(segments[-1])
  broad = {"calendar", "events", "event", "program", "programme", "programm", "kalender", "veranstaltungen", "archive", "en", "de"}
  if last in broad:
    return False
  if re.search(r"\b20\d{2}|\d{1,2}[-_.]\d{1,2}", parsed.path):
    return True
  return len(segments) >= 2 and len(last) >= 8


def is_weak_event_anchor(title: str, url: str) -> bool:
  text = normalize_key(f"{title} {url}")
  return any(re.search(pattern, text) for pattern in WEAK_EVENT_ANCHOR_PATTERNS)


def event_text_has_date_signal(value: object) -> bool:
  text = str(value or "")
  return bool(
    re.search(r"\b20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}\b", text)
    or re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text)
    or re.search(r"\b\d{4}-\d{2}-\d{2}[t_ -]\d{2}", text, re.IGNORECASE)
  )


def event_link_score(link: dict, source: dict, city: str, target_date: str) -> int:
  text = normalize_key(" ".join([
    link.get("title", ""),
    link.get("url", ""),
    source.get("name", ""),
    source.get("sourceKind", ""),
    " ".join(source.get("bestFor", []) if isinstance(source.get("bestFor"), list) else []),
  ]))
  score = 0
  event_tokens = (
    "event", "events", "veranstaltung", "veranstaltungen", "programm", "program", "calendar", "kalender",
    "tickets", "ticket", "festival", "kino", "cinema", "concert", "konzert", "workshop", "market",
    "markt", "ausstellung", "exhibition", "theater", "theatre", "lesung", "jazz", "cabaret", "kabarett",
    "party", "tour", "führung", "fuehrung", "screening", "premiere", "opening", "vernissage", "kurs",
    "show", "performance", "tanz", "dance", "talk", "lecture", "vortrag", "open air", "advent",
  )
  weak_tokens = (
    "privacy", "datenschutz", "impressum", "kontakt", "newsletter", "login", "account", "jobs", "press",
    "facebook.com", "instagram.com", "youtube.com", "tiktok.com", "wa.me", "mailto:",
  )
  if any(token in text for token in event_tokens):
    score += 24
  if any(token in text for token in ("events/", "event/", "veranstaltungen/", "programm/", "calendar/", "kalender/", "tickets/")):
    score += 12
  if city_alias_key(city).replace(" ", "") in text.replace(" ", ""):
    score += 10
  year = target_date[:4]
  if year and year in text:
    score += 8
  if re.search(r"\b\d{1,2}[\./-]\d{1,2}", text):
    score += 8
  if any(token in text for token in ("today", "tonight", "heute", "morgen", "weekend", "wochenende")):
    score += 8
  if any(token in text for token in weak_tokens):
    score -= 30
  if is_weak_event_anchor(str(link.get("title", "")), str(link.get("url", ""))):
    score -= 36
  if not link_has_detail_shape(str(link.get("url", ""))) and not event_text_has_date_signal(text):
    score -= 18
  if str(source.get("sourceKind") or "").endswith("editorial-guide") and not event_text_has_date_signal(text):
    score -= 16
  if source.get("trustLevel") == "high":
    score += 8
  elif source.get("trustLevel") == "medium":
    score += 4
  if source.get("scope") == "city-pack":
    score += 8
  bucket = source_quality_bucket(source)
  if bucket == "venue_primary":
    score += 12
  elif bucket == "local_curated":
    score += 8
  elif bucket == "official_baseline":
    score -= 4
  elif bucket == "event_platform":
    score -= 2
  elif bucket == "national_discovery":
    score -= 10
  if len(str(link.get("title", "")).split()) >= 3:
    score += 4
  if link_has_detail_shape(str(link.get("url", ""))):
    score += 6
  return score


def category_from_event_text(value: str) -> str:
  text = normalize_key(value)
  if any(token in text for token in ("food", "wine", "markt", "tasting", "kulinar")):
    return "culinary"
  if any(token in text for token in ("walk", "bike", "lake", "river", "hike", "outdoor", "garden")):
    return "sportive"
  if any(token in text for token in ("concert", "konzert", "jazz", "cabaret", "kabarett", "club", "music")):
    return "social"
  if any(token in text for token in ("museum", "gallery", "ausstellung", "exhibition", "kino", "cinema", "theater", "theatre", "festival")):
    return "cultural"
  if any(token in text for token in ("workshop", "ceramic", "craft", "architecture", "photo")):
    return "different"
  return "standard"


def extracted_event_from_link(link: dict, source: dict, city: str, target_date: str, score: int) -> dict:
  title = str(link.get("title") or source.get("name") or "Event lead").strip()
  url = str(link.get("url") or "").strip()
  event_date = target_date
  start_date = str(link.get("startDate") or "").strip()
  if re.match(r"^\d{4}-\d{2}-\d{2}", start_date):
    event_date = start_date[:10]
  category = category_from_event_text(f"{title} {url} {source.get('sourceKind', '')}")
  candidate = {
    "id": f"event-{stable_rotation(url, score)}",
    "candidateKind": link.get("candidateKind") or "extracted-event-lead",
    "title": title[:100],
    "city": city,
    "region": "",
    "date": event_date,
    "targetDate": target_date,
    "category": category,
    "duration": "evening" if category in {"social", "cultural", "premium"} else "half day",
    "timing": "precise" if re.search(r"\b\d{1,2}[\./-]\d{1,2}", f"{title} {url}") else "flexible",
    "eventUrl": url,
    "imageUrl": "",
    "sourceName": source.get("name", ""),
    "sourceId": source.get("id", ""),
    "sourceScope": source.get("scope", ""),
    "sourceCities": source.get("cities", []),
    "specialness": ["source-grounded"],
    "surpriseFit": ["safe-stretch"],
    "bookingNeed": "unknown",
    "privacyFit": "medium",
    "risk": "needs-verification",
    "whyNotGeneric": f"Extracted from {source.get('name', 'a curated source-scout page')}. It is a lead, not final truth, and must be verified before reveal.",
    "liveVerificationRule": source.get("verificationRule") or "Verify current date, time, venue, booking, and closure status before revealing.",
    "bestFor": normalize_tags(source.get("bestFor")),
    "budgetFit": ["casual"],
    "sourceType": source.get("sourceKind", ""),
    "sourceKind": source.get("sourceKind", ""),
    "sourceQualityBucket": source.get("sourceQualityBucket") or source_quality_bucket(source),
    "sourceQualityScore": source.get("sourceQualityScore") or source_quality_score(source),
    "extractionScore": score,
  }
  return apply_specialness_model(candidate)


def filter_events_for_research_target(events: list[dict], target_date: str | None = None) -> tuple[list[dict], list[dict]]:
  prepared = []
  for event in events:
    row = dict(event)
    if target_date and not row.get("targetDate"):
      row["targetDate"] = target_date
    prepared.append(row)
  return filter_event_leads(prepared)


def event_extraction_path(city: str, target_date: str) -> Path:
  slug = re.sub(r"[^a-z0-9]+", "-", city_alias_key(city)).strip("-") or "city"
  return EVENT_EXTRACTION_DIR / f"{slug}-{target_date}.json"


def extract_events_from_source(source: dict, city: str, target_date: str, limit: int = 30, expand_pages: int = 3) -> list[dict]:
  url = str(source.get("url") or "").strip()
  if not url.startswith(("http://", "https://")):
    return []
  try:
    document = fetch_text(url, timeout=4.0)
  except Exception:
    return []
  if not document:
    return []
  leads = []
  seen = set()
  first_pass_links = json_ld_event_links(document, url) + iter_html_links(document, url)
  scored_links = []
  for link in first_pass_links:
    key = normalize_url_key(link.get("url"))
    if not key or key in seen:
      continue
    score = event_link_score(link, source, city, target_date)
    if score < 24:
      continue
    scored_links.append((score, link))
  scored_links.sort(key=lambda item: item[0], reverse=True)
  for score, link in scored_links:
    key = normalize_url_key(link.get("url"))
    if not key or key in seen:
      continue
    seen.add(key)
    leads.append(extracted_event_from_link(link, source, city, target_date, score))
  for score, link in scored_links[:expand_pages]:
    if len(leads) >= limit:
      break
    child_url = str(link.get("url") or "")
    if normalize_url_key(child_url) == normalize_url_key(url):
      continue
    try:
      child_document = fetch_text(child_url, timeout=3.0)
    except Exception:
      continue
    for child_link in json_ld_event_links(child_document, child_url) + iter_html_links(child_document, child_url):
      key = normalize_url_key(child_link.get("url"))
      if not key or key in seen:
        continue
      child_score = event_link_score(child_link, source, city, target_date) + 4
      if child_score < 24:
        continue
      seen.add(key)
      leads.append(extracted_event_from_link(child_link, source, city, target_date, child_score))
      if len(leads) >= limit:
        break
  leads.sort(key=lambda item: (item.get("extractionScore", 0), item.get("specialnessScore", 0)), reverse=True)
  return leads[:limit]


def extract_city_events(
  city: str,
  target_date: str | None = None,
  force: bool = False,
  max_sources: int = 48,
  max_events: int = 360,
  source_override: list[dict] | None = None,
  expand_pages: int = 3,
) -> dict:
  target_date = target_date or date.today().isoformat()
  path = event_extraction_path(city, target_date)
  if path.exists() and not force:
    try:
      cached = load_json(path)
      cached["cacheHit"] = True
      cached["liveFetch"] = False
      cached["fetchedSourceCount"] = 0
      return cached
    except Exception:
      pass
  started = time.time()
  events = []
  errors = []
  store = PipelineStore(PIPELINE_DB_PATH)
  if source_override is None:
    catalog_sources = discovery_sources_for_city(city, limit=max_sources * 3)
    due_sources = store.due_sources(city, catalog_sources, limit=max_sources * 3, force=force)
  else:
    catalog_sources = list(source_override)
    store.register_sources(city, catalog_sources)
    due_sources = catalog_sources
  selected_sources = select_sources_for_research(due_sources, max_sources)
  per_source_limit = max(12, min(40, (max_events // max(1, len(selected_sources))) + 8))
  for source in selected_sources:
    source_started = time.time()
    try:
      source_events = extract_events_from_source(source, city, target_date, limit=per_source_limit, expand_pages=expand_pages)
      events.extend(source_events)
      store.record_source_fetch(
        source,
        city=city,
        target_date=target_date,
        status="succeeded",
        duration_ms=int((time.time() - source_started) * 1000),
        event_count=len(source_events),
      )
    except Exception as exc:
      errors.append({"source": source.get("name", ""), "error": type(exc).__name__, "message": str(exc)})
      store.record_source_fetch(
        source,
        city=city,
        target_date=target_date,
        status="failed",
        duration_ms=int((time.time() - source_started) * 1000),
        event_count=0,
        error_type=type(exc).__name__,
        error_message=str(exc),
      )
  deduped: dict[str, dict] = {}
  for event in events:
    key = normalize_url_key(event.get("eventUrl"))
    if not key:
      continue
    existing = deduped.get(key)
    if not existing or event.get("specialnessScore", 0) > existing.get("specialnessScore", 0):
      deduped[key] = event
  raw_events = sorted(deduped.values(), key=lambda item: (item.get("specialnessScore", 0), item.get("extractionScore", 0)), reverse=True)[:max_events]
  current_events, rejected_events = filter_events_for_research_target(raw_events, target_date)
  accepted_sources = Counter(str(event.get("sourceName") or "") for event in current_events)
  accepted_categories = Counter(str(event.get("category") or "standard") for event in current_events)
  rejection_reasons = Counter(reason for event in rejected_events for reason in event.get("reasons", []))
  selected_source_mix = source_mix_summary(selected_sources)
  accepted_source_mix = source_mix_summary(current_events)
  quality_gate_status = extraction_quality_gate_status(
    accepted_count=len(current_events),
    selected_mix=selected_source_mix,
    accepted_mix=accepted_source_mix,
  )
  ingest = store.upsert_events(current_events, city=city, target_date=target_date)
  stored_raw = store.events_for_city(city, target_date=target_date, limit=max_events).get("events", [])
  stored, stored_rejected_events = filter_events_for_research_target(stored_raw, target_date)
  combined: dict[str, dict] = {}
  for event in stored + current_events:
    key = normalize_url_key(event.get("eventUrl"))
    if not key:
      continue
    existing = combined.get(key)
    if not existing or event.get("specialnessScore", 0) > existing.get("specialnessScore", 0):
      combined[key] = event
  final_events = sorted(combined.values(), key=lambda item: (item.get("specialnessScore", 0), item.get("extractionScore", 0)), reverse=True)[:max_events]
  payload = {
    "schema": "datepipe.event_extraction.v1",
    "city": city,
    "date": target_date,
    "generatedAt": now_iso(),
    "durationMs": int((time.time() - started) * 1000),
    "cacheHit": False,
    "liveFetch": True,
    "fetchedSourceCount": len(selected_sources),
    "sourceCount": len(selected_sources),
    "catalogSourceCount": len(catalog_sources),
    "sourceBudget": max_sources,
    "targetEventCount": max_events,
    "rawEventCount": len(raw_events),
    "newlyExtractedEventCount": len(current_events),
    "rejectedEventCount": len(rejected_events),
    "rejectedEvents": rejected_events[:24],
    "qualityGateStatus": quality_gate_status,
    "qualitySummary": {
      "acceptedBySource": accepted_sources.most_common(12),
      "acceptedByCategory": accepted_categories.most_common(),
      "rejectedByReason": rejection_reasons.most_common(),
      "selectedSourceMix": selected_source_mix,
      "acceptedSourceMix": accepted_source_mix,
      "storedRejectedCount": len(stored_rejected_events),
    },
    "storedEventCount": len(stored),
    "storedRejectedEventCount": len(stored_rejected_events),
    "ingestedEventCount": ingest.get("total", 0),
    "insertedEventCount": ingest.get("inserted", 0),
    "updatedEventCount": ingest.get("updated", 0),
    "eventCount": len(final_events),
    "events": final_events,
    "errors": errors,
  }
  write_json(path, payload)
  return payload


def attr_value(tag: str, name: str) -> str:
  pattern = rf"""\b{name}\s*=\s*["']([^"']+)["']"""
  match = re.search(pattern, tag, re.IGNORECASE)
  return html.unescape(match.group(1).strip()) if match else ""


def image_size_hint(url: str) -> int:
  try:
    request = Request(url, headers={"User-Agent": "DatePipe prototype image picker"}, method="HEAD")
    with urlopen(request, timeout=0.9) as response:
      content_type = response.headers.get("Content-Type", "")
      if not content_type.startswith("image/"):
        return 0
      return int(response.headers.get("Content-Length", "0") or 0)
  except Exception:
    return 0


def first_srcset_url(value: str) -> str:
  if not value:
    return ""
  best_url = ""
  best_width = 0
  for item in value.split(","):
    parts = item.strip().split()
    if not parts:
      continue
    width = 0
    if len(parts) > 1 and parts[1].endswith("w"):
      try:
        width = int(parts[1][:-1])
      except ValueError:
        width = 0
    if width >= best_width:
      best_width = width
      best_url = parts[0]
  return best_url


def usable_image_url(url: str) -> bool:
  if not url or url.startswith("data:"):
    return False
  lower = url.lower()
  if "https:/" in lower and "https://" not in lower:
    return False
  return not any(token in lower for token in IMAGE_SKIP_TOKENS)


def should_resolve_page_image(fallback: str) -> bool:
  if not fallback:
    return True
  lower = fallback.lower()
  return "facebook-share-fallback" in lower or "/resource/image/297888/" in lower


def resolve_page_image(page_url: str, fallback: str = "", keywords: str = "") -> str:
  if not page_url:
    return search_web_image(keywords) or fallback
  if not should_resolve_page_image(fallback):
    return fallback
  with IMAGE_CACHE_LOCK:
    if page_url in IMAGE_CACHE:
      return IMAGE_CACHE[page_url] or fallback
  try:
    page = fetch_text(page_url, timeout=2.2)
    resolved = choose_page_image(page_url, page, fallback)
  except Exception:
    resolved = fallback
  if image_uncertain(resolved, fallback):
    resolved = search_web_image(keywords) or resolved
  with IMAGE_CACHE_LOCK:
    IMAGE_CACHE[page_url] = resolved
  return resolved or fallback


def image_uncertain(value: str, fallback: str = "") -> bool:
  if not value:
    return True
  lower = value.lower()
  if "facebook-share-fallback" in lower or "/resource/image/297888/" in lower:
    return True
  return bool(fallback and value == fallback and should_resolve_page_image(fallback))


def search_web_image(keywords: str) -> str:
  query = " ".join(str(keywords or "").split())
  if not query:
    return ""
  with IMAGE_CACHE_LOCK:
    if query in SEARCH_IMAGE_CACHE:
      return SEARCH_IMAGE_CACHE[query]
  try:
    result = search_duckduckgo_image(query)
  except Exception:
    result = ""
  with IMAGE_CACHE_LOCK:
    SEARCH_IMAGE_CACHE[query] = result
  return result


def search_duckduckgo_image(query: str) -> str:
  search_url = "https://duckduckgo.com/?" + urlencode({
    "q": query,
    "iar": "images",
    "iax": "images",
    "ia": "images",
  })
  request = Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
  with urlopen(request, timeout=3.0) as response:
    page = response.read(200_000).decode("utf-8", errors="ignore")
  match = re.search(r"vqd=([\d-]+)&", page)
  if not match:
    return ""
  image_url = "https://duckduckgo.com/i.js?" + urlencode({
    "l": "us-en",
    "o": "json",
    "q": query,
    "vqd": match.group(1),
    "f": ",,,",
    "p": "1",
  })
  request = Request(image_url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://duckduckgo.com/"})
  with urlopen(request, timeout=3.0) as response:
    payload = json.loads(response.read(500_000).decode("utf-8", errors="ignore"))
  results = []
  for item in payload.get("results", []):
    url = str(item.get("image", ""))
    if not usable_image_url(url):
      continue
    try:
      width = int(item.get("width", 0) or 0)
      height = int(item.get("height", 0) or 0)
    except ValueError:
      width = height = 0
    results.append((width * height, url))
  if not results:
    return ""
  return max(results, key=lambda item: item[0])[1]


def choose_page_image(page_url: str, page_html: str, fallback: str = "") -> str:
  candidates: list[tuple[int, int, str]] = []
  order = 0

  for tag in re.findall(r"<meta\b[^>]*>", page_html, re.IGNORECASE):
    prop = (attr_value(tag, "property") or attr_value(tag, "name")).lower()
    if prop not in {"og:image", "og:image:secure_url", "twitter:image", "image"}:
      continue
    url = normalize_image_url(page_url, attr_value(tag, "content"))
    if usable_image_url(url):
      candidates.append((900_000, order, url))
      order += 1

  for tag in re.findall(r"<img\b[^>]*>", page_html, re.IGNORECASE):
    url = first_srcset_url(attr_value(tag, "srcset")) or attr_value(tag, "src") or attr_value(tag, "data-src")
    url = normalize_image_url(page_url, url)
    if not usable_image_url(url):
      continue
    try:
      width = int(attr_value(tag, "width") or 0)
      height = int(attr_value(tag, "height") or 0)
    except ValueError:
      width = height = 0
    area = width * height
    score = area if area else 0
    candidates.append((score, order, url))
    order += 1

  seen: set[str] = set()
  unique = []
  for score, index, url in candidates:
    if url in seen:
      continue
    seen.add(url)
    unique.append((score, index, url))

  scored = []
  for score, index, url in unique[:8]:
    size_score = image_size_hint(url) if score <= 0 else 0
    scored.append((max(score, size_score), index, url))
  if fallback and usable_image_url(fallback):
    scored.append((1, len(scored) + 1000, fallback))
  if not scored:
    return fallback
  return max(scored, key=lambda item: (item[0], -item[1]))[2]


def normalize_image_url(page_url: str, value: str) -> str:
  value = html.unescape((value or "").strip())
  if value.startswith("//"):
    return f"{urlparse(page_url).scheme}:{value}"
  if value.startswith("http://") or value.startswith("https://"):
    return value
  return urljoin(page_url, value)


def plan_score(idea: dict, slot: dict, settings: dict) -> int:
  score = int(idea.get("fit", 50))
  idea_tags = set(normalize_tags(idea.get("tags", [])) + normalize_tags(idea.get("category", "")))
  slot_tags = set(normalize_tags(slot.get("tags", [])))
  comfort = set(normalize_tags(settings.get("comfort", [])))
  stretch = set(normalize_tags(settings.get("stretch", [])))
  if idea_tags & slot_tags:
    score += 6
  if idea_tags & comfort:
    score += 5
  if idea.get("stretch") in {"safe-stretch", "outside-zone", "spicy"}:
    score += 4 if settings.get("stretchFrequency") != "never" else -20
  if slot.get("status") in {"needs-plan", "open"}:
    score += 4
  if idea.get("duration") == settings.get("preferredDuration"):
    score += 3
  if idea_matches_budget(idea.get("budget", ""), settings):
    score += 5
  elif idea.get("budget") == "premium" and profile_budget(settings) in {"free", "casual"}:
    score -= 14
  if idea_matches_time(idea.get("duration", ""), settings):
    score += 5
  if idea.get("timing") == "flexible" and slot.get("status") == "open":
    score += 2
  return max(1, min(score, 100))


def profile_budget(settings: dict) -> str:
  raw = str(settings.get("budgetComfort") or settings.get("budget") or "").lower()
  if "free" in raw:
    return "free"
  if "premium" in raw:
    return "premium"
  if "nice" in raw:
    return "nice"
  if "casual" in raw:
    return "casual"
  return ""


def idea_matches_budget(idea_budget: str, settings: dict) -> bool:
  budget = profile_budget(settings)
  if not budget:
    return False
  idea_budget = str(idea_budget or "").lower()
  if budget == "premium":
    return idea_budget in {"premium", "nice", "casual"}
  if budget == "nice":
    return idea_budget in {"nice", "casual"}
  if budget == "casual":
    return idea_budget in {"casual", "free"}
  if budget == "free":
    return idea_budget == "free"
  return False


def idea_matches_time(duration: str, settings: dict) -> bool:
  free_time = str(settings.get("freeTime") or "").lower()
  duration = str(duration or "").lower()
  if "2 hour" in free_time:
    return duration == "short"
  if "evening" in free_time:
    return duration in {"normal evening", "long evening"}
  if "half day" in free_time:
    return duration == "half day"
  if "weekend" in free_time:
    return duration in {"half day", "long evening"}
  return False


def localize_idea_for_city(idea: dict, home_base: str) -> dict:
  if home_base.lower() == "vienna":
    return {}
  city = CITY_SOURCES.get(home_base.lower())
  category = str(idea.get("category", "standard")).lower()
  source = (city.get(category) or city.get("standard") or {}) if city else generic_city_source(home_base, category)
  title = source.get("title") or f"{home_base} date shortlist"
  return {
    "title": title,
    "eventUrl": source.get("eventUrl", ""),
    "imageUrl": source.get("imageUrl", ""),
    "sourceName": source.get("sourceName", f"{home_base} events"),
    "why": source.get("why", f"Use {source.get('sourceName', home_base + ' events')} as a live city source, then pick the exact date-night stop from the current listings."),
    "visibleHint": source.get("visibleHint", "I found a city source and will verify the exact stop before reveal."),
    "steps": source.get("steps", ["Open the current city source", "Choose one current listing that fits the date window", "Verify time and booking details"]),
  }


def generic_city_source(home_base: str, category: str) -> dict:
  city = str(home_base or "").strip()
  if not city:
    return {}
  topic_by_category = {
    "cultural": "current exhibitions concerts and cultural events",
    "culinary": "distinctive restaurants tasting menus and date night food",
    "premium": "special dinner tasting menu date night",
    "sportive": "active date ideas walks classes and outdoor events",
    "different": "unusual hidden places and original date ideas",
    "social": "live music comedy and social date night events",
  }
  title_by_category = {
    "cultural": f"{city} cultural shortlist",
    "culinary": f"{city} food date shortlist",
    "premium": f"{city} special dinner shortlist",
    "sportive": f"{city} active date discovery",
    "different": f"{city} obscure date discovery",
    "social": f"{city} live night shortlist",
  }
  topic = topic_by_category.get(category, "events and date night ideas")
  return {
    "title": title_by_category.get(category, f"{city} event-calendar discovery"),
    "eventUrl": "https://www.google.com/search?" + urlencode({"q": f"{city} {topic}"}),
    "sourceName": f"{city} live search",
    "why": "Uses the detected home base and searches for current local destinations.",
    "visibleHint": "I found a local discovery source and will verify the exact stop before reveal.",
    "steps": ["Open the live local search", "Choose one current listing that fits the date window", "Verify time and booking details"],
  }


CITY_SOURCES = {
  "graz": {
    "standard": {"title": "Graz event-calendar shortlist", "eventUrl": "https://www.graztourismus.at/en/events/event-calendar", "sourceName": "Graz events"},
    "cultural": {"title": "Graz cultural event-calendar shortlist", "eventUrl": "https://www.graztourismus.at/en/events/event-calendar", "sourceName": "Graz cultural events"},
    "culinary": {"title": "Graz culinary date shortlist", "eventUrl": "https://www.graztourismus.at/en/food-drinks-shopping", "sourceName": "Graz food and drink"},
    "sportive": {"title": "Graz active event-calendar shortlist", "eventUrl": "https://www.graztourismus.at/en/events/event-calendar", "sourceName": "Graz active events"},
    "different": {"title": "Graz things-to-do shortlist", "eventUrl": "https://www.graztourismus.at/en/see-and-do", "sourceName": "Graz things to do"},
    "romantic": {"title": "Graz romantic things-to-do shortlist", "eventUrl": "https://www.graztourismus.at/en/see-and-do", "sourceName": "Graz things to do"},
    "social": {"title": "Graz social event-calendar shortlist", "eventUrl": "https://www.graztourismus.at/en/events/event-calendar", "sourceName": "Graz social events"},
    "premium": {"title": "Graz restaurant shortlist", "eventUrl": "https://www.graztourismus.at/en/food-drinks-shopping", "sourceName": "Graz restaurants"},
  },
  "linz": {
    "standard": {"title": "Linz event-calendar shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz/events", "sourceName": "Linz events"},
    "cultural": {"title": "Ars Electronica date lead", "eventUrl": "https://ars.electronica.art/center/en/", "sourceName": "Ars Electronica Center Linz"},
    "culinary": {"title": "Linz food-and-drink shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz/food-drink", "sourceName": "Linz food and drink"},
    "sportive": {"title": "Linz active event-calendar shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz/events", "sourceName": "Linz active events"},
    "different": {"title": "Linz things-to-do shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz", "sourceName": "Linz things to do"},
    "romantic": {"title": "Linz Danube date shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz", "sourceName": "Linz things to do"},
    "social": {"title": "Linz social event-calendar shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz/events", "sourceName": "Linz social events"},
    "premium": {"title": "Linz restaurant shortlist", "eventUrl": "https://www.linztourismus.at/en/leisure/discover-linz/food-drink", "sourceName": "Linz restaurants"},
  },
  "salzburg": {
    "standard": {"title": "Salzburg event-calendar shortlist", "eventUrl": "https://www.salzburg.info/en/events/events-calendar", "sourceName": "Salzburg event calendar"},
    "cultural": {"title": "Salzburg cultural event-calendar shortlist", "eventUrl": "https://www.salzburg.info/en/events/events-calendar", "sourceName": "Salzburg cultural events"},
    "culinary": {"title": "Salzburg restaurant-guide shortlist", "eventUrl": "https://www.salzburg.info/en/dining-shopping/restaurant-guide", "sourceName": "Salzburg restaurant guide"},
    "sportive": {"title": "Salzburg active event-calendar shortlist", "eventUrl": "https://www.salzburg.info/en/events/events-calendar", "sourceName": "Salzburg active events"},
    "different": {"title": "Salzburg sights shortlist", "eventUrl": "https://www.salzburg.info/en/sights", "sourceName": "Salzburg sights"},
    "romantic": {"title": "Salzburg romantic sights shortlist", "eventUrl": "https://www.salzburg.info/en/sights", "sourceName": "Salzburg sights"},
    "social": {"title": "Salzburg social event-calendar shortlist", "eventUrl": "https://www.salzburg.info/en/events/events-calendar", "sourceName": "Salzburg social events"},
    "premium": {"title": "Salzburg gourmet restaurant shortlist", "eventUrl": "https://www.salzburg.info/en/dining-shopping/gourmet-restaurants", "sourceName": "Salzburg gourmet restaurants"},
  },
  "innsbruck": {
    "standard": {"title": "Innsbruck event-calendar shortlist", "eventUrl": "https://www.innsbruck.info/en/customs-and-events/events.html", "sourceName": "Innsbruck events"},
    "cultural": {"title": "Innsbruck cultural event-calendar shortlist", "eventUrl": "https://www.innsbruck.info/en/customs-and-events/events.html", "sourceName": "Innsbruck cultural events"},
    "culinary": {"title": "Innsbruck food date shortlist", "eventUrl": "https://www.innsbruck.info/en/food-and-drink.html", "sourceName": "Innsbruck food and drink"},
    "sportive": {"title": "Innsbruck active event-calendar shortlist", "eventUrl": "https://www.innsbruck.info/en/customs-and-events/events.html", "sourceName": "Innsbruck active events"},
    "different": {"title": "Innsbruck things-to-do shortlist", "eventUrl": "https://www.innsbruck.info/en/see-and-do.html", "sourceName": "Innsbruck things to do"},
    "romantic": {"title": "Innsbruck romantic things-to-do shortlist", "eventUrl": "https://www.innsbruck.info/en/see-and-do.html", "sourceName": "Innsbruck things to do"},
    "social": {"title": "Innsbruck social event-calendar shortlist", "eventUrl": "https://www.innsbruck.info/en/customs-and-events/events.html", "sourceName": "Innsbruck social events"},
    "premium": {"title": "Innsbruck restaurant shortlist", "eventUrl": "https://www.innsbruck.info/en/food-and-drink.html", "sourceName": "Innsbruck restaurants"},
  },
}


def date_label_for(value: str) -> str:
  parsed = parse_date_or_none(value)
  if not parsed:
    return value
  return parsed.strftime("%a %m-%d")


def search_window_dates(center_value: object, settings: dict | None = None, radius_days: int = 2) -> list[dict]:
  center = parse_date_or_none(center_value) or date.today()
  if center < date.today():
    center = date.today()
  start, end = week_bounds(center)
  blocked_days = {item.lower() for item in normalize_tags((settings or {}).get("impossibleDays", []))}
  out = []
  for offset in range(7):
    day = start + timedelta(days=offset)
    if day < date.today():
      continue
    if day.strftime("%A").lower() in blocked_days:
      continue
    iso = day.isoformat()
    out.append({"iso": iso, "label": date_label_for(iso)})
  if out:
    return out
  iso = max(center, end).isoformat()
  return [{"iso": iso, "label": date_label_for(iso)}]


def slot_for_request(state: dict, slot_id: str | None = None, search_date: object = None) -> dict:
  slots = effective_slots(state)
  if not slots:
    return {}
  if slot_id:
    found = next((item for item in slots if item.get("id") == slot_id), None)
    if found:
      return found
  target = parse_date_or_none(search_date)
  if target:
    return min(slots, key=lambda item: abs(((parse_date_or_none(item.get("start")) or target) - target).days))
  return next((item for item in slots if item.get("status") in {"needs-plan", "open", "idea", "candidate"}), slots[0])


GENERATE_MIN_RELEASE_OPTIONS = 6


def proposal_identity(option: dict) -> tuple[str, str]:
  return (
    re.sub(r"\s+", " ", str(option.get("title") or "").strip().lower()),
    str(option.get("eventUrl") or "").strip().lower(),
  )


def merge_proposal_options(primary: list[dict], *fallback_groups: list[dict], limit: int = 10) -> list[dict]:
  merged = []
  seen: set[tuple[str, str]] = set()
  for group in (primary, *fallback_groups):
    for option in group:
      if not isinstance(option, dict):
        continue
      key = proposal_identity(option)
      if key in seen:
        continue
      seen.add(key)
      merged.append(option)
      if len(merged) >= limit:
        return merged
  return merged


def align_options_to_search_date(options: list[dict], search_date: object) -> list[dict]:
  target = parse_date_or_none(search_date)
  if not target:
    return options
  aligned = []
  for option in options:
    if not isinstance(option, dict):
      continue
    row = {**option}
    row["dateChoice"] = target.isoformat()
    row["dateLabel"] = target.strftime("%a %m-%d")
    aligned.append(row)
  return aligned


def generate_options(state: dict, slot_id: str | None = None, mood: str = "", search_date: object = None) -> tuple[list[dict], str]:
  ai_options, source = generate_ai_options(state, slot_id, mood, search_date)
  if len(ai_options) >= GENERATE_MIN_RELEASE_OPTIONS:
    return ai_options, source
  local_options = align_options_to_search_date(generate_local_options(state, slot_id, mood, search_date), search_date)
  if ai_options and len(ai_options) + len(local_options) >= GENERATE_MIN_RELEASE_OPTIONS:
    return merge_proposal_options(ai_options, local_options), f"{source}+local-backfill"
  web_options = generate_web_options(state, slot_id, mood, search_date)
  if ai_options and len(ai_options) + len(local_options) + len(web_options) >= GENERATE_MIN_RELEASE_OPTIONS:
    return merge_proposal_options(ai_options, local_options, web_options), f"{source}+local-web-backfill"
  if not ai_options and len(web_options) >= GENERATE_MIN_RELEASE_OPTIONS:
    return web_options, "web-search"
  if ai_options:
    return merge_proposal_options(ai_options, local_options, web_options), f"{source}+local-backfill"
  if web_options:
    return merge_proposal_options(web_options, local_options), "web-search+local-backfill"
  return local_options, source


def generate_ai_options(state: dict, slot_id: str | None = None, mood: str = "", search_date: object = None) -> tuple[list[dict], str]:
  provider = os.environ.get("DATEPIPE_AI_PROVIDER", "auto").strip().lower()
  if provider in {"auto", "gemini"} and gemini_api_key():
    options, source = generate_gemini_options(state, slot_id, mood, search_date)
    if options or provider == "gemini":
      return options, source
  if provider not in {"auto", "openai"}:
    return [], "gemini:missing" if provider == "gemini" else "local"
  api_key = os.environ.get("OPENAI_API_KEY", "").strip()
  ok, preflight = preflight_openai_key(api_key)
  if not ok:
    return [], preflight
  slot = slot_for_request(state, slot_id, search_date)
  settings = state.get("profile", {})
  dates = search_window_dates(search_date or slot.get("start"), settings)
  payload = {
    "model": os.environ.get("DATEPIPE_OPENAI_MODEL", "gpt-4.1-mini"),
    "tools": [{"type": "web_search"}],
    "tool_choice": "auto",
    "input": proposal_prompt(state, slot, dates, mood),
    "max_output_tokens": 2400,
  }
  request = Request(
    OPENAI_RESPONSES_URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={
      "Authorization": f"Bearer {api_key}",
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urlopen(request, timeout=35) as response:
      data = json.loads(response.read().decode("utf-8"))
    options = normalize_ai_options(extract_response_text(data), state, slot, dates)
    return options, "openai-web" if options else "openai-empty"
  except Exception as exc:
    return [], f"fallback:{type(exc).__name__}"


def generate_gemini_options(state: dict, slot_id: str | None = None, mood: str = "", search_date: object = None) -> tuple[list[dict], str]:
  if not gemini_api_key():
    return [], "gemini:missing"
  slot = slot_for_request(state, slot_id, search_date)
  settings = state.get("profile", {})
  dates = search_window_dates(search_date or slot.get("start"), settings)
  text, source = gemini_generate_text(
    proposal_prompt(state, slot, dates, mood),
    max_output_tokens=2600,
    temperature=0.85,
    use_google_search=True,
    timeout=35,
  )
  if not text:
    return [], source
  options = normalize_ai_options(text, state, slot, dates)
  return options, "gemini-search" if options and source == "gemini" else source


def generate_web_options(state: dict, slot_id: str | None = None, mood: str = "", search_date: object = None) -> list[dict]:
  settings = state.get("profile", {})
  slot = slot_for_request(state, slot_id, search_date)
  dates = search_window_dates(search_date or slot.get("start"), settings)
  city = str(settings.get("homeBase") or "Vienna").strip() or "Vienna"
  keywords = normalize_tags(settings.get("searchKeywords", []))
  query_bits = [city, "events", dates[0]["iso"], "date night"]
  query_bits.extend(keywords[:2])
  if mood:
    query_bits.extend(normalize_tags(mood)[:2])
  results = search_duckduckgo_results(" ".join(query_bits), limit=6)
  options = []
  for result in results:
    title = result["title"]
    url = result["url"]
    if not title or not url.startswith(("http://", "https://")):
      continue
    if looks_like_generic_search_page(url):
      continue
    date_item = dates[len(options) % len(dates)]
    category = infer_category(title, keywords)
    source_name = urlparse(url).netloc.replace("www.", "") or "event source"
    if not proposal_matches_home_base(title, url, source_name, city):
      continue
    options.append({
      "id": new_id("proposal"),
      "sourceIdeaId": "web-" + re.sub(r"[^a-z0-9]+", "-", f"{source_name}-{title}".lower()).strip("-")[:64],
      "slotId": slot.get("id"),
      "title": title,
      "score": 88 - len(options),
      "mode": "solo-secret",
      "category": category,
      "duration": duration_for_settings(settings),
      "timing": "precise",
      "stretch": stretch_for_category(category, settings),
      "why": f"Found for {city} around {date_item['label']} using your profile and search keywords.",
      "eventUrl": url,
      "imageUrl": "",
      "sourceName": source_name,
      "variety": "fresh",
      "visibleHint": f"Surprise {category} date. I will handle the details.",
      "steps": ["Check date and availability", "Book or save the main stop", "Prepare a small reveal hint"],
      "todos": [
        {"id": new_id("todo"), "text": "Verify current date and availability", "done": False},
        {"id": new_id("todo"), "text": "Book or save the main stop", "done": False},
        {"id": new_id("todo"), "text": "Prepare reveal hint", "done": False},
      ],
      "backup": "Pick one nearby low-effort fallback before departure.",
      "dateChoice": date_item["iso"],
      "dateLabel": date_item["label"],
    })
  return options


def search_duckduckgo_results(query: str, limit: int = 8) -> list[dict]:
  search_url = "https://duckduckgo.com/html/?" + urlencode({"q": query})
  request = Request(search_url, headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
  try:
    with urlopen(request, timeout=5.0) as response:
      page = response.read(350_000).decode("utf-8", errors="ignore")
  except Exception:
    return []
  rows = []
  seen: set[str] = set()
  for match in re.finditer(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.IGNORECASE | re.DOTALL):
    url = clean_duckduckgo_url(html.unescape(match.group(1)))
    title = strip_tags(match.group(2))
    if not url or url in seen or not title:
      continue
    seen.add(url)
    rows.append({"title": title, "url": url})
    if len(rows) >= limit:
      break
  return rows


def reverse_geocode_city(lat_value: object, lon_value: object) -> str:
  try:
    lat = float(lat_value)
    lon = float(lon_value)
  except (TypeError, ValueError):
    return ""
  if not (-90 <= lat <= 90 and -180 <= lon <= 180):
    return ""
  url = "https://nominatim.openstreetmap.org/reverse?" + urlencode({
    "format": "jsonv2",
    "lat": f"{lat:.6f}",
    "lon": f"{lon:.6f}",
    "zoom": "10",
    "addressdetails": "1",
  })
  request = Request(url, headers={"User-Agent": "DatePipe prototype reverse geocoder"})
  try:
    with urlopen(request, timeout=5.0) as response:
      data = json.loads(response.read(100_000).decode("utf-8", errors="ignore"))
  except Exception:
    return ""
  address = data.get("address") if isinstance(data, dict) else {}
  if not isinstance(address, dict):
    return ""
  for key in ("city", "town", "village", "municipality", "county", "state"):
    value = str(address.get(key) or "").strip()
    if value:
      return value
  return ""


def clean_duckduckgo_url(value: str) -> str:
  if value.startswith("//"):
    value = "https:" + value
  parsed = urlparse(value)
  if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
    target = parse_qs(parsed.query).get("uddg", [""])[0]
    return target or value
  return value


def strip_tags(value: str) -> str:
  value = re.sub(r"<[^>]+>", " ", value)
  return " ".join(html.unescape(value).split())


def looks_like_generic_search_page(url: str) -> bool:
  lower = url.lower()
  return any(host in lower for host in ("google.com/search", "duckduckgo.com/", "bing.com/search"))


LINK_STOPWORDS = {
  "and",
  "art",
  "at",
  "calendar",
  "date",
  "de",
  "discover",
  "drink",
  "en",
  "event",
  "events",
  "for",
  "food",
  "guide",
  "https",
  "leisure",
  "night",
  "shortlist",
  "the",
  "to",
  "tourismus",
  "tourism",
  "www",
}

BROAD_DISCOVERY_TOKENS = {
  "calendar",
  "date",
  "dates",
  "event",
  "events",
  "guide",
  "lead",
  "restaurant",
  "restaurants",
  "shortlist",
  "sights",
  "things",
}


CITY_ALIASES = {
  "vienna": {"vienna", "wien"},
  "graz": {"graz"},
  "linz": {"linz"},
  "salzburg": {"salzburg"},
}


def proposal_matches_home_base(title: str, event_url: str, source_name: str, home_base: str) -> bool:
  city = str(home_base or "Vienna").strip().lower()
  if not city or city == "vienna":
    return True
  text = " ".join([title, event_url, source_name]).lower()
  wanted = CITY_ALIASES.get(city, {city})
  forbidden = set().union(*(aliases for key, aliases in CITY_ALIASES.items() if key != city))
  if any(token in text for token in forbidden):
    return False
  return any(token in text for token in wanted)


def proposal_link_relevance(title: str, event_url: str, source_name: str = "", home_base: str = "") -> tuple[bool, str]:
  title_tokens = meaningful_link_tokens(title)
  url_tokens = meaningful_link_tokens(" ".join([urlparse(event_url).netloc, urlparse(event_url).path]))
  source_tokens = meaningful_link_tokens(source_name)
  city_tokens = meaningful_link_tokens(home_base)
  if not event_url.startswith(("http://", "https://")):
    return False, "missing-url"
  if not proposal_matches_home_base(title, event_url, source_name, home_base):
    return False, "wrong-city"
  if title_tokens & (url_tokens | source_tokens):
    return True, "token-match"
  if is_broad_discovery_link(event_url, source_name) and title_tokens & city_tokens and title_tokens & BROAD_DISCOVERY_TOKENS:
    return True, "broad-discovery-title"
  return False, "title-url-mismatch"


def proposal_has_relevant_link(title: str, event_url: str, source_name: str = "", home_base: str = "") -> bool:
  ok, _reason = proposal_link_relevance(title, event_url, source_name, home_base)
  return ok


def meaningful_link_tokens(value: object) -> set[str]:
  tokens = {
    token
    for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
    if len(token) >= 3 and token not in LINK_STOPWORDS
  }
  if "things" in tokens and "todo" in tokens:
    tokens.add("things-to-do")
  return tokens


def is_broad_discovery_link(event_url: str, source_name: str = "") -> bool:
  parsed = urlparse(event_url)
  text = " ".join([parsed.netloc, parsed.path, source_name]).lower()
  broad_markers = (
    "events",
    "event-calendar",
    "food-drinks-shopping",
    "food-drink",
    "restaurant-guide",
    "gourmet-restaurants",
    "see-and-do",
    "discover-linz",
    "sights",
  )
  return any(marker in text for marker in broad_markers)


def infer_category(title: str, keywords: list[str]) -> str:
  text = " ".join([title, *keywords]).lower()
  if any(token in text for token in ("food", "dinner", "restaurant", "wine", "cooking", "tasting")):
    return "culinary"
  if any(token in text for token in ("concert", "music", "theater", "museum", "art", "festival", "exhibition")):
    return "cultural"
  if any(token in text for token in ("dance", "sport", "walk", "hike", "bike", "active")):
    return "sportive"
  if any(token in text for token in ("comedy", "escape", "weird", "special", "unusual")):
    return "different"
  return "standard"


def duration_for_settings(settings: dict) -> str:
  free_time = str(settings.get("freeTime") or "").lower()
  if "2 hour" in free_time:
    return "short"
  if "half day" in free_time:
    return "half day"
  if "weekend" in free_time:
    return "half day"
  return "normal evening"


def stretch_for_category(category: str, settings: dict) -> str:
  stretch = set(normalize_tags(settings.get("stretch", [])))
  if category in stretch or stretch:
    return "safe-stretch"
  return "familiar"


def proposal_prompt(state: dict, slot: dict, dates: list[dict], mood: str) -> str:
  settings = state.get("profile", {})
  home_base = str(settings.get("homeBase") or "Vienna")
  safe_state = {
    "profile": settings,
    "targetSlot": slot,
    "allowedDates": dates,
    "recentPlans": state.get("plans", [])[:8],
    "seedIdeaTitlesToAvoidRepeating": [item.get("title", "") for item in state.get("ideas", [])[:20]],
  }
  return (
    "Find current, real, original date-night destinations, events, venues, or bookable activities. "
    "Use web search. Include a mix of obvious reliable picks and obscure local discoveries: "
    "small venues, unusual workshops, niche performances, pop-ups, neighborhood spots, scenic micro-trips, "
    "and low-key places that still have a real public page. Respect the profile exactly, especially homeBase/city, travel radius, "
    "budget comfort, free time, blocked days, comfort/stretch tastes, and allowedDates. "
    "Do not return generic evergreen placeholders unless a specific current event is impossible. "
    "Prefer official event, venue, museum, ticket, city calendar, or organizer pages. "
    "Do not repeat the seed titles. Return only JSON, no markdown, with this shape: "
    "{\"options\":[{\"title\":\"...\",\"dateChoice\":\"YYYY-MM-DD\",\"dateLabel\":\"Fri 05-22\","
    "\"category\":\"standard|sportive|different|cultural|culinary|romantic|social|premium\","
    "\"duration\":\"short|normal evening|long evening|half day\",\"timing\":\"precise|flexible\","
    "\"stretch\":\"familiar|safe-stretch|outside-zone|spicy\",\"why\":\"...\","
    "\"eventUrl\":\"https://...\",\"sourceName\":\"...\",\"visibleHint\":\"...\","
    "\"variety\":\"obvious|obscure|fresh\",\"steps\":[\"...\"],\"backup\":\"...\"}]}. "
    "Every option must be in or near the homeBase city and must fit one allowed date.\n\n"
    f"State:\n{json.dumps(safe_state, ensure_ascii=False)}\n\n"
    f"Extra request or mood:\n{str(mood or '').strip()}"
  )


def normalize_ai_options(text: str, state: dict, slot: dict, dates: list[dict]) -> list[dict]:
  parsed = parse_json_object_from_text(text)
  rows = parsed.get("options") if isinstance(parsed, dict) else []
  if not isinstance(rows, list):
    return []
  allowed_dates = {item["iso"]: item["label"] for item in dates}
  settings = state.get("profile", {})
  home_base = str(settings.get("homeBase") or "Vienna")
  options = []
  for row in rows[:10]:
    if not isinstance(row, dict):
      continue
    title = str(row.get("title") or "").strip()
    event_url = str(row.get("eventUrl") or "").strip()
    if not title or not event_url.startswith(("http://", "https://")):
      continue
    requested_date = str(row.get("dateChoice") or "").strip()
    if requested_date not in allowed_dates:
      requested_date = next(iter(allowed_dates))
    category = choice_or_default(row.get("category"), {"standard", "sportive", "different", "cultural", "culinary", "romantic", "social", "premium"}, "standard")
    duration = choice_or_default(row.get("duration"), {"short", "normal evening", "long evening", "half day"}, settings.get("preferredDuration") or "normal evening")
    timing = choice_or_default(row.get("timing"), {"precise", "flexible"}, "precise")
    stretch = choice_or_default(row.get("stretch"), {"familiar", "safe-stretch", "outside-zone", "spicy"}, "safe-stretch")
    steps = row.get("steps") if isinstance(row.get("steps"), list) else []
    source_name = str(row.get("sourceName") or urlparse(event_url).netloc or "event source").strip()
    if not proposal_has_relevant_link(title, event_url, source_name, home_base):
      continue
    image_url = resolve_page_image(event_url, str(row.get("imageUrl") or ""), f"{title} {settings.get('homeBase', '')} event")
    source_id = "ai-" + re.sub(r"[^a-z0-9]+", "-", f"{source_name}-{title}".lower()).strip("-")[:64]
    options.append({
      "id": new_id("proposal"),
      "sourceIdeaId": source_id or new_id("source"),
      "slotId": slot.get("id"),
      "title": title,
      "score": 94 - len(options),
      "mode": "solo-secret",
      "category": category,
      "duration": duration,
      "timing": timing,
      "stretch": stretch,
      "why": str(row.get("why") or "Found from current event sources for this profile and date.").strip(),
      "eventUrl": event_url,
      "imageUrl": image_url,
      "sourceName": source_name,
      "variety": choice_or_default(row.get("variety"), {"obvious", "obscure", "fresh"}, "fresh"),
      "visibleHint": str(row.get("visibleHint") or f"Surprise {category} date. I will handle the details.").strip(),
      "steps": [str(step).strip() for step in steps if str(step).strip()][:5],
      "todos": [
        {"id": new_id("todo"), "text": "Check current availability", "done": False},
        {"id": new_id("todo"), "text": "Book or save the main stop", "done": False},
        {"id": new_id("todo"), "text": "Prepare reveal hint", "done": False},
      ],
      "backup": str(row.get("backup") or "Pick one nearby low-effort fallback before departure.").strip(),
      "dateChoice": requested_date,
      "dateLabel": allowed_dates[requested_date],
    })
  return options


def parse_json_object_from_text(text: str) -> dict:
  text = str(text or "").strip()
  if not text:
    return {}
  try:
    parsed = json.loads(text)
    return parsed if isinstance(parsed, dict) else {}
  except json.JSONDecodeError:
    pass
  match = re.search(r"\{.*\}", text, re.DOTALL)
  if not match:
    return {}
  try:
    parsed = json.loads(match.group(0))
    return parsed if isinstance(parsed, dict) else {}
  except json.JSONDecodeError:
    return {}


def choice_or_default(value: object, allowed: set[str], default: object) -> str:
  clean = str(value or "").strip().lower()
  return clean if clean in allowed else str(default)


def generate_local_options(state: dict, slot_id: str | None = None, mood: str = "", search_date: object = None) -> list[dict]:
  settings = state.get("profile", {})
  home_base = str(settings.get("homeBase", "Vienna") or "Vienna")
  slot = slot_for_request(state, slot_id, search_date)
  dates = search_window_dates(search_date or slot.get("start"), settings)
  options = []
  seen_localized_sources: set[tuple[str, str]] = set()
  mood_tags = set(normalize_tags(mood))
  profile_keywords = set(normalize_tags(settings.get("searchKeywords", [])))
  for idea in state.get("ideas", []):
    event_date = parse_date_or_none(idea.get("eventDate"))
    if event_date and event_date < date.today():
      continue
    score = plan_score(idea, slot, settings)
    idea_tags = set(normalize_tags(idea.get("tags", [])) + normalize_tags(idea.get("category", "")))
    if mood_tags and idea_tags & mood_tags:
      score += 8
    idea_text = " ".join([
      str(idea.get("title", "")),
      str(idea.get("why", "")),
      " ".join(normalize_tags(idea.get("tags", []))),
      str(idea.get("category", "")),
    ]).lower()
    if profile_keywords and any(keyword in idea_text for keyword in profile_keywords):
      score += 10
    localized = localize_idea_for_city(idea, home_base)
    if localized:
      localized_key = (str(localized.get("title", "")).lower(), str(localized.get("eventUrl", "")).lower())
      if localized_key in seen_localized_sources:
        continue
      seen_localized_sources.add(localized_key)
    title = localized.get("title", idea.get("title", "Date idea"))
    keyword_text = " ".join(profile_keywords)
    keywords = idea.get("imageSearchQuery") or f"{title} {keyword_text} {localized.get('sourceName', idea.get('sourceName', ''))} Vienna date event"
    event_url = localized.get("eventUrl", idea.get("eventUrl", ""))
    image_url = resolve_page_image(event_url, localized.get("imageUrl", idea.get("imageUrl", "")), keywords.replace("Vienna", home_base))
    date_item = dates[len(options) % len(dates)]
    options.append({
      "id": new_id("proposal"),
      "sourceIdeaId": idea.get("id"),
      "slotId": slot.get("id"),
      "title": title,
      "score": min(score, 100),
      "mode": "solo-secret" if "secret" in idea_tags else "open",
      "category": idea.get("category", "standard"),
      "duration": idea.get("duration", "normal evening"),
      "timing": idea.get("timing", "flexible"),
      "stretch": idea.get("stretch", "familiar"),
      "why": localized.get("why", idea.get("why", "")),
      "eventUrl": event_url,
      "imageUrl": image_url,
      "sourceName": localized.get("sourceName", idea.get("sourceName", "")),
      "variety": variety_label(idea),
      "visibleHint": localized.get("visibleHint", visible_hint_for(idea)),
      "steps": localized.get("steps", idea.get("steps", [])),
      "todos": [
        {"id": new_id("todo"), "text": "Confirm time window", "done": False},
        {"id": new_id("todo"), "text": "Book or save the main stop", "done": False},
        {"id": new_id("todo"), "text": "Prepare reveal hint", "done": False},
      ],
      "backup": backup_for(idea),
      "dateChoice": date_item["iso"],
      "dateLabel": date_item["label"],
    })
  return rank_varied_options(options, slot, settings, search_date, mood)


def release_evaluation_options(state: dict, search_date: object = None) -> list[dict]:
  settings = state.get("profile", {})
  home_base = str(settings.get("homeBase", "Vienna") or "Vienna")
  slot = slot_for_request(state, search_date=search_date)
  dates = search_window_dates(search_date or slot.get("start"), settings)
  options = []
  seen: set[tuple[str, str]] = set()
  for idea in state.get("ideas", []):
    event_date = parse_date_or_none(idea.get("eventDate"))
    if event_date and event_date < date.today():
      continue
    localized = localize_idea_for_city(idea, home_base)
    title = localized.get("title", idea.get("title", "Date idea"))
    event_url = localized.get("eventUrl", idea.get("eventUrl", ""))
    key = (str(title).lower(), str(event_url).lower())
    if key in seen:
      continue
    seen.add(key)
    date_item = dates[len(options) % max(1, len(dates))]
    options.append({
      "id": new_id("proposal"),
      "sourceIdeaId": idea.get("id"),
      "slotId": slot.get("id"),
      "title": title,
      "score": min(plan_score(idea, slot, settings), 100),
      "mode": "solo-secret" if "secret" in normalize_tags(idea.get("tags", [])) else "open",
      "category": idea.get("category", "standard"),
      "duration": idea.get("duration", "normal evening"),
      "timing": idea.get("timing", "flexible"),
      "stretch": idea.get("stretch", "familiar"),
      "why": localized.get("why", idea.get("why", "")),
      "eventUrl": event_url,
      "imageUrl": localized.get("imageUrl", idea.get("imageUrl", "")),
      "sourceName": localized.get("sourceName", idea.get("sourceName", "")),
      "variety": variety_label(idea),
      "visibleHint": localized.get("visibleHint", visible_hint_for(idea)),
      "steps": localized.get("steps", idea.get("steps", [])),
      "backup": backup_for(idea),
      "dateChoice": date_item["iso"],
      "dateLabel": date_item["label"],
    })
    if len(options) >= 8:
      break
  return rank_varied_options(options, slot, settings, search_date, "")


def variety_label(idea: dict) -> str:
  tags = set(normalize_tags(idea.get("tags", [])) + normalize_tags(idea.get("category", "")) + normalize_tags(idea.get("stretch", "")))
  if tags & {"outside-zone", "spicy", "different", "unusual food", "new district", "explore"}:
    return "obscure"
  if tags & {"standard", "culinary", "romantic", "familiar"}:
    return "obvious"
  return "fresh"


def rank_varied_options(options: list[dict], slot: dict, settings: dict, search_date: object = None, mood: str = "") -> list[dict]:
  seed = ":".join([
    str(settings.get("homeBase", "")),
    str(search_date or slot.get("start", "")),
    str(mood or ""),
    ",".join(normalize_tags(settings.get("searchKeywords", []))),
    ",".join(normalize_tags(settings.get("comfort", []))),
    ",".join(normalize_tags(settings.get("stretch", []))),
  ])
  rotation = sum(ord(char) for char in seed)
  buckets = {"obvious": [], "obscure": [], "fresh": []}
  for option in options:
    buckets.setdefault(option.get("variety", "fresh"), []).append(option)
  for bucket_name, bucket in buckets.items():
    bucket.sort(key=lambda item: (item.get("score", 0), -stable_rotation(item.get("sourceIdeaId", ""), rotation)), reverse=True)
  order = ["obvious", "obscure", "fresh", "obscure", "obvious", "fresh"]
  ranked = []
  while any(buckets.values()):
    for bucket_name in order:
      bucket = buckets.get(bucket_name, [])
      if bucket:
        ranked.append(bucket.pop(0))
  return ranked


def stable_rotation(value: str, rotation: int) -> int:
  return (sum(ord(char) for char in str(value)) + rotation) % 997


def visible_hint_for(idea: dict) -> str:
  category = idea.get("category", "date")
  timing = "more details" if idea.get("timing") == "precise" else idea.get("timing", "flexible")
  if idea.get("stretch") in {"outside-zone", "spicy"}:
    return f"Surprise {category} date. Wear something comfortable. Timing is {timing}."
  return f"{category.title()} date. I will handle the details. Timing is {timing}."


def backup_for(idea: dict) -> str:
  if idea.get("timing") == "precise":
    return "Keep a nearby cafe or bar ready in case the main event slips."
  if idea.get("category") == "sportive":
    return "Have a calm food stop ready if the active part is too much."
  return "Pick one nearby low-effort fallback before departure."


def plan_itinerary_from_proposal(proposal: dict) -> list[dict]:
  date_choice = str(proposal.get("dateChoice") or "")
  duration = str(proposal.get("duration") or "")
  timing = str(proposal.get("timing") or "")
  if duration == "short":
    anchors = [("warmup", "Arrive nearby and keep the reveal soft."), ("main", proposal.get("title") or "Main stop"), ("close", "Leave room for a drink, walk, or early exit.")]
  elif duration == "half day":
    anchors = [("warmup", "Start with an easy food or coffee anchor."), ("main", proposal.get("title") or "Main stop"), ("close", "Add a scenic pause before heading home.")]
  else:
    anchors = [("warmup", "Reserve a quiet arrival buffer."), ("main", proposal.get("title") or "Main stop"), ("close", "Keep one nearby after-stop ready.")]
  return [
    {
      "id": new_id("step"),
      "kind": kind,
      "date": date_choice,
      "timing": timing if kind == "main" else "flexible",
      "text": str(text),
      "status": "planned" if kind == "main" else "optional",
    }
    for kind, text in anchors
  ]


def accomplice_brief_from_proposal(proposal: dict) -> dict:
  return {
    "enabled": proposal.get("mode") in {"accomplice", "solo-secret"} or bool(proposal.get("eventUrl")),
    "ask": "Help verify timing, booking friction, and whether the reveal hint gives away too much.",
    "shareSafeHint": proposal.get("visibleHint", ""),
    "privateContext": proposal.get("why", ""),
    "doNotShare": ["exact destination until reveal", "private notes", "backup plan"],
  }


def booking_checklist_from_proposal(proposal: dict) -> list[dict]:
  items = [
    "Verify current date, time, price, and opening status",
    "Confirm booking or ticket requirement",
    "Save the official link and address",
    "Prepare a small reveal hint",
    "Choose a weather or fatigue fallback",
  ]
  return [{"id": new_id("todo"), "text": item, "done": False} for item in items]


def plan_date_value(plan: dict) -> str:
  value = str(plan.get("dateChoice") or "").strip()
  if value:
    return value[:10]
  for step in plan.get("itinerary", []) if isinstance(plan.get("itinerary"), list) else []:
    if isinstance(step, dict) and step.get("date"):
      return str(step.get("date"))[:10]
  return ""


def same_week(left: object, right: object) -> bool:
  left_day = parse_date_or_none(left)
  right_day = parse_date_or_none(right)
  if not left_day or not right_day:
    return False
  return week_bounds(left_day)[0] == week_bounds(right_day)[0]


def schedule_conflicts_for_plan(state: dict, plan: dict) -> list[dict]:
  target = plan_date_value(plan)
  if not target:
    return []
  warnings = []
  for existing in state.get("plans", []):
    if existing.get("id") == plan.get("id"):
      continue
    if existing.get("status") not in {"candidate", "needs-booking", "booked"}:
      continue
    existing_date = plan_date_value(existing)
    if existing_date and same_week(existing_date, target):
      warnings.append({
        "type": "same_week_plan",
        "severity": "warning",
        "message": "There is already another active plan in this calendar week.",
        "date": existing_date,
        "title": existing.get("title", "Existing plan"),
        "planId": existing.get("id", ""),
      })
  for slot in effective_slots(state):
    if slot.get("status") not in {"booked", "done"}:
      continue
    if same_week(slot.get("start"), target) or same_week(slot.get("end"), target):
      warnings.append({
        "type": "same_week_booked_slot",
        "severity": "warning",
        "message": "This week already has a booked date window.",
        "date": slot.get("start", ""),
        "title": slot.get("title", "Booked date"),
        "slotId": slot.get("id", ""),
      })
  return warnings


def make_plan_from_proposal(proposal: dict) -> dict:
  todos = proposal.get("todos") if isinstance(proposal.get("todos"), list) else []
  return {
    "id": new_id("plan"),
    "slotId": proposal.get("slotId"),
    "title": proposal.get("title", "Untitled plan"),
    "mode": proposal.get("mode", "solo-secret"),
    "status": "candidate",
    "reveal": "at departure",
    "visibleHint": proposal.get("visibleHint", ""),
    "hiddenNotes": proposal.get("why", ""),
    "dateChoice": proposal.get("dateChoice", ""),
    "dateLabel": proposal.get("dateLabel", ""),
    "todos": todos or booking_checklist_from_proposal(proposal),
    "attachments": [],
    "eventUrl": proposal.get("eventUrl", ""),
    "imageUrl": proposal.get("imageUrl", ""),
    "sourceName": proposal.get("sourceName", ""),
    "steps": proposal.get("steps", []),
    "itinerary": plan_itinerary_from_proposal(proposal),
    "accompliceBrief": accomplice_brief_from_proposal(proposal),
    "weatherFallback": "If weather, transit, or energy breaks the plan, switch to the saved nearby fallback without revealing the original idea.",
    "backup": proposal.get("backup", ""),
    "createdAt": now_iso(),
  }


def preflight_openai_key(api_key: str) -> tuple[bool, str]:
  if not api_key:
    return False, "missing"
  request = Request(OPENAI_MODELS_URL, headers={"Authorization": f"Bearer {api_key}"})
  try:
    with urlopen(request, timeout=8) as response:
      if 200 <= response.status < 300:
        return True, "ok"
  except HTTPError as exc:
    if exc.code in (401, 403):
      return False, f"unusable:{exc.code}"
    return False, f"inconclusive:{exc.code}"
  except URLError as exc:
    return False, f"inconclusive:{exc.reason}"
  except TimeoutError:
    return False, "inconclusive:timeout"
  return False, "inconclusive"


def gemini_api_key() -> str:
  return os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip()


def llm_provider_order(purpose: str = "default") -> list[str]:
  specific = os.environ.get(f"DATEPIPE_{purpose.upper()}_LLM_PROVIDER", "").strip().lower()
  selected = specific or os.environ.get("DATEPIPE_LLM_PROVIDER", "").strip().lower() or os.environ.get("DATEPIPE_AI_PROVIDER", "auto").strip().lower()
  if selected and selected != "auto":
    return [selected]
  raw = os.environ.get("DATEPIPE_LLM_FALLBACKS", "openai,gemini")
  return [item.strip().lower() for item in raw.split(",") if item.strip()]


def llm_generate_text(
  prompt: str,
  *,
  purpose: str = "default",
  max_output_tokens: int = 1200,
  temperature: float = 0.7,
  use_web_search: bool = False,
  timeout: float = 25,
) -> dict:
  errors = []
  for provider in llm_provider_order(purpose):
    if provider == "openai":
      result = openai_generate_text(prompt, max_output_tokens=max_output_tokens, use_web_search=use_web_search, timeout=timeout)
    elif provider == "gemini":
      text, source = gemini_generate_text(prompt, max_output_tokens=max_output_tokens, temperature=temperature, use_google_search=use_web_search, timeout=timeout)
      result = {
        "ok": bool(text),
        "provider": "gemini",
        "model": os.environ.get("DATEPIPE_GEMINI_MODEL", "gemini-2.0-flash"),
        "text": text,
        "status": source,
      }
    else:
      result = {"ok": False, "provider": provider or "none", "model": "", "text": "", "status": "unsupported-provider"}
    if result.get("ok") and result.get("text"):
      return result
    errors.append(f"{result.get('provider')}:{result.get('status')}")
  return {"ok": False, "provider": "", "model": "", "text": "", "status": "|".join(errors) or "no-provider"}


def openai_generate_text(prompt: str, *, max_output_tokens: int, use_web_search: bool = False, timeout: float = 25) -> dict:
  api_key = os.environ.get("OPENAI_API_KEY", "").strip()
  ok, preflight = preflight_openai_key(api_key)
  if not ok:
    return {"ok": False, "provider": "openai", "model": "", "text": "", "status": preflight}
  model = os.environ.get("DATEPIPE_OPENAI_MODEL", "gpt-4.1-mini")
  payload: dict[str, object] = {
    "model": model,
    "input": prompt,
    "max_output_tokens": max_output_tokens,
  }
  if use_web_search:
    payload["tools"] = [{"type": "web_search"}]
    payload["tool_choice"] = "auto"
  request = Request(
    OPENAI_RESPONSES_URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    method="POST",
  )
  try:
    with urlopen(request, timeout=timeout) as response:
      data = json.loads(response.read().decode("utf-8"))
    return {"ok": True, "provider": "openai", "model": model, "text": extract_response_text(data), "status": "ok"}
  except HTTPError as exc:
    return {"ok": False, "provider": "openai", "model": model, "text": "", "status": f"http:{exc.code}"}
  except Exception as exc:
    return {"ok": False, "provider": "openai", "model": model, "text": "", "status": type(exc).__name__}


def planner_prompt(state: dict, message: str) -> str:
  safe_state = {
    "profile": state.get("profile", {}),
    "slots": effective_slots(state)[:12],
    "ideas": state.get("ideas", []),
    "plans": state.get("plans", []),
  }
  return (
    "You are DatePipe, a discreet local-first surprise date planner. "
    "Suggest concise, concrete plans. Respect secrecy, reveal timing, accomplice mode, "
    "blocked days, frequency, and the couple's history. Return practical bullets.\n\n"
    f"State:\n{json.dumps(safe_state, ensure_ascii=False)}\n\n"
    f"User request:\n{message.strip()}"
  )


def ask_ai(state: dict, message: str) -> tuple[bool, str, str]:
  provider = os.environ.get("DATEPIPE_AI_PROVIDER", "auto").strip().lower()
  if provider in {"auto", "gemini"} and gemini_api_key():
    used_ai, source, answer = ask_gemini(state, message)
    if used_ai or provider == "gemini":
      return used_ai, source, answer
  if provider in {"auto", "openai"}:
    return ask_openai(state, message)
  return False, "local", local_assistant_answer(state, message)


def ask_gemini(state: dict, message: str) -> tuple[bool, str, str]:
  if not gemini_api_key():
    return False, "gemini:missing", local_assistant_answer(state, message)
  text, source = gemini_generate_text(
    planner_prompt(state, message),
    max_output_tokens=900,
    temperature=0.7,
    use_google_search=False,
    timeout=20,
  )
  if text:
    return True, source, text
  return False, source, local_assistant_answer(state, message)


def gemini_generate_text(
  prompt: str,
  max_output_tokens: int,
  temperature: float,
  use_google_search: bool,
  timeout: float,
) -> tuple[str, str]:
  api_key = gemini_api_key()
  if not api_key:
    return "", "gemini:missing"
  model = os.environ.get("DATEPIPE_GEMINI_MODEL", "gemini-2.0-flash").strip()
  payload = {
    "contents": [
      {
        "role": "user",
        "parts": [{"text": prompt}],
      }
    ],
    "generationConfig": {
      "maxOutputTokens": max_output_tokens,
      "temperature": temperature,
    },
  }
  if use_google_search:
    payload["tools"] = [{"google_search": {}}]
  request = Request(
    GEMINI_GENERATE_URL.format(model=model),
    data=json.dumps(payload).encode("utf-8"),
    headers={
      "x-goog-api-key": api_key,
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urlopen(request, timeout=timeout) as response:
      data = json.loads(response.read().decode("utf-8"))
      return extract_gemini_text(data), "gemini"
  except HTTPError as exc:
    return "", f"gemini:fallback:{exc.code}"
  except Exception as exc:
    return "", f"gemini:fallback:{type(exc).__name__}"


def ask_openai(state: dict, message: str) -> tuple[bool, str, str]:
  api_key = os.environ.get("OPENAI_API_KEY", "").strip()
  ok, preflight = preflight_openai_key(api_key)
  if not ok:
    return False, preflight, local_assistant_answer(state, message)
  payload = {
    "model": os.environ.get("DATEPIPE_OPENAI_MODEL", "gpt-4.1-mini"),
    "input": planner_prompt(state, message),
    "max_output_tokens": 900,
  }
  request = Request(
    OPENAI_RESPONSES_URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={
      "Authorization": f"Bearer {api_key}",
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urlopen(request, timeout=20) as response:
      data = json.loads(response.read().decode("utf-8"))
      text = extract_response_text(data)
      return True, "openai", text or local_assistant_answer(state, message)
  except Exception as exc:  # Keep the prototype useful even when the paid path fails.
    return False, f"fallback:{type(exc).__name__}", local_assistant_answer(state, message)


def extract_gemini_text(payload: dict) -> str:
  parts = []
  for candidate in payload.get("candidates", []):
    content = candidate.get("content", {})
    for part in content.get("parts", []):
      if "text" in part:
        parts.append(str(part.get("text", "")))
  return "\n".join(part for part in parts if part).strip()


def extract_response_text(payload: dict) -> str:
  parts = []
  for item in payload.get("output", []):
    for content in item.get("content", []):
      if content.get("type") in {"output_text", "text"}:
        parts.append(str(content.get("text", "")))
  if parts:
    return "\n".join(part for part in parts if part).strip()
  return str(payload.get("output_text", "")).strip()


def local_assistant_answer(state: dict, message: str) -> str:
  options = generate_local_options(state, mood=message)
  lines = ["Local planner fallback:", ""]
  for option in options[:3]:
    lines.append(f"- {option['title']} ({option['category']}, score {option['score']})")
    lines.append(f"  Hint: {option['visibleHint']}")
    lines.append(f"  Why: {option['why']}")
  lines.append("")
  lines.append("I can keep this local-only. Use the Generate button to turn one of these into a plan.")
  return "\n".join(lines)


def apk_manifest() -> dict[str, object]:
  return MobileApkService(DATEPIPE_APK_PATH).manifest()


def apk_build_job() -> dict[str, object]:
  return MobileApkService(DATEPIPE_APK_PATH).build_job()


class DatePipeServer(ThreadingHTTPServer):
  allow_reuse_address = True

  def __init__(self, server_address: tuple[str, int]) -> None:
    if server_address[0] != "127.0.0.1":
      raise ValueError("This local prototype binds only to 127.0.0.1")
    super().__init__(server_address, DatePipeHandler)


class DatePipeHandler(BaseHTTPRequestHandler):
  server_version = "DatePipe/0.1"

  def parse_request(self) -> bool:
    if not super().parse_request():
      return False
    port = self.server.server_address[1]
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if port == 80:
      allowed_hosts.update({"127.0.0.1", "localhost"})
    host = self.headers.get("Host", "").lower()
    origin = self.headers.get("Origin", "")
    allowed_origins = {f"http://{value}" for value in allowed_hosts}
    fetch_site = self.headers.get("Sec-Fetch-Site", "").lower()
    if host not in allowed_hosts or (origin and origin.lower() not in allowed_origins) or fetch_site in {"cross-site", "same-site"}:
      self.send_json({"error": "local_origin_required"}, HTTPStatus.FORBIDDEN)
      return False
    return True

  def end_headers(self) -> None:
    image_sources = "'self' data:"
    if os.environ.get("DATEPIPE_ALLOW_NETWORK") == "1":
      image_sources += " https: http:"
    self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src " + image_sources + "; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
    self.send_header("X-Content-Type-Options", "nosniff")
    self.send_header("Referrer-Policy", "no-referrer")
    super().end_headers()

  def log_message(self, fmt: str, *args: object) -> None:
    sys.stderr.write("[datepipe] " + (fmt % args) + "\n")

  def do_GET(self) -> None:
    parsed = urlparse(self.path)
    if parsed.path in {"/api/download/source.zip", "/api/source.zip"}:
      self.send_error(HTTPStatus.NOT_FOUND)
      return
    context = auth_context_from_headers(self.headers)
    if parsed.path == "/api/health":
      self.send_json({"ok": True, "service": "datepipe", "version": "0.1", "statePath": str(STATE_PATH), "time": now_iso()})
      return
    if parsed.path == "/api/readiness":
      self.send_json(readiness_payload(
        state_path=STATE_PATH,
        city_pack_dir=CITY_PACK_DIR,
        digest_dir=CITY_DIGEST_DIR,
        extraction_dir=EVENT_EXTRACTION_DIR,
        apk_path=DATEPIPE_APK_PATH,
        operational_log_path=OPERATIONAL_LOG_PATH,
      ))
      return
    if parsed.path == "/api/metrics":
      state = load_state()
      container = accounts_container(state)
      summary = operational_sink().summary()
      self.send_json({
        "schema": "datepipe.metrics.v1",
        "time": now_iso(),
        "accounts": len(container.get("accounts", {})),
        "authEvents": len(container.get("authEvents", [])),
        "loginAttempts": len(container.get("loginAttempts", [])),
        "stateSizeBytes": STATE_PATH.stat().st_size if STATE_PATH.exists() else 0,
        "operationalEvents": summary,
      })
      return
    if parsed.path == "/api/account":
      if not context:
        self.send_json({"error": "unauthorized", "requestId": new_id("req")}, HTTPStatus.UNAUTHORIZED)
        return
      status, payload = get_account_summary(context)
      self.send_json(payload, status)
      return
    if parsed.path == "/api/subscription":
      self.send_json(get_subscription_summary(context))
      return
    if parsed.path == "/api/developer/mobile-apk/manifest":
      self.send_json(apk_manifest())
      return
    if parsed.path == "/api/developer/mobile-apk/build-status":
      self.send_json(apk_build_job())
      return
    if parsed.path == "/api/developer/pipeline/status":
      query = parse_qs(parsed.query)
      limit = int(query.get("limit", ["8"])[0] or 8)
      self.send_json(pipeline_status(limit=max(1, min(50, limit))))
      return
    if parsed.path == "/api/developer/mobile-apk/download":
      apk_path = MobileApkService(DATEPIPE_APK_PATH).latest_apk()
      self.send_file_download(apk_path, "application/vnd.android.package-archive", apk_path.name)
      return
    if parsed.path == "/api/reverse-geocode":
      query = parse_qs(parsed.query)
      city = reverse_geocode_city(query.get("lat", [""])[0], query.get("lon", [""])[0])
      self.send_json({"city": city, "ok": bool(city)})
      return
    if parsed.path == "/api/city-digest":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      target_date = query.get("date", [date.today().isoformat()])[0][:10]
      force = query.get("force", ["false"])[0].lower() in {"1", "true", "yes"}
      extract_events = query.get("extractEvents", ["false"])[0].lower() in {"1", "true", "yes"}
      self.send_json(build_city_digest(city, target_date, force=force, extract_events=extract_events))
      return
    if parsed.path == "/api/extracted-events":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      target_date = query.get("date", [date.today().isoformat()])[0][:10]
      force = query.get("force", ["false"])[0].lower() in {"1", "true", "yes"}
      self.send_json(extract_city_events(city, target_date, force=force))
      return
    if parsed.path == "/api/research/events":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      target_date = query.get("date", [""])[0][:10] or None
      limit = int(query.get("limit", ["120"])[0] or 120)
      payload = PipelineStore(PIPELINE_DB_PATH).events_for_city(city, target_date=target_date, limit=max(1, min(500, limit)))
      accepted, rejected = filter_events_for_research_target(payload.get("events", []), target_date)
      payload["events"] = accepted
      payload["eventCount"] = len(accepted)
      payload["filteredRejectedEventCount"] = len(rejected)
      self.send_json(payload)
      return
    if parsed.path == "/api/research-review":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      target_date = query.get("date", [""])[0][:10]
      self.send_json(city_research_review_payload(city, target_date))
      return
    if parsed.path == "/api/quality-report":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      target_date = query.get("date", [date.today().isoformat()])[0][:10]
      scope = query.get("scope", ["research"])[0].strip().lower()
      scoped_state = load_scoped_state(context)
      if scope == "catalog":
        self.send_json(catalog_quality_report(scoped_state, city=city))
        return
      if scope in {"planning", "planning_readiness"}:
        self.send_json(planning_readiness_report(scoped_state))
        return
      if scope == "release":
        self.send_json(release_evaluation_report(scoped_state, cities=[city], weeks=1))
        return
      review = city_research_review_payload(city, target_date)
      self.send_json(review.get("qualityReport", research_quality_report(review, city=city, target_date=target_date)))
      return
    if parsed.path == "/api/developer/release-evaluation":
      query = parse_qs(parsed.query)
      raw_cities = query.get("cities", [query.get("city", ["Vienna"])[0]])[0]
      cities = [item.strip() for item in raw_cities.split(",") if item.strip()] or ["Vienna"]
      weeks = int(query.get("weeks", ["4"])[0] or 4)
      self.send_json(release_evaluation_report(load_scoped_state(context), cities=cities[:12], weeks=weeks))
      return
    if parsed.path == "/api/developer/weekly-event-research/latest":
      self.send_json(load_latest_weekly_event_research_report())
      return
    if parsed.path == "/api/discovery-sources":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      self.send_json({
        "schema": "datepipe.discovery_sources_for_city.v1",
        "city": city,
        "sources": discovery_sources_for_city(city),
      })
      return
    if parsed.path == "/api/city-research":
      query = parse_qs(parsed.query)
      city = query.get("city", [""])[0].strip()
      if not city:
        self.send_json({"error": "city_required"}, HTTPStatus.BAD_REQUEST)
        return
      job = refresh_city_research_job(load_city_research_job(city) or {})
      self.send_json({
        "schema": "datepipe.city_research_status.v1",
        "city": city,
        "sourceStatus": city_source_status(city),
        "job": public_city_research_job(job),
      })
      return
    if parsed.path == "/api/city-digests/manifest":
      query = parse_qs(parsed.query)
      target_date = query.get("date", [date.today().isoformat()])[0][:10]
      self.send_json(load_city_digest_manifest(target_date))
      return
    if parsed.path == "/api/matching-review":
      query = parse_qs(parsed.query)
      city = query.get("city", ["Vienna"])[0]
      target_date = query.get("date", [date.today().isoformat()])[0][:10]
      extract_events = query.get("extractEvents", ["true"])[0].lower() in {"1", "true", "yes"}
      self.send_json(review_matching_contract(build_city_digest(city, target_date, extract_events=extract_events)))
      return
    if parsed.path == "/api/matching-profile":
      state = load_scoped_state(context)
      profile = couple_profile_from_payload({}, state)
      self.send_json(matching_taste_profile(state, profile))
      return
    if parsed.path == "/api/couple-memory":
      state = load_scoped_state(context)
      profile = couple_profile_from_payload({}, state)
      self.send_json(couple_memory_profile(state, profile))
      return
    if parsed.path == "/api/state":
      state = load_scoped_state(context)
      payload = deepcopy(state)
      payload.pop("_accounts", None)
      payload["slots"] = effective_slots(state)
      payload["stats"] = status_counts(state)
      payload["ai"] = {
        "provider": os.environ.get("DATEPIPE_AI_PROVIDER", "auto"),
        "geminiConfigured": bool(gemini_api_key()),
        "openaiConfigured": bool(os.environ.get("OPENAI_API_KEY")),
        "cityResearch": city_research_cost_policy(),
      }
      payload["openai"] = {"configured": bool(os.environ.get("OPENAI_API_KEY"))}
      self.send_json(payload)
      return
    if parsed.path == "/api/download/source.zip":
      self.send_source_zip()
      return
    if parsed.path == "/api/download/android-debug.apk":
      self.send_file_download(DATEPIPE_APK_PATH, "application/vnd.android.package-archive", "datepipe-debug.apk")
      return
    if parsed.path in {"/developer/mobile-update", "/developer/mobile-update/"}:
      self.serve_static("/mobile_update.html")
      return
    self.serve_static(parsed.path)

  def do_HEAD(self) -> None:
    parsed = urlparse(self.path)
    if parsed.path == "/api/download/source.zip":
      payload = build_source_zip()
      self.send_response(HTTPStatus.OK)
      self.send_header("Content-Type", "application/zip")
      self.send_header("Content-Disposition", 'attachment; filename="datepipe-prototype.zip"')
      self.send_header("Content-Length", str(len(payload)))
      self.end_headers()
      return
    if parsed.path in {"/api/download/android-debug.apk", "/api/developer/mobile-apk/download"} and DATEPIPE_APK_PATH.exists():
      apk_path = MobileApkService(DATEPIPE_APK_PATH).latest_apk()
      self.send_response(HTTPStatus.OK)
      self.send_header("Content-Type", "application/vnd.android.package-archive")
      self.send_header("Content-Disposition", f'attachment; filename="{apk_path.name}"')
      self.send_header("Content-Length", str(apk_path.stat().st_size))
      self.end_headers()
      return
    if parsed.path == "/api/health":
      self.send_response(HTTPStatus.OK)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.end_headers()
      return
    self.send_error(HTTPStatus.NOT_FOUND)

  def do_POST(self) -> None:
    parsed = urlparse(self.path)
    body = self.read_body()
    context = auth_context_from_headers(self.headers)
    if parsed.path == "/api/auth/register":
      status, payload = register_account(body)
      self.send_json(payload, status)
      return
    if parsed.path == "/api/auth/login":
      status, payload = login_account(body)
      self.send_json(payload, status)
      return
    if parsed.path == "/api/auth/refresh":
      if not context:
        self.send_json({"error": "unauthorized", "requestId": new_id("req")}, HTTPStatus.UNAUTHORIZED)
        return
      status, payload = refresh_account_session(context, body)
      self.send_json(payload, status)
      return
    if parsed.path == "/api/auth/logout":
      if not context:
        self.send_json({"error": "unauthorized", "requestId": new_id("req")}, HTTPStatus.UNAUTHORIZED)
        return
      status, payload = logout_account(context)
      self.send_json(payload, status)
      return
    if parsed.path == "/api/subscription/activate-dev":
      if not context:
        self.send_json({"error": "unauthorized", "requestId": new_id("req")}, HTTPStatus.UNAUTHORIZED)
        return
      status, payload = activate_dev_subscription(context, body)
      self.send_json(payload, status)
      return
    if parsed.path == "/api/settings":
      state = load_scoped_state(context)
      state["profile"] = {**state.get("profile", {}), **body}
      save_scoped_state(state, context)
      record_operational_event("settings_update", status_code=HTTPStatus.OK, account_id=(context or {}).get("accountId", ""))
      self.send_json(state["profile"])
      return
    if parsed.path == "/api/generate":
      state = load_scoped_state(context)
      if isinstance(body.get("profile"), dict):
        state = deepcopy(state)
        state["profile"] = {**state.get("profile", {}), **body["profile"]}
      city = str(state.get("profile", {}).get("homeBase") or "Vienna").strip() or "Vienna"
      if not city_has_local_source(city):
        target_date = str(body.get("searchDate") or date.today().isoformat())[:10]
        job = start_city_research(city, target_date, state.get("profile", {}))
        self.send_json({
          "options": [],
          "source": f"city-research:{job.get('status') or 'queued'}",
          "cityResearch": public_city_research_job(job),
          "qualityReport": option_quality_report([], city=city, target_date=target_date, source="city-research"),
        }, HTTPStatus.ACCEPTED)
        return
      options, source = generate_options(state, body.get("slotId"), body.get("mood", ""), body.get("searchDate"))
      target_date = str(body.get("searchDate") or date.today().isoformat())[:10]
      self.send_json({
        "options": options,
        "source": source,
        "qualityReport": option_quality_report(options, city=city, target_date=target_date, source=source),
      })
      return
    if parsed.path == "/api/personalized-suggestions":
      state = load_scoped_state(context)
      options, source, digest = generate_backend_personalized_options(body, state)
      city = str(digest.get("city") or body.get("city") or state.get("profile", {}).get("homeBase") or "Vienna")
      target_date = str(digest.get("date") or body.get("date") or body.get("searchDate") or date.today().isoformat())[:10]
      self.send_json({
        "schema": "datepipe.personalized_suggestions.v1",
        "mobileContractVersion": 1,
        "options": options,
        "source": source,
        "qualityReport": option_quality_report(options, city=city, target_date=target_date, source=source, digest=digest),
        "digest": {
          "city": digest.get("city"),
          "date": digest.get("date"),
          "generatedAt": digest.get("generatedAt"),
          "candidateCount": len(digest.get("candidates", [])),
          "extractedEventCount": len(digest.get("extractedEvents", [])),
        },
      })
      return
    if parsed.path == "/api/matching-signal":
      self.send_json(record_matching_signal(body, context=context), HTTPStatus.CREATED)
      return
    if parsed.path == "/api/couple-memory":
      try:
        self.send_json(record_couple_memory(body, context=context), HTTPStatus.CREATED)
      except ValueError as exc:
        self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
      return
    if parsed.path == "/api/city-digests/refresh":
      raw_cities = body.get("cities")
      cities = [str(item).strip() for item in raw_cities if str(item).strip()] if isinstance(raw_cities, list) else None
      target_date = str(body.get("date") or date.today().isoformat())[:10]
      force = bool(body.get("force"))
      extract_events = bool(body.get("extractEvents"))
      payload = refresh_city_digests(cities=cities, target_date=target_date, force=force, extract_events=extract_events)
      record_operational_event("digest_refresh", status_code=HTTPStatus.OK, account_id=(context or {}).get("accountId", ""))
      self.send_json(payload)
      return
    if parsed.path == "/api/city-research":
      city = str(body.get("city") or "").strip()
      if not city:
        self.send_json({"error": "city_required"}, HTTPStatus.BAD_REQUEST)
        return
      target_date = str(body.get("date") or date.today().isoformat())[:10]
      profile = body.get("profile") if isinstance(body.get("profile"), dict) else {}
      job = start_city_research(city, target_date, profile, force=bool(body.get("force")))
      record_operational_event("city_research_start", status_code=HTTPStatus.ACCEPTED, account_id=(context or {}).get("accountId", ""))
      self.send_json(public_city_research_job(job), HTTPStatus.ACCEPTED)
      return
    if parsed.path == "/api/developer/pipeline/run":
      raw_cities = body.get("cities")
      cities = [str(item).strip() for item in raw_cities if str(item).strip()] if isinstance(raw_cities, list) else None
      target_date = str(body.get("date") or date.today().isoformat())[:10]
      payload = refresh_city_digests(
        cities=cities,
        target_date=target_date,
        force=bool(body.get("force")),
        extract_events=bool(body.get("extractEvents")),
      )
      record_operational_event("pipeline_run", status_code=HTTPStatus.OK, account_id=(context or {}).get("accountId", ""))
      self.send_json(payload)
      return
    if parsed.path == "/api/plans":
      state = load_scoped_state(context)
      plan = make_plan_from_proposal(body.get("proposal", body))
      plan["scheduleWarnings"] = schedule_conflicts_for_plan(state, plan)
      state.setdefault("plans", []).insert(0, plan)
      for slot in state.get("slots", []):
        if slot.get("id") == plan.get("slotId"):
          slot["status"] = "candidate"
      save_scoped_state(state, context)
      self.send_json(plan, HTTPStatus.CREATED)
      return
    if parsed.path == "/api/ideas":
      state = load_scoped_state(context)
      idea = {
        "id": new_id("idea"),
        "title": str(body.get("title", "New idea")).strip() or "New idea",
        "category": body.get("category", "standard"),
        "duration": body.get("duration", "normal evening"),
        "timing": body.get("timing", "flexible"),
        "stretch": body.get("stretch", "familiar"),
        "energy": body.get("energy", "medium"),
        "budget": body.get("budget", "casual"),
        "fit": int(body.get("fit", 70)),
        "why": body.get("why", "Manually added idea."),
        "steps": body.get("steps", []),
        "tags": normalize_tags(body.get("tags", [])),
      }
      state.setdefault("ideas", []).insert(0, idea)
      save_scoped_state(state, context)
      self.send_json(idea, HTTPStatus.CREATED)
      return
    if parsed.path == "/api/attachments":
      state = load_scoped_state(context)
      attachment = {
        "id": new_id("att"),
        "planId": body.get("planId"),
        "label": body.get("label", "Attachment"),
        "kind": body.get("kind", "link"),
        "value": body.get("value", ""),
        "previewUrl": body.get("previewUrl", ""),
        "createdAt": now_iso(),
      }
      state.setdefault("attachments", []).insert(0, attachment)
      for plan in state.get("plans", []):
        if plan.get("id") == attachment["planId"]:
          plan.setdefault("attachments", []).append(attachment)
      save_scoped_state(state, context)
      self.send_json(attachment, HTTPStatus.CREATED)
      return
    if parsed.path == "/api/assistant":
      state = load_scoped_state(context)
      used_ai, source, answer = ask_ai(state, str(body.get("message", "")))
      self.send_json({"usedAi": used_ai, "source": source, "answer": answer})
      return
    if parsed.path == "/api/developer/mobile-apk/build":
      self.send_json(apk_build_job())
      return
    self.send_error(HTTPStatus.NOT_FOUND)

  def do_PATCH(self) -> None:
    parsed = urlparse(self.path)
    body = self.read_body()
    context = auth_context_from_headers(self.headers)
    if parsed.path.startswith("/api/plans/"):
      plan_id = unquote(parsed.path.rsplit("/", 1)[-1])
      state = load_scoped_state(context)
      for plan in state.get("plans", []):
        if plan.get("id") == plan_id:
          for key in ("title", "mode", "status", "reveal", "visibleHint", "hiddenNotes", "steps", "backup", "todos"):
            if key in body:
              plan[key] = body[key]
          save_scoped_state(state, context)
          self.send_json(plan)
          return
      self.send_error(HTTPStatus.NOT_FOUND)
      return
    if parsed.path.startswith("/api/research/events/"):
      event_id = unquote(parsed.path.rsplit("/", 1)[-1])
      event = PipelineStore(PIPELINE_DB_PATH).set_event_review_status(
        event_id,
        str(body.get("reviewStatus") or body.get("status") or "needs_review"),
        str(body.get("reviewNote") or body.get("note") or ""),
      )
      if event is None:
        self.send_error(HTTPStatus.NOT_FOUND)
        return
      self.send_json({"schema": "datepipe.research_event_review_update.v1", "event": event})
      return
    if parsed.path.startswith("/api/research/sources/"):
      source_id = unquote(parsed.path.rsplit("/", 1)[-1])
      source = PipelineStore(PIPELINE_DB_PATH).set_source_review_status(
        source_id,
        str(body.get("reviewStatus") or body.get("status") or "needs_review"),
        str(body.get("reviewNote") or body.get("note") or ""),
      )
      if source is None:
        self.send_error(HTTPStatus.NOT_FOUND)
        return
      self.send_json({"schema": "datepipe.research_source_review_update.v1", "source": source})
      return
    if parsed.path.startswith("/api/slots/"):
      slot_id = unquote(parsed.path.rsplit("/", 1)[-1])
      state = load_scoped_state(context)
      for slot in state.get("slots", []):
        if slot.get("id") == slot_id:
          for key in ("title", "status", "notes", "tags"):
            if key in body:
              slot[key] = body[key]
          save_scoped_state(state, context)
          self.send_json(slot)
          return
      self.send_error(HTTPStatus.NOT_FOUND)
      return
    self.send_error(HTTPStatus.NOT_FOUND)

  def read_body(self) -> dict:
    length = int(self.headers.get("Content-Length", "0") or "0")
    if length > MAX_BODY_BYTES:
      self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
      return {}
    if length <= 0:
      return {}
    raw = self.rfile.read(length)
    try:
      payload = json.loads(raw.decode("utf-8"))
      return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
      self.send_error(HTTPStatus.BAD_REQUEST, "Invalid JSON")
      return {}

  def send_json(self, payload: object, status: HTTPStatus = HTTPStatus.OK) -> None:
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Content-Length", str(len(encoded)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    self.wfile.write(encoded)

  def send_source_zip(self) -> None:
    payload = build_source_zip()
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", "application/zip")
    self.send_header("Content-Disposition", 'attachment; filename="datepipe-prototype.zip"')
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)

  def send_file_download(self, path: Path, content_type: str, filename: str) -> None:
    if not path.exists() or not path.is_file():
      self.send_error(HTTPStatus.NOT_FOUND)
      return
    payload = path.read_bytes()
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)

  def serve_static(self, request_path: str) -> None:
    path = "/index.html" if request_path in {"", "/"} else request_path
    path = unquote(path).lstrip("/")
    target = (STATIC_ROOT / path).resolve()
    if not target.is_relative_to(STATIC_ROOT.resolve()) or not target.exists() or not target.is_file():
      self.send_error(HTTPStatus.NOT_FOUND)
      return
    content_type = "text/html; charset=utf-8"
    if target.suffix == ".css":
      content_type = "text/css; charset=utf-8"
    elif target.suffix == ".js":
      content_type = "text/javascript; charset=utf-8"
    payload = target.read_bytes()
    self.send_response(HTTPStatus.OK)
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)


def main() -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--host", default=DEFAULT_HOST)
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--refresh-digests", action="store_true", help="Refresh city digests and exit.")
  parser.add_argument("--digest-city", action="append", default=[], help="City to refresh; repeat for multiple cities. Defaults to all city-pack cities.")
  parser.add_argument("--digest-date", default=date.today().isoformat(), help="Digest date in YYYY-MM-DD format.")
  parser.add_argument("--force-digests", action="store_true", help="Rebuild digests even if cached artifacts exist.")
  parser.add_argument("--extract-events", action="store_true", help="Run Phase B event extraction from discovery-source pages while refreshing digests.")
  parser.add_argument("--run-daily-pipeline", action="store_true", help="Run the local Phase J daily city pipeline and record it in SQLite.")
  args = parser.parse_args()
  if args.host != "127.0.0.1":
    parser.error("This local prototype binds only to 127.0.0.1")
  load_state()
  if args.refresh_digests or args.run_daily_pipeline:
    manifest = refresh_city_digests(cities=args.digest_city or None, target_date=args.digest_date[:10], force=args.force_digests, extract_events=args.extract_events)
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0 if not manifest.get("errors") else 1
  server = DatePipeServer((args.host, args.port))
  print(f"DatePipe running at http://{args.host}:{args.port}/")
  try:
    server.serve_forever()
  except KeyboardInterrupt:
    print("\nDatePipe stopped.")
  finally:
    server.server_close()
  return 0


def build_source_zip() -> bytes:
  files = [
    ROOT / "AGENTS.md",
    ROOT / "README.md",
    ROOT / ".gitignore",
    ROOT / "server.py",
    ROOT / "data" / "seed_dates.json",
    ROOT / "data" / "discovery_sources.json",
    ROOT / "static" / "index.html",
    ROOT / "static" / "styles.css",
    ROOT / "static" / "app.js",
  ]
  buffer = io.BytesIO()
  with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in files:
      if path.exists():
        archive.write(path, arcname=str(Path("datepipe") / path.relative_to(ROOT)))
  return buffer.getvalue()


if __name__ == "__main__":
  raise SystemExit(main())
