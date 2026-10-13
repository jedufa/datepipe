#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

import server


DEFAULT_CITIES = ["Vienna", "Graz", "Linz", "Salzburg", "Innsbruck"]


def week_starts(anchor: date, count: int) -> list[date]:
  first = anchor - timedelta(days=anchor.weekday())
  return [first + timedelta(days=index * 7) for index in range(max(1, count))]


def source_summary(cities: list[str]) -> dict:
  city_rows = []
  family_counts: Counter[str] = Counter()
  for city in cities:
    sources = server.discovery_sources_for_city(city, limit=48)
    source_ids = [str(item.get("id") or "") for item in sources]
    buckets = Counter(str(item.get("sourceQualityBucket") or "other") for item in sources)
    kinds = Counter(str(item.get("sourceKind") or "unknown") for item in sources)
    city_rows.append({
      "city": city,
      "sourceCount": len(sources),
      "has1000things": any("1000things" in source_id for source_id in source_ids),
      "buckets": buckets.most_common(),
      "kinds": kinds.most_common(8),
    })
    family_counts.update(buckets)
  return {
    "citySources": city_rows,
    "sourceFamilyCounts": family_counts.most_common(),
  }


def extraction_flags(extraction: dict, source_status: dict) -> list[str]:
  flags = []
  gate = str(extraction.get("qualityGateStatus") or "")
  if gate and gate != "passed":
    flags.append(gate)
  if str(source_status.get("sourceReadinessStatus") or "") != "ready":
    flags.append(str(source_status.get("sourceReadinessStatus") or "source_not_ready"))
  if int(extraction.get("eventCount") or 0) < 3:
    flags.append("thin_event_leads")
  if int(extraction.get("fetchedSourceCount") or 0) == 0:
    flags.append("no_sources_fetched")
  if extraction.get("errors"):
    flags.append("source_fetch_errors")
  quality = extraction.get("qualitySummary") if isinstance(extraction.get("qualitySummary"), dict) else {}
  for key in ("selectedSourceMix", "acceptedSourceMix"):
    mix = quality.get(key) if isinstance(quality.get(key), dict) else {}
    if mix.get("sourceMixStatus") == "generic_heavy":
      flags.append(f"{key}_generic_heavy")
  return sorted(set(flag for flag in flags if flag))


def build_report(
  cities: list[str],
  weeks: int,
  *,
  force: bool,
  anchor: date,
  live_weeks: int = 1,
  max_sources: int = 8,
  max_events: int = 80,
  expand_pages: int = 1,
) -> dict:
  starts = week_starts(anchor, weeks)
  rows = []
  failed_sources = []
  started_at = server.now_iso()
  for city in cities:
    source_status = server.city_source_status(city)
    for index, start in enumerate(starts):
      end = start + timedelta(days=6)
      target = server.release_evaluation_target_date(anchor, start, end).isoformat()
      should_extract = index < max(1, live_weeks)
      digest = server.build_city_digest(
        city,
        target,
        force=force if should_extract else False,
        extract_events=should_extract,
        max_sources=max_sources,
        max_events=max_events,
        expand_pages=expand_pages,
      )
      extraction_path = server.event_extraction_path(city, target)
      extraction = server.load_json(extraction_path) if should_extract and extraction_path.exists() else {}
      flags = extraction_flags(extraction, source_status) if should_extract else []
      for error in extraction.get("errors", []) if isinstance(extraction.get("errors"), list) else []:
        failed_sources.append({
          "city": city,
          "targetDate": target,
          "source": error.get("source", ""),
          "error": error.get("error", ""),
          "message": error.get("message", ""),
        })
      rows.append({
        "city": city,
        "weekStart": start.isoformat(),
        "weekEnd": end.isoformat(),
        "targetDate": target,
        "sourceCount": int(digest.get("discoverySourceCount") or len(digest.get("discoverySources", []))),
        "fetchedSourceCount": int(extraction.get("fetchedSourceCount") or 0),
        "acceptedEventCount": int(extraction.get("eventCount") or 0),
        "newlyExtractedEventCount": int(extraction.get("newlyExtractedEventCount") or 0),
        "rejectedEventCount": int(extraction.get("rejectedEventCount") or 0),
        "qualityGateStatus": extraction.get("qualityGateStatus") or ("not_scheduled" if not should_extract else "not_run"),
        "sourceReadinessStatus": source_status.get("sourceReadinessStatus") or "unknown",
        "flags": flags,
        "needsCodexReview": bool(flags),
      })
  weak_cities = sorted({row["city"] for row in rows if row["needsCodexReview"]})
  report = {
    "schema": "datepipe.weekly_event_research_report.v1",
    "available": True,
    "status": "available",
    "generatedAt": server.now_iso(),
    "startedAt": started_at,
    "anchorDate": anchor.isoformat(),
    "weekCount": len(starts),
    "liveWeekCount": max(1, live_weeks),
    "sourceBudget": max_sources,
    "eventBudget": max_events,
    "expandPages": expand_pages,
    "cityCount": len(cities),
    "cities": cities,
    "sourceSummary": source_summary(cities),
    "rows": rows,
    "weakCities": weak_cities,
    "failedSources": failed_sources,
    "recommendedMaintenance": maintenance_recommendations(rows, failed_sources),
    "releaseEvaluation": server.release_evaluation_report(server.load_state(), cities=cities, weeks=len(starts)),
  }
  return report


