from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")
  tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
  tmp.replace(path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
  if not path.exists():
    return []
  rows: list[dict[str, Any]] = []
  for raw in path.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
      continue
    try:
      parsed = json.loads(raw)
    except json.JSONDecodeError:
      continue
    if isinstance(parsed, dict):
      rows.append(parsed)
  return rows
