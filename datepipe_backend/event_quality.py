from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from urllib.parse import parse_qs, urlparse

from datepipe_backend.source_quality import source_quality_bucket


EXACT_BAD_TITLES = {
  "share",
  "events",
  "events in the",
  "event",
  "calendar",
  "programme",
  "program",
  "guided tours",
  "tours & groups",
  "kids & families",
  "school groups & more",
  "opening hours, tickets & prices",
}

BAD_TITLE_PATTERNS = (
  r"\bopening hours\b",
  r"\btickets?\s*&\s*prices?\b",
  r"\bprivacy\b",
  r"\bdatenschutz\b",
  r"\bnewsletter\b",
  r"\bcontact\b",
  r"\bkontakt\b",
  r"\bpress\b",
  r"\bjobs?\b",
  r"\bschool groups?\b",
  r"\bkids?\b",
  r"\bkids?\s*&\s*famil",
  r"\bfamilies\b",
  r"\bfamily\b",
  r"\bfamilie\b",
  r"\bchildren\b",
  r"\bfuer kinder\b",
  r"\bfür kinder\b",
  r"\bstay for\b",
  r"\bbook now\b",
  r"\bhotel\b",
  r"^today:\s+\d{1,2}:\d{2}",
  r"^news\b",
  r"\bcalendar\b",
  r"\bstartseite\b",
  r"\bhomepage\b",
  r"\bgewinn(?:e|spiel)?\b",
  r"\bgiveaway\b",
  r"\bvip packages?\b",
  r"\bsponsor(?:ing)?\b",
  r"\bnachruf\b",
  r"\bobituary\b",
  r"\bpress release\b",
  r"\bpressemeldung\b",
  r"\bdigitalisate online\b",
  r"\bübernimmt interimistisch\b",
  r"\buebernimmt interimistisch\b",
  r"\bworkshop office\b",
  r"\bworkshop centre\b",
  r"\bopen doors\b",
  r"\blink zur\b",
  r"\bwebseite\b",
  r"\blearn more\b",
  r"\boffers in the region\b",
  r"\bfind the right\b",
  r"\bculinary offers?\b",
  r"^https?://",
  r"\bcourtesy of\b",
  r"\bevents? in the .* region\b",
)

BAD_URL_TOKENS = (
  "facebook.com/share",
  "twitter.com/share",
  "x.com/share",
  "open.spotify.com",
  "spotify.com",
  "bandcamp.com",
  "mailto:",
  "/privacy",
  "/datenschutz",
  "/impressum",
  "/kontakt",
  "/contact",
  "/newsletter",
  "/press",
)

BROAD_INDEX_SEGMENTS = {
  "calendar",
  "calendar_1",
  "events",
  "event",
  "program",
  "programme",
  "programm",
  "kalender",
  "veranstaltungen",
}

PAGEISH_TITLE_PATTERNS = (
  r"\bworkshops?\s*&\s*research\b",
  r"\bfull workshop schedule\b",
  r"\bworkshop office\b",
  r"\bworkshop centre\b",
  r"\bopen doors\b",
  r"\bour workshop\b",
  r"\bwebsite nachlesen\b",
  r"\blink zur\b",
  r"\blearn more\b",
  r"\boffers in the region\b",
  r"\bfind the right\b",
  r"\bzur erweiterten suche\b",
  r"\barchive\b",
  r"\bgallery\b",
)

CITY_ALIASES = {
  "vienna": ("vienna", "wien"),
  "graz": ("graz",),
  "linz": ("linz",),
  "salzburg": ("salzburg",),
  "innsbruck": ("innsbruck",),
  "klagenfurt": ("klagenfurt", "klagenfurt am worthersee", "klagenfurt am woerthersee"),
  "telfs": ("telfs",),
  "munich": ("munich", "muenchen", "münchen"),
  "schladming": ("schladming",),
  "wachau": ("wachau",),
}

NATIONAL_SOURCE_IDS = {
  "at.national.1000things",
  "at.national.oeticket",
  "at.national.falter-events",
  "at.national.ntry",
  "at.national.ntry-events",
  "at.national.eventbrite",
  "at.national.kultur-net",
  "at.national.kultur-net-events",
}

MONTH_NAMES = {
  "january": 1,
  "jan": 1,
  "januar": 1,
  "february": 2,
  "feb": 2,
  "februar": 2,
  "march": 3,
  "mar": 3,
  "märz": 3,
  "maerz": 3,
  "april": 4,
  "apr": 4,
  "may": 5,
  "mai": 5,
  "june": 6,
  "jun": 6,
  "juni": 6,
  "july": 7,
  "jul": 7,
  "juli": 7,
  "august": 8,
  "aug": 8,
  "september": 9,
  "sep": 9,
  "sept": 9,
  "oktober": 10,
  "october": 10,
  "okt": 10,
  "oct": 10,
  "november": 11,
  "nov": 11,
  "december": 12,
  "dezember": 12,
  "dec": 12,
  "dez": 12,
}