def maintenance_recommendations(rows: list[dict], failed_sources: list[dict]) -> list[dict]:
  recommendations = []
  by_city: dict[str, list[dict]] = {}
  for row in rows:
    by_city.setdefault(str(row.get("city") or ""), []).append(row)
  for city, city_rows in sorted(by_city.items()):
    flags = sorted({flag for row in city_rows for flag in row.get("flags", [])})
    if not flags:
      continue
    recommendations.append({
      "city": city,
      "action": "review_source_mix",
      "flags": flags,
      "note": "Inspect city source balance and patch data/discovery_sources.json only if sources are stale, broken, or too generic.",
    })
  if failed_sources:
    recommendations.append({
      "city": "",
      "action": "check_failed_sources",
      "flags": ["source_fetch_errors"],
      "note": "Review sources with repeated fetch failures before removing them.",
    })
  return recommendations


def markdown_report(report: dict) -> str:
  rows = report.get("rows", [])
  lines = [
    "# DatePipe Weekly Event Research",
    "",
    f"- Generated: {report.get('generatedAt', '')}",
    f"- Cities: {', '.join(report.get('cities', []))}",
    f"- Weeks: {report.get('weekCount', 0)}",
    f"- Weak cities: {', '.join(report.get('weakCities', [])) or 'none'}",
    "",
    "## City Weeks",
    "",
    "| City | Week | Target | Events | Sources | Gate | Flags |",
    "| --- | --- | --- | ---: | ---: | --- | --- |",
  ]
  for row in rows:
    lines.append(
      "| {city} | {weekStart} to {weekEnd} | {targetDate} | {events} | {sources} | {gate} | {flags} |".format(
        city=row.get("city", ""),
        weekStart=row.get("weekStart", ""),
        weekEnd=row.get("weekEnd", ""),
        targetDate=row.get("targetDate", ""),
        events=row.get("acceptedEventCount", 0),
        sources=row.get("fetchedSourceCount", 0),
        gate=row.get("qualityGateStatus", ""),
        flags=", ".join(row.get("flags", [])) or "-",
      )
    )
  lines.extend(["", "## Recommended Maintenance", ""])
  recommendations = report.get("recommendedMaintenance", [])
  if recommendations:
    for item in recommendations:
      city = item.get("city") or "all cities"
      lines.append(f"- {city}: {item.get('action', '')} ({', '.join(item.get('flags', []))})")
  else:
    lines.append("- No Codex maintenance needed.")
  return "\n".join(lines) + "\n"


def write_reports(report: dict, output_dir: Path) -> tuple[Path, Path, Path]:
  output_dir.mkdir(parents=True, exist_ok=True)
  timestamp = str(report.get("generatedAt") or server.now_iso()).replace(":", "").replace("-", "").replace("T", "-").replace("Z", "")
  json_path = output_dir / f"weekly-event-research-{timestamp}.json"
  markdown_path = output_dir / f"weekly-event-research-{timestamp}.md"
  latest_path = server.WEEKLY_REPORT_LATEST_PATH
  server.write_json(json_path, report)
  markdown_path.write_text(markdown_report(report), encoding="utf-8")
  server.write_json(latest_path, {**report, "path": server.display_runtime_path(json_path), "markdownPath": server.display_runtime_path(markdown_path)})
  return json_path, markdown_path, latest_path


def parse_args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Refresh DatePipe city event research and write an operator report.")
  parser.add_argument("--city", action="append", default=[], help="City to refresh. Repeat for multiple cities.")
  parser.add_argument("--cities", nargs="+", default=[], help="Cities to refresh.")
  parser.add_argument("--weeks", type=int, default=4, help="Number of calendar weeks to report.")
  parser.add_argument("--live-weeks", type=int, default=1, help="Number of leading weeks to refresh with live extraction.")
  parser.add_argument("--max-sources", type=int, default=8, help="Maximum sources fetched per city live week.")
  parser.add_argument("--max-events", type=int, default=80, help="Maximum events retained per city live week.")
  parser.add_argument("--expand-pages", type=int, default=1, help="Child pages fetched per selected source.")
  parser.add_argument("--anchor-date", default=date.today().isoformat(), help="Date used to choose the first calendar week.")
  parser.add_argument("--force", action="store_true", help="Force live digest/event extraction even if cached files exist.")
  parser.add_argument("--report", action="store_true", help="Kept for readable automation prompts; reports are always written.")
  parser.add_argument("--output-dir", default=str(server.WEEKLY_REPORT_DIR), help="Directory for timestamped reports.")
  return parser.parse_args()


def main() -> int:
  args = parse_args()
  cities = [item.strip() for item in [*args.cities, *args.city] if item.strip()] or DEFAULT_CITIES
  try:
    anchor = date.fromisoformat(str(args.anchor_date)[:10])
  except ValueError:
    print(f"Invalid --anchor-date: {args.anchor_date}", file=sys.stderr)
    return 2
  report = build_report(
    cities,
    args.weeks,
    force=args.force,
    anchor=anchor,
    live_weeks=args.live_weeks,
    max_sources=args.max_sources,
    max_events=args.max_events,
    expand_pages=args.expand_pages,
  )
  json_path, markdown_path, latest_path = write_reports(report, Path(args.output_dir))
  print(f"[ok] DatePipe weekly event research report written: {server.display_runtime_path(json_path)}")
  print(f"[ok] Markdown summary written: {server.display_runtime_path(markdown_path)}")
  print(f"[ok] Latest report pointer written: {server.display_runtime_path(latest_path)}")
  print(f"[info] Weak cities: {', '.join(report.get('weakCities', [])) or 'none'}")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
