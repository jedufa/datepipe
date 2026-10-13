from __future__ import annotations

import csv
from pathlib import Path


ASSET_DIR = Path("data/city_packs")
SOURCE_REQUIRED = {"country", "countryCode", "city", "region", "sourceStatus", "id", "name", "url", "sourceType", "categories", "privacyFit", "confidence"}


def read_rows(path: Path) -> list[dict]:
  with path.open("r", encoding="utf-8", newline="") as fh:
    return list(csv.DictReader(fh))


def test_city_pack_csv_assets_parse_and_have_required_columns():
  for filename in ("city_summary.csv", "sources_selected.csv", "sources_all.csv"):
    path = ASSET_DIR / filename
    assert path.exists(), filename
    rows = read_rows(path)
    assert rows, filename
    assert all(any(str(value).strip() for value in row.values()) for row in rows[:5])

  for filename in ("sources_selected.csv", "sources_all.csv"):
    rows = read_rows(ASSET_DIR / filename)
    assert SOURCE_REQUIRED <= set(rows[0].keys())


def test_selected_sources_have_valid_core_city_fields_and_no_duplicate_ids_per_city():
  rows = read_rows(ASSET_DIR / "sources_selected.csv")
  seen: set[tuple[str, str]] = set()

  for row in rows:
    assert row["country"] == "Austria"
    assert row["countryCode"] == "AT"
    assert row["city"].strip()
    assert row["id"].startswith("at.")
    key = (row["city"].lower(), row["id"].lower())
    assert key not in seen
    seen.add(key)