def normalize_text(value: object) -> str:
  return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def has_date_signal(value: object) -> bool:
  text = str(value or "")
  return bool(
    re.search(r"\b20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}\b", text)
    or re.search(r"\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b", text)
    or re.search(r"\b\d{4}-\d{2}-\d{2}[t_ -]\d{2}", text, re.IGNORECASE)
  )


def parse_target_date(value: object) -> date | None:
  try:
    return date.fromisoformat(str(value or "")[:10])
  except ValueError:
    return None


def explicit_dates(value: object, target: date | None = None) -> list[date]:
  text = str(value or "")
  found: list[date] = []
  for year, month, day in re.findall(r"\b(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})\b", text):
    try:
      found.append(date(int(year), int(month), int(day)))
    except ValueError:
      pass
  for day, month, year in re.findall(r"\b(\d{1,2})[.](\d{1,2})[.](20\d{2})\b", text):
    try:
      found.append(date(int(year), int(month), int(day)))
    except ValueError:
      pass
  if target:
    for day, month in re.findall(r"\b(\d{1,2})[.](\d{1,2})[.](?!\d)", text):
      try:
        found.append(date(target.year, int(month), int(day)))
      except ValueError:
        pass
  month_pattern = "|".join(re.escape(name) for name in sorted(MONTH_NAMES, key=len, reverse=True))
  for day, month_name, year in re.findall(rf"\b(\d{{1,2}})\.?\s+({month_pattern})\s+(20\d{{2}})\b", text, re.IGNORECASE):
    try:
      found.append(date(int(year), MONTH_NAMES[month_name.lower()], int(day)))
    except (KeyError, ValueError):
      pass
  return found


def city_mentions(value: object) -> set[str]:
  text = normalize_text(value)
  mentions = set()
  for city, aliases in CITY_ALIASES.items():
    if any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases):
      mentions.add(city)
  return mentions


def title_specificity(title: str) -> int:
  words = re.findall(r"[A-Za-zÀ-ž0-9]+", title)
  meaningful = [word for word in words if len(word) > 2]
  return len(meaningful)


def url_path_segments(url: str) -> list[str]:
  parsed = urlparse(url)
  return [segment.lower() for segment in parsed.path.strip("/").split("/") if segment]


def is_broad_index_url(url: str) -> bool:
  segments = url_path_segments(url)
  if not segments:
    return True
  last = segments[-1]
  return last in BROAD_INDEX_SEGMENTS or len(segments) <= 1 and last in BROAD_INDEX_SEGMENTS


def has_detail_url_shape(url: str) -> bool:
  segments = url_path_segments(url)
  if not segments:
    return False
  last = segments[-1]
  if last in BROAD_INDEX_SEGMENTS or last in {"archive", "en", "de"}:
    return False
  parsed = urlparse(url)
  if re.search(r"\b20\d{2}|\d{1,2}[-_.]\d{1,2}", parsed.path):
    return True
  return len(segments) >= 2 and len(last) >= 8


def is_share_url(url: str) -> bool:
  lower = str(url or "").lower()
  if any(token in lower for token in BAD_URL_TOKENS):
    return True
  parsed = urlparse(lower)
  if parsed.netloc in {"www.facebook.com", "facebook.com"} and parsed.path.strip("/") == "share.php":
    return True
  query = parse_qs(parsed.query)
  return any(key.lower() in {"share", "share_url"} for key in query)


