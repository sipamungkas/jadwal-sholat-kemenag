"""Filesystem cache, slugs and small parsing helpers."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from typing import Any

_TIME_KEYS = (
    "imsak",
    "subuh",
    "terbit",
    "dhuha",
    "dzuhur",
    "ashar",
    "maghrib",
    "isya",
)


def slugify(name: str) -> str:
    """Filesystem-safe lowercase slug: ``KAB. ACEH BARAT`` -> ``kab-aceh-barat``."""
    norm = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    norm = re.sub(r"[^a-z0-9]+", "-", norm.lower()).strip("-")
    return norm or "unknown"


def unique_slug(name: str, seen: set[str]) -> str:
    """slugify with a ``-2``, ``-3``... suffix when the slug is already taken."""
    slug = slugify(name)
    if slug not in seen:
        seen.add(slug)
        return slug
    n = 2
    while f"{slug}-{n}" in seen:
        n += 1
    slug = f"{slug}-{n}"
    seen.add(slug)
    return slug


def write_json(path: str, payload: Any) -> None:
    """Write JSON atomically (tmp file + rename) so crashes never leave halves."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str) -> Any | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


_DMS_RE = re.compile(
    r"""^\s*(\d+)\s*°\s*(?:(\d+)\s*')?\s*(?:(\d+(?:\.\d+)?)\s*")?\s*([NSEW])\s*$""",
    re.VERBOSE,
)


def parse_dms(value: str | None) -> float | None:
    """``4° 09' 31.76" N`` -> ``4.1588``; S/W bearings are negative."""
    if not value:
        return None
    m = _DMS_RE.match(value)
    if not m:
        return None
    deg, minutes, seconds, bearing = m.groups()
    result = float(deg) + float(minutes or 0) / 60 + float(seconds or 0) / 3600
    if bearing in "SW":
        result = -result
    return round(result, 6)


def flat_day_row(
    date: str,
    day: dict[str, Any],
    prov: str,
    kabko: str,
    lat: float | None,
    lon: float | None,
) -> dict[str, Any]:
    """One calendar day flattened to a single row (used by CSV/JSONL export)."""
    row: dict[str, Any] = {
        "date": date,
        "province": prov,
        "city": kabko,
        "latitude": lat,
        "longitude": lon,
    }
    for key in _TIME_KEYS:
        row[key] = day.get(key)
    return row
