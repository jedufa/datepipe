from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse


def now_iso() -> str:
  return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_url_key(value: object) -> str:
  raw = str(value or "").strip()
  if not raw:
    return ""
  parsed = urlparse(raw)
  host = parsed.netloc.lower().removeprefix("www.")
  path = "/".join(part for part in parsed.path.rstrip("/").lower().split("/") if part)
  return f"{host}/{path}" if host and path else host or raw.lower()


def source_cadence_days(source: dict) -> int:
  kind = str(source.get("sourceKind") or source.get("sourceType") or "").lower()
  trust = str(source.get("trustLevel") or "").lower()
  cadence = str(source.get("typicalCadence") or "").lower()
  text = f"{kind} {cadence}"
  if "daily" in cadence or "official-city-calendar" in text or "event-calendar" in text:
    return 1
  if "ticket-platform" in text or "event-platform" in text:
    return 2
  if "venue" in text or "weekly" in cadence:
    return 4
  if "editorial" in text or "guide" in text:
    return 14 if trust != "low" else 30
  return 7


@dataclass(frozen=True)
class PipelineStore:
  db_path: Path

  def connect(self) -> sqlite3.Connection:
    self.db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(self.db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS pipeline_runs (
        id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        target_date TEXT NOT NULL,
        cities_json TEXT NOT NULL,
        force INTEGER NOT NULL,
        extract_events INTEGER NOT NULL,
        status TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        city_count INTEGER NOT NULL,
        error_count INTEGER NOT NULL,
        manifest_json TEXT NOT NULL
      )
      """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_created_at ON pipeline_runs(created_at DESC)")
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS research_sources (
        id TEXT PRIMARY KEY,
        city_key TEXT NOT NULL,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        url_key TEXT NOT NULL,
        source_kind TEXT NOT NULL,
        trust_level TEXT NOT NULL,
        cadence_days INTEGER NOT NULL,
        last_checked_at TEXT,
        last_success_at TEXT,
        last_event_count INTEGER NOT NULL DEFAULT 0,
        failure_count INTEGER NOT NULL DEFAULT 0,
        next_due_at TEXT NOT NULL,
        review_status TEXT NOT NULL DEFAULT 'new',
        review_note TEXT NOT NULL DEFAULT '',
        reviewed_at TEXT,
        source_json TEXT NOT NULL
      )
      """
    )
    for statement in (
      "ALTER TABLE research_sources ADD COLUMN review_status TEXT NOT NULL DEFAULT 'new'",
      "ALTER TABLE research_sources ADD COLUMN review_note TEXT NOT NULL DEFAULT ''",
      "ALTER TABLE research_sources ADD COLUMN reviewed_at TEXT",
    ):
      try:
        connection.execute(statement)
      except sqlite3.OperationalError:
        pass
    connection.execute("CREATE INDEX IF NOT EXISTS idx_research_sources_due ON research_sources(city_key, next_due_at)")
    connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_research_sources_url_city ON research_sources(city_key, url_key)")
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS source_fetches (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        city_key TEXT NOT NULL,
        target_date TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        status TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        event_count INTEGER NOT NULL,
        error_type TEXT NOT NULL,
        error_message TEXT NOT NULL
      )
      """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_source_fetches_source ON source_fetches(source_id, fetched_at DESC)")
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY,
        canonical_url TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        city TEXT NOT NULL,
        category TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        last_verified_at TEXT,
        source_id TEXT NOT NULL,
        source_name TEXT NOT NULL,
        status TEXT NOT NULL,
        review_status TEXT NOT NULL,
        event_json TEXT NOT NULL
      )
      """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_last_seen ON events(last_seen_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_city_status ON events(city, status, review_status)")
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS event_occurrences (
        event_id TEXT NOT NULL,
        city TEXT NOT NULL,
        target_date TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        PRIMARY KEY (event_id, city, target_date)
      )
      """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_event_occurrences_date ON event_occurrences(city, target_date)")
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS event_images (
        event_id TEXT PRIMARY KEY,
        image_url TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
      )
      """
    )
    connection.execute(
      """
      CREATE TABLE IF NOT EXISTS research_jobs (
        id TEXT PRIMARY KEY,
        city TEXT NOT NULL,
        target_date TEXT NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        quality_gate_status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        deadline_at TEXT NOT NULL,
        fallback_reason TEXT NOT NULL,
        message TEXT NOT NULL,
        job_json TEXT NOT NULL
      )
      """
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_research_jobs_city ON research_jobs(city, updated_at DESC)")
    return connection

  def register_sources(self, city: str, sources: list[dict]) -> None:
    city_key = str(city or "").strip().lower()
    generated_at = now_iso()
    with self.connect() as connection:
      for index, source in enumerate(sources):
        url = str(source.get("url") or "").strip()
        url_key = normalize_url_key(url)
        if not url_key:
          continue
        source_id = str(source.get("id") or f"{city_key}.{url_key}").strip()
        cadence_days = source_cadence_days(source)
        row = connection.execute(
          """
          SELECT id, last_checked_at, last_success_at, last_event_count, failure_count, next_due_at,
                 review_status, review_note, reviewed_at
          FROM research_sources
          WHERE id = ? OR (city_key = ? AND url_key = ?)
          ORDER BY id = ? DESC
          LIMIT 1
          """,
          (source_id, city_key, url_key, source_id),
        ).fetchone()
        canonical_id = row["id"] if row else source_id
        stored_source = dict(source)
        stored_source["id"] = canonical_id
        next_due_at = row["next_due_at"] if row else (generated_at if index < 12 else (datetime.now(UTC) + timedelta(days=index // 12)).isoformat(timespec="seconds").replace("+00:00", "Z"))
        connection.execute(
          """
          INSERT INTO research_sources (
            id, city_key, name, url, url_key, source_kind, trust_level, cadence_days,
            last_checked_at, last_success_at, last_event_count, failure_count, next_due_at,
            review_status, review_note, reviewed_at, source_json
          )
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(id) DO UPDATE SET
            city_key=excluded.city_key,
            name=excluded.name,
            url=excluded.url,
            url_key=excluded.url_key,
            source_kind=excluded.source_kind,
            trust_level=excluded.trust_level,
            cadence_days=excluded.cadence_days,
            source_json=excluded.source_json
          """,
          (
            canonical_id,
            city_key,
            str(source.get("name") or ""),
            url,
            url_key,
            str(source.get("sourceKind") or source.get("sourceType") or ""),
            str(source.get("trustLevel") or ""),
            cadence_days,
            row["last_checked_at"] if row else None,
            row["last_success_at"] if row else None,
            int(row["last_event_count"]) if row else 0,
            int(row["failure_count"]) if row else 0,
            next_due_at,
            row["review_status"] if row else str(source.get("reviewStatus") or "new"),
            row["review_note"] if row else "",
            row["reviewed_at"] if row else None,
            json.dumps(stored_source, ensure_ascii=False),
          ),
        )

  def record_research_job(self, job: dict) -> None:
    job_id = str(job.get("id") or "").strip()
    if not job_id:
      return
    updated_at = now_iso()
    with self.connect() as connection:
      connection.execute(
        """
        INSERT INTO research_jobs (
          id, city, target_date, provider, status, quality_gate_status, started_at,
          updated_at, deadline_at, fallback_reason, message, job_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          city=excluded.city,
          target_date=excluded.target_date,
          provider=excluded.provider,
          status=excluded.status,
          quality_gate_status=excluded.quality_gate_status,
          updated_at=excluded.updated_at,
          deadline_at=excluded.deadline_at,
          fallback_reason=excluded.fallback_reason,
          message=excluded.message,
          job_json=excluded.job_json
        """,
        (
          job_id,
          str(job.get("city") or ""),
          str(job.get("targetDate") or ""),
          str(job.get("provider") or ""),
          str(job.get("status") or ""),
          str(job.get("qualityGateStatus") or ""),
          str(job.get("startedAt") or updated_at),
          updated_at,
          str(job.get("deadlineAt") or ""),
          str(job.get("deepResearchFallbackReason") or job.get("errorType") or ""),
          str(job.get("message") or "")[:500],
          json.dumps(job, ensure_ascii=False),
        ),
      )

  def due_sources(self, city: str, sources: list[dict], *, limit: int, force: bool = False) -> list[dict]:
    self.register_sources(city, sources)
    city_key = str(city or "").strip().lower()
    now = now_iso()
    with self.connect() as connection:
      if force:
        rows = connection.execute(
          """
          SELECT source_json FROM research_sources
          WHERE city_key = ?
          ORDER BY last_checked_at IS NOT NULL, last_checked_at ASC, next_due_at ASC
          LIMIT ?
          """,
          (city_key, limit),
        ).fetchall()
      else:
        rows = connection.execute(
          """
          SELECT source_json FROM research_sources
          WHERE city_key = ? AND next_due_at <= ?
          ORDER BY
            failure_count ASC,
            last_event_count DESC,
            last_checked_at IS NOT NULL,
            last_checked_at ASC,
            next_due_at ASC
          LIMIT ?
          """,
          (city_key, now, limit),
        ).fetchall()
    return [json.loads(row["source_json"]) for row in rows]

  def record_source_fetch(
    self,
    source: dict,
    *,
    city: str,
    target_date: str,
    status: str,
    duration_ms: int,
    event_count: int,
    error_type: str = "",
    error_message: str = "",
  ) -> None:
    source_id = str(source.get("id") or "").strip()
    if not source_id:
      return
    checked_at = now_iso()
    cadence_days = source_cadence_days(source)
    if status == "succeeded":
      due_at = (datetime.now(UTC) + timedelta(days=cadence_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
      failure_increment = 0
    else:
      backoff_days = min(30, max(1, cadence_days) * 2)
      due_at = (datetime.now(UTC) + timedelta(days=backoff_days)).isoformat(timespec="seconds").replace("+00:00", "Z")
      failure_increment = 1
    with self.connect() as connection:
      connection.execute(
        """
        INSERT INTO source_fetches (
          id, source_id, city_key, target_date, fetched_at, status, duration_ms,
          event_count, error_type, error_message
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          f"fetch_{uuid.uuid4().hex[:12]}",
          source_id,
          str(city or "").strip().lower(),
          target_date,
          checked_at,
          status,
          duration_ms,
          event_count,
          error_type[:80],
          error_message[:240],
        ),
      )
      connection.execute(
        """
        UPDATE research_sources
        SET last_checked_at = ?,
            last_success_at = CASE WHEN ? = 'succeeded' THEN ? ELSE last_success_at END,
            last_event_count = ?,
            failure_count = CASE WHEN ? = 'succeeded' THEN 0 ELSE failure_count + ? END,
            next_due_at = ?
        WHERE id = ?
        """,
        (checked_at, status, checked_at, event_count, status, failure_increment, due_at, source_id),
      )

  def upsert_events(self, events: list[dict], *, city: str, target_date: str) -> dict:
    seen_at = now_iso()
    inserted = 0
    updated = 0
    with self.connect() as connection:
      for event in events:
        url_key = normalize_url_key(event.get("eventUrl"))
        if not url_key:
          continue
        existing = connection.execute("SELECT id, review_status FROM events WHERE canonical_url = ?", (url_key,)).fetchone()
        event_id = f"event_{hashlib.sha1(url_key.encode('utf-8')).hexdigest()[:16]}"
        review_status = existing["review_status"] if existing else str(event.get("reviewStatus") or "new")
        source_id = str(event.get("sourceId") or "")
        if existing:
          event_id = existing["id"]
          updated += 1
        else:
          inserted += 1
        connection.execute(
          """
          INSERT INTO events (
            id, canonical_url, title, city, category, first_seen_at, last_seen_at,
            last_verified_at, source_id, source_name, status, review_status, event_json
          )
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(canonical_url) DO UPDATE SET
            title=excluded.title,
            city=excluded.city,
            category=excluded.category,
            last_seen_at=excluded.last_seen_at,
            source_id=excluded.source_id,
            source_name=excluded.source_name,
            status='active',
            event_json=excluded.event_json
          """,
          (
            event_id,
            url_key,
            str(event.get("title") or "")[:180],
            city,
            str(event.get("category") or ""),
            seen_at,
            seen_at,
            None,
            source_id,
            str(event.get("sourceName") or "")[:160],
            "active",
            review_status,
            json.dumps(event, ensure_ascii=False),
          ),
        )
        connection.execute(
          """
          INSERT INTO event_occurrences (event_id, city, target_date, first_seen_at, last_seen_at)
          VALUES (?, ?, ?, ?, ?)
          ON CONFLICT(event_id, city, target_date) DO UPDATE SET last_seen_at=excluded.last_seen_at
          """,
          (event_id, city, target_date, seen_at, seen_at),
        )
        image_url = str(event.get("imageUrl") or "").strip()
        if image_url:
          connection.execute(
            """
            INSERT INTO event_images (event_id, image_url, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET image_url=excluded.image_url, last_seen_at=excluded.last_seen_at
            """,
            (event_id, image_url, seen_at, seen_at),
          )
    return {"inserted": inserted, "updated": updated, "total": inserted + updated}

  def events_for_city(
    self,
    city: str,
    *,
    target_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 120,
  ) -> dict:
    params: list[object] = [city]
    date_join = ""
    date_where = ""
    if target_date:
      date_join = "JOIN event_occurrences o ON o.event_id = e.id"
      date_where = "AND o.target_date = ?"
      params.append(target_date)
    elif date_from and date_to:
      date_join = "JOIN event_occurrences o ON o.event_id = e.id"
      date_where = "AND o.target_date BETWEEN ? AND ?"
      params.extend([date_from, date_to])
    params.append(limit)
    with self.connect() as connection:
      rows = connection.execute(
        f"""
        SELECT e.id, e.event_json, e.first_seen_at, e.last_seen_at, e.review_status, e.status
        FROM events e
        {date_join}
        WHERE e.city = ? AND e.status = 'active' {date_where}
        GROUP BY e.id
        ORDER BY e.last_seen_at DESC
        LIMIT ?
        """,
        tuple(params),
      ).fetchall()
    events = []
    for row in rows:
      item = json.loads(row["event_json"])
      item["researchState"] = {
        "id": row["id"],
        "firstSeenAt": row["first_seen_at"],
        "lastSeenAt": row["last_seen_at"],
        "reviewStatus": row["review_status"],
        "status": row["status"],
      }
      events.append(item)
    return {
      "schema": "datepipe.research_events.v1",
      "generatedAt": now_iso(),
      "city": city,
      "date": target_date or "",
      "dateFrom": date_from or "",
      "dateTo": date_to or "",
      "eventCount": len(events),
      "events": events,
    }

  def review_queue(
    self,
    city: str,
    *,
    target_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 80,
  ) -> dict:
    city_key = str(city or "").strip().lower()
    with self.connect() as connection:
      source_rows = connection.execute(
        """
        SELECT id, name, url, source_kind, trust_level, last_checked_at, last_event_count,
               failure_count, next_due_at, review_status, review_note, reviewed_at, source_json
        FROM research_sources
        WHERE city_key = ?
        ORDER BY
          CASE review_status WHEN 'new' THEN 0 WHEN 'needs_review' THEN 1 WHEN 'approved' THEN 2 ELSE 3 END,
          last_event_count DESC,
          next_due_at ASC
        LIMIT ?
        """,
        (city_key, limit),
      ).fetchall()
      fetch_rows = connection.execute(
        """
        SELECT source_id, fetched_at, status, event_count, error_type, error_message
        FROM source_fetches
        WHERE city_key = ?
        ORDER BY fetched_at DESC
        LIMIT ?
        """,
        (city_key, min(limit, 40)),
      ).fetchall()
      job_rows = connection.execute(
        """
        SELECT id, city, target_date, provider, status, quality_gate_status, started_at,
               updated_at, deadline_at, fallback_reason, message
        FROM research_jobs
        WHERE lower(city) = ?
        ORDER BY updated_at DESC
        LIMIT 12
        """,
        (city_key,),
      ).fetchall()
    events = self.events_for_city(city, target_date=target_date, date_from=date_from, date_to=date_to, limit=limit).get("events", [])
    return {
      "schema": "datepipe.research_review.v1",
      "generatedAt": now_iso(),
      "city": city,
      "date": target_date or "",
      "dateFrom": date_from or "",
      "dateTo": date_to or "",
      "sources": [self._source_review_row(row) for row in source_rows],
      "events": events,
      "fetches": [dict(row) for row in fetch_rows],
      "jobs": [dict(row) for row in job_rows],
    }

  def _source_review_row(self, row: sqlite3.Row) -> dict:
    source = json.loads(row["source_json"])
    return {
      "id": row["id"],
      "name": row["name"],
      "url": row["url"],
      "sourceKind": row["source_kind"],
      "trustLevel": row["trust_level"],
      "lastCheckedAt": row["last_checked_at"] or "",
      "lastEventCount": row["last_event_count"],
      "failureCount": row["failure_count"],
      "nextDueAt": row["next_due_at"],
      "reviewStatus": row["review_status"],
      "reviewNote": row["review_note"],
      "reviewedAt": row["reviewed_at"] or "",
      "sourceQualityBucket": source.get("sourceQualityBucket", ""),
      "researchProvenance": source.get("researchProvenance", ""),
    }

  def set_event_review_status(self, event_id: str, status: str, note: str = "") -> dict | None:
    allowed = {"new", "approved", "rejected", "needs_review"}
    clean_status = status if status in allowed else "needs_review"
    with self.connect() as connection:
      row = connection.execute("SELECT id, event_json FROM events WHERE id = ?", (event_id,)).fetchone()
      if not row:
        return None
      event = json.loads(row["event_json"])
      event["reviewStatus"] = clean_status
      if note:
        event["reviewNote"] = note[:240]
      connection.execute(
        """
        UPDATE events
        SET review_status = ?, status = CASE WHEN ? = 'rejected' THEN 'rejected' ELSE 'active' END,
            last_verified_at = ?, event_json = ?
        WHERE id = ?
        """,
        (clean_status, clean_status, now_iso(), json.dumps(event, ensure_ascii=False), event_id),
      )
      return event

  def set_source_review_status(self, source_id: str, status: str, note: str = "") -> dict | None:
    allowed = {"new", "approved", "rejected", "needs_review"}
    clean_status = status if status in allowed else "needs_review"
    reviewed_at = now_iso()
    with self.connect() as connection:
      row = connection.execute("SELECT id, source_json FROM research_sources WHERE id = ?", (source_id,)).fetchone()
      if not row:
        return None
      source = json.loads(row["source_json"])
      source["reviewStatus"] = clean_status
      if note:
        source["reviewNote"] = note[:240]
      connection.execute(
        """
        UPDATE research_sources
        SET review_status = ?, review_note = ?, reviewed_at = ?, source_json = ?
        WHERE id = ?
        """,
        (clean_status, note[:240], reviewed_at, json.dumps(source, ensure_ascii=False), source_id),
      )
      return source

  def research_summary(self) -> dict:
    with self.connect() as connection:
      source_count = connection.execute("SELECT COUNT(*) FROM research_sources").fetchone()[0]
      due_count = connection.execute("SELECT COUNT(*) FROM research_sources WHERE next_due_at <= ?", (now_iso(),)).fetchone()[0]
      event_count = connection.execute("SELECT COUNT(*) FROM events WHERE status = 'active'").fetchone()[0]
      new_count = connection.execute("SELECT COUNT(*) FROM events WHERE status = 'active' AND review_status = 'new'").fetchone()[0]
      last_fetch = connection.execute("SELECT fetched_at, status, event_count FROM source_fetches ORDER BY fetched_at DESC LIMIT 1").fetchone()
    return {
      "sourceCount": source_count,
      "dueSourceCount": due_count,
      "eventCount": event_count,
      "newEventCount": new_count,
      "lastFetch": dict(last_fetch) if last_fetch else None,
    }

  def record_manifest(
    self,
    manifest: dict,
    *,
    cities: list[str],
    force: bool,
    extract_events: bool,
    started_at: float | None = None,
  ) -> dict:
    duration_ms = int((time.time() - started_at) * 1000) if started_at else int(manifest.get("durationMs") or 0)
    error_count = int(manifest.get("errorCount") or len(manifest.get("errors", []) if isinstance(manifest.get("errors"), list) else []))
    row = {
      "id": f"pipe_{uuid.uuid4().hex[:12]}",
      "createdAt": now_iso(),
      "targetDate": str(manifest.get("date") or ""),
      "cities": cities,
      "force": force,
      "extractEvents": extract_events,
      "status": "failed" if error_count else "succeeded",
      "durationMs": duration_ms,
      "cityCount": int(manifest.get("cityCount") or len(manifest.get("digests", []) if isinstance(manifest.get("digests"), list) else [])),
      "errorCount": error_count,
      "manifest": manifest,
    }
    with self.connect() as connection:
      connection.execute(
        """
        INSERT INTO pipeline_runs (
          id, created_at, target_date, cities_json, force, extract_events, status,
          duration_ms, city_count, error_count, manifest_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
          row["id"],
          row["createdAt"],
          row["targetDate"],
          json.dumps(cities, ensure_ascii=False),
          1 if force else 0,
          1 if extract_events else 0,
          row["status"],
          row["durationMs"],
          row["cityCount"],
          row["errorCount"],
          json.dumps(manifest, ensure_ascii=False),
        ),
      )
    return {key: value for key, value in row.items() if key != "manifest"}

  def status(self, limit: int = 8) -> dict:
    with self.connect() as connection:
      rows = connection.execute(
        """
        SELECT id, created_at, target_date, cities_json, force, extract_events, status,
               duration_ms, city_count, error_count
        FROM pipeline_runs
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
      ).fetchall()
    runs = []
    for row in rows:
      runs.append({
        "id": row["id"],
        "createdAt": row["created_at"],
        "targetDate": row["target_date"],
        "cities": json.loads(row["cities_json"]),
        "force": bool(row["force"]),
        "extractEvents": bool(row["extract_events"]),
        "status": row["status"],
        "durationMs": row["duration_ms"],
        "cityCount": row["city_count"],
        "errorCount": row["error_count"],
      })
    return {
      "schema": "datepipe.pipeline_status.v1",
      "generatedAt": now_iso(),
      "runCount": len(runs),
      "runs": runs,
      "research": self.research_summary(),
    }