def event_quality(event: dict) -> dict:
  title = str(event.get("title") or "").strip()
  title_key = normalize_text(title)
  url = str(event.get("eventUrl") or "").strip()
  combined = f"{title} {url}"
  reasons = []
  target = parse_target_date(event.get("targetDate") or event.get("date"))

  if not title or title_key in EXACT_BAD_TITLES:
    reasons.append("generic_title")
  if any(re.search(pattern, title_key) for pattern in BAD_TITLE_PATTERNS):
    reasons.append("non_event_title")
  if is_share_url(url):
    reasons.append("share_or_utility_url")
  if is_broad_index_url(url) and not has_date_signal(combined):
    reasons.append("broad_index_url")
  if title_specificity(title) < 3 and not has_date_signal(combined):
    reasons.append("weak_title")
  dates = explicit_dates(combined, target)
  if target and dates and max(dates) < target:
    reasons.append("past_date")
  target_city = normalize_text(event.get("city"))
  target_city_key = next((city for city, aliases in CITY_ALIASES.items() if target_city in aliases or city == target_city), target_city)
  mentions = city_mentions(combined)
  if mentions and target_city_key in CITY_ALIASES and target_city_key not in mentions:
    reasons.append("city_mismatch")
  source_id = str(event.get("sourceId") or "")
  source_scope = normalize_text(event.get("sourceScope"))
  source_kind = normalize_text(event.get("sourceKind") or event.get("sourceType"))
  candidate_kind = str(event.get("candidateKind") or "")
  if (source_scope == "national" or source_id in NATIONAL_SOURCE_IDS) and target_city_key in CITY_ALIASES and target_city_key not in mentions:
    reasons.append("missing_city_evidence")

  if any(re.search(pattern, title_key) for pattern in PAGEISH_TITLE_PATTERNS):
    reasons.append("page_or_section_title")

  has_evidence = has_date_signal(combined) or candidate_kind == "jsonld-event"
  if candidate_kind == "extracted-event-lead" and not has_detail_url_shape(url) and not has_date_signal(combined):
    reasons.append("missing_detail_page")
  if "editorial" in source_kind and not has_date_signal(combined):
    reasons.append("editorial_without_date")
  if source_scope == "national" and not has_date_signal(combined):
    reasons.append("national_without_date")
  if not has_evidence and title_specificity(title) < 5:
    reasons.append("missing_event_evidence")

  accepted = not reasons
  return {
    "accepted": accepted,
    "reviewStatus": "new" if accepted else "rejected_non_event",
    "reasons": reasons,
  }


def series_key(event: dict) -> str:
  title = normalize_text(event.get("title"))
  title = re.sub(r"\b(mo|di|mi|do|fr|sa|so|mon|tue|wed|thu|fri|sat|sun)\.?\s*", "", title)
  title = re.sub(r"\b20\d{2}[-_/]\d{1,2}[-_/]\d{1,2}(?:[t_ -]\d{2,4})?\b", "", title)
  title = re.sub(r"\b\d{1,2}[./-]\d{1,2}[./-]?(?:\d{2,4})?\b", "", title)
  title = re.sub(r"\b\d{1,2}:\d{2}\s*(?:uhr)?(?:\s*[–-]\s*\d{1,2}:\d{2})?\b", "", title)
  title = re.sub(r"\s+", " ", title).strip()
  source = normalize_text(event.get("sourceName"))
  return f"{source}|{title}"


def source_kind_limit(event: dict, default_limit: int) -> int:
  bucket = str(event.get("sourceQualityBucket") or source_quality_bucket({
    "scope": event.get("sourceScope", ""),
    "sourceKind": event.get("sourceKind") or event.get("sourceType") or "",
  }))
  if bucket == "official_baseline":
    return min(default_limit, 8)
  if bucket == "event_platform":
    return min(default_limit, 10)
  if bucket == "national_discovery":
    return min(default_limit, 4)
  return default_limit * 3


def filter_event_leads(events: list[dict], *, per_series_limit: int = 3, per_source_limit: int = 12) -> tuple[list[dict], list[dict]]:
  accepted = []
  rejected = []
  series_counts: dict[str, int] = defaultdict(int)
  source_counts: dict[str, int] = defaultdict(int)
  source_kind_counts: dict[str, int] = defaultdict(int)
  for event in events:
    quality = event_quality(event)
    if not quality["accepted"]:
      rejected.append({
        "title": event.get("title", ""),
        "eventUrl": event.get("eventUrl", ""),
        "sourceName": event.get("sourceName", ""),
        "reasons": quality["reasons"],
      })
      continue
    key = series_key(event)
    series_counts[key] += 1
    if series_counts[key] > per_series_limit:
      rejected.append({
        "title": event.get("title", ""),
        "eventUrl": event.get("eventUrl", ""),
        "sourceName": event.get("sourceName", ""),
        "reasons": ["duplicate_series"],
      })
      continue
    source_key = normalize_text(event.get("sourceName"))
    source_counts[source_key] += 1
    if source_counts[source_key] > per_source_limit:
      rejected.append({
        "title": event.get("title", ""),
        "eventUrl": event.get("eventUrl", ""),
        "sourceName": event.get("sourceName", ""),
        "reasons": ["source_daily_cap"],
      })
      continue
    source_kind_key = str(event.get("sourceQualityBucket") or source_quality_bucket({
      "scope": event.get("sourceScope", ""),
      "sourceKind": event.get("sourceKind") or event.get("sourceType") or "",
    }))
    source_kind_counts[source_kind_key] += 1
    if source_kind_counts[source_kind_key] > source_kind_limit(event, per_source_limit):
      rejected.append({
        "title": event.get("title", ""),
        "eventUrl": event.get("eventUrl", ""),
        "sourceName": event.get("sourceName", ""),
        "reasons": ["source_kind_daily_cap"],
      })
      continue
    clean = dict(event)
    clean["reviewStatus"] = quality["reviewStatus"]
    accepted.append(clean)
  return accepted, rejected
