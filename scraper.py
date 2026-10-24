#!/usr/bin/env python3
"""CLI scraper for jadwal sholat from bimasislam.kemenag.dev.

Examples:
  python3 scraper.py scrape --year 2026-2070            # everything, resumable
  python3 scraper.py scrape --year 2026 --prov aceh --months 1-3
  python3 scraper.py scrape --year 2026 --prov riau --kabko "kota pekanbaru" --force
  python3 scraper.py list
  python3 scraper.py export --format csv --out-file data/jadwal.csv
  python3 scraper.py db                                  # raw cache -> jadwal.db
  python3 scraper.py db --rebuild                        # drop + rebuild the db

Layout under --out (default ``data``):
  provinsi.json                            cached province list
  kabko/<prov-slug>.json                   cached regency list per province
  raw/<year>/<prov-slug>/<kabko-slug>/<MM>.json   raw monthly API payloads
  schedules/<year>/<prov-slug>/<kabko-slug>.json  merged whole-year schedule
  errors-<year>.json                       failures from the last scrape run
  jadwal.db                                SQLite store built from raw payloads

Re-running a scrape skips work whose raw month file already exists
(--force re-fetches), so an interrupted multi-year scrape can simply be rerun.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import sqlite3
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kemenag import __version__
from kemenag.client import KemenagClient, KemenagError
from kemenag.store import flat_day_row, parse_dms, read_json, unique_slug, write_json

MONTHS_IN_YEAR = 12
TIME_KEYS = (
    "imsak",
    "subuh",
    "terbit",
    "dhuha",
    "dzuhur",
    "ashar",
    "maghrib",
    "isya",
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _parse_int_spec(spec: str, lo: int, hi: int) -> list[int]:
    """``a``, ``a-b`` or ``a,b,c`` -> sorted unique ints within [lo, hi]."""
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, _, b = part.partition("-")
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    bad = [v for v in values if not lo <= v <= hi]
    if bad:
        raise ValueError(f"values out of range {lo}..{hi}: {sorted(bad)}")
    return sorted(values)


def parse_months(spec: str) -> list[int]:
    return _parse_int_spec(spec, 1, MONTHS_IN_YEAR)


def parse_years(spec: str) -> list[int]:
    return _parse_int_spec(spec, 1900, 2200)


# -- directory discovery (with caching + resume) -----------------------------


def load_provinsi(client: KemenagClient, out: Path) -> list[dict[str, str]]:
    cache = out / "provinsi.json"
    cached = read_json(str(cache))
    if isinstance(cached, list) and cached:
        return cached
    entries = client.get_provinsi()
    write_json(str(cache), entries)
    return entries


def load_kabko(client: KemenagClient, out: Path, prov_slug: str, prov_id: str) -> list[dict[str, str]]:
    cache = out / "kabko" / f"{prov_slug}.json"
    cached = read_json(str(cache))
    if isinstance(cached, list) and cached:
        return cached
    entries = client.get_kabko(prov_id)
    write_json(str(cache), entries)
    return entries


def pick_provinces(
    entries: list[dict[str, str]], prov_filter: str | None
) -> list[dict[str, str]]:
    if not prov_filter:
        return entries
    needle = prov_filter.strip().lower()
    exact = [p for p in entries if p["name"].lower() == needle]
    if exact:
        return exact
    partial = [p for p in entries if needle in p["name"].lower()]
    if not partial:
        raise SystemExit(f"no province matches {prov_filter!r}")
    return partial


def pick_kabko(entries: list[dict[str, str]], kabko_filter: str | None) -> list[dict[str, str]]:
    if not kabko_filter:
        return entries
    needle = kabko_filter.strip().lower()
    return [k for k in entries if needle in k["name"].lower()]


# -- scraping -----------------------------------------------------------------


def build_plan(client: KemenagClient, out: Path, args: argparse.Namespace) -> list[dict[str, str]]:
    """Resolve province/kabko filters into scrape targets with unique slugs."""
    provinces = pick_provinces(load_provinsi(client, out), args.prov)
    log(f"provinces to scrape: {len(provinces)}")

    plan: list[dict[str, str]] = []
    for prov in provinces:
        prov_slug_seen: set[str] = set()
        prov_slug = unique_slug(prov["name"], prov_slug_seen)
        kabkos = pick_kabko(load_kabko(client, out, prov_slug, prov["id"]), args.kabko)
        for kabko in kabkos:
            kabko_slug = unique_slug(kabko["name"], prov_slug_seen)
            plan.append(
                {
                    "prov": prov["name"],
                    "prov_id": prov["id"],
                    "prov_slug": prov_slug,
                    "kabko": kabko["name"],
                    "kabko_id": kabko["id"],
                    "kabko_slug": kabko_slug,
                }
            )
        if args.kabko and not kabkos:
            log(f"  no kabko matches {args.kabko!r} in {prov['name']}")
    if not plan:
        raise SystemExit("nothing to scrape (check --prov/--kabko filters)")
    return plan


def scrape(args: argparse.Namespace) -> int:
    out = Path(args.out)
    client = KemenagClient(
        timeout=args.timeout,
        retries=args.retries,
        min_interval=args.delay,
    )
    try:
        months = parse_months(args.months)
        years = parse_years(args.year)
    except ValueError as e:
        raise SystemExit(f"bad --months/--year value: {e}")

    plan = build_plan(client, out, args)
    log(
        f"{len(plan)} kabko x {len(months)} month(s) x {len(years)} year(s)"
        f" ({years[0]}..{years[-1]}) = {len(plan) * len(months) * len(years)} requests max"
    )

    total_errors = 0
    started = time.monotonic()
    for year in years:
        errors, interrupted = scrape_year(client, out, plan, year, months, args, started)
        total_errors += errors
        if interrupted:
            log(f"interrupted in {year} - progress is cached, rerun to continue")
            return 130

    log(
        f"all years done: {total_errors} failed request(s), "
        f"elapsed {(time.monotonic()-started)/60:.1f}m"
    )
    return 1 if total_errors else 0


def scrape_year(
    client: KemenagClient,
    out: Path,
    plan: list[dict[str, str]],
    year: int,
    months: list[int],
    args: argparse.Namespace,
    run_started: float,
) -> tuple[int, bool]:
    """Fetch all (kabko, month) tasks for one year. Returns (errors, interrupted)."""
    tasks: list[tuple[dict[str, str], int]] = []
    skipped = 0
    for loc in plan:
        for month in months:
            raw_path = raw_month_path(out, year, loc, month)
            if raw_path.is_file() and not args.force:
                skipped += 1
            else:
                tasks.append((loc, month))

    log(
        f"{year}: {len(tasks) + skipped} requests total, {skipped} cached, "
        f"{len(tasks)} to fetch"
    )

    errors: list[dict[str, Any]] = []
    errors_lock = threading.Lock()

    def fetch(task: tuple[dict[str, str], int]) -> None:
        loc, month = task
        try:
            payload = client.get_jadwal(loc["prov_id"], loc["kabko_id"], year, month)
            write_json(str(raw_month_path(out, year, loc, month)), payload)
        except KemenagError as e:
            with errors_lock:
                errors.append(
                    {
                        "prov": loc["prov"],
                        "kabko": loc["kabko"],
                        "year": year,
                        "month": month,
                        "error": str(e),
                    }
                )

    if tasks:
        with cf.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(fetch, t) for t in tasks]
            try:
                done = 0
                for fut in cf.as_completed(futures):
                    fut.result()
                    done += 1
                    if done % 100 == 0 or done == len(tasks):
                        rate = done / max(1e-9, time.monotonic() - run_started)
                        log(f"  {year}: {done}/{len(tasks)} ({rate:.1f}/s overall, {len(errors)} errors)")
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                return len(errors), True

    merged = 0
    for loc in plan:
        merged += merge_schedule(out, year, loc, months)
    log(f"{year}: merged {merged}/{len(plan)} kabko schedules into {out / 'schedules'}")

    if errors:
        write_json(str(out / f"errors-{year}.json"), errors)
        log(f"{year}: {len(errors)} failures saved to {out / f'errors-{year}.json'}")
    return len(errors), False


def raw_month_path(out: Path, year: int, loc: dict[str, str], month: int) -> Path:
    return out / "raw" / str(year) / loc["prov_slug"] / loc["kabko_slug"] / f"{month:02d}.json"


def merge_schedule(out: Path, year: int, loc: dict[str, str], months: list[int]) -> int:
    """Combine the raw month files of one kabko into ``schedules/<y>/...json``.

    Returns 1 when the merged file was written, 0 when no month exists yet.
    """
    raw_dir = out / "raw" / str(year) / loc["prov_slug"] / loc["kabko_slug"]
    data: dict[str, Any] = {}
    meta: dict[str, Any] = {}
    have: list[int] = []
    for month in months:
        payload = read_json(str(raw_dir / f"{month:02d}.json"))
        if not isinstance(payload, dict):
            continue
        have.append(month)
        meta = {
            "prov": payload.get("prov"),
            "kabko": payload.get("kabko"),
            "lintang": payload.get("lintang"),
            "bujur": payload.get("bujur"),
        }
        data.update(payload.get("data") or {})
    if not have:
        return 0
    dest = out / "schedules" / str(year) / loc["prov_slug"] / f"{loc['kabko_slug']}.json"
    write_json(
        str(dest),
        {
            "prov": meta.get("prov") or loc["prov"],
            "kabko": meta.get("kabko") or loc["kabko"],
            "lintang": meta.get("lintang"),
            "bujur": meta.get("bujur"),
            "lat": parse_dms(meta.get("lintang")),
            "lon": parse_dms(meta.get("bujur")),
            "year": year,
            "months": have,
            "data": dict(sorted(data.items())),
        },
    )
    return 1


# -- SQLite store ---------------------------------------------------------------


SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY,
    prov        TEXT NOT NULL,
    kabko       TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    prov_token  TEXT,
    kabko_token TEXT,
    UNIQUE (prov, kabko)
);
CREATE TABLE IF NOT EXISTS schedules (
    location_id INTEGER NOT NULL REFERENCES locations(id),
    date        TEXT NOT NULL,
    imsak TEXT, subuh TEXT, terbit TEXT, dhuha TEXT,
    dzuhur TEXT, ashar TEXT, maghrib TEXT, isya TEXT,
    PRIMARY KEY (location_id, date)
);
CREATE INDEX IF NOT EXISTS idx_schedules_date ON schedules(date);
"""


def db_cmd(args: argparse.Namespace) -> int:
    """Build/update jadwal.db from the raw scrape cache (no network)."""
    out = Path(args.out)
    db_path = Path(args.db or out / "jadwal.db")
    raw_root = out / "raw"
    if not raw_root.is_dir():
        raise SystemExit(f"no raw cache under {raw_root} - run a scrape first")
    try:
        year_filter = set(parse_years(args.year)) if args.year else None
    except ValueError as e:
        raise SystemExit(f"bad --year value: {e}")

    prov_token_by_name = {
        p["name"]: p["id"] for p in (read_json(str(out / "provinsi.json")) or [])
    }

    con = sqlite3.connect(str(db_path))
    con.executescript(SCHEMA)
    if args.rebuild:
        con.executescript("DROP TABLE IF EXISTS schedules; DROP TABLE IF EXISTS locations;")
        con.executescript(SCHEMA)
        log(f"rebuilt {db_path} from scratch")

    started = time.monotonic()
    month_files = [
        p for p in sorted(raw_root.rglob("[0-9][0-9].json"))
        if year_filter is None or int(p.parent.parent.parent.name) in year_filter
    ]
    if not month_files:
        raise SystemExit(f"no raw month files found under {raw_root}")

    for i, month_file in enumerate(month_files, 1):
        payload = read_json(str(month_file))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            log(f"  skipping unreadable {month_file}")
            continue
        prov = payload.get("prov")
        kabko = payload.get("kabko")
        if not prov or not kabko:
            continue

        prov_slug = month_file.parent.parent.name
        kabko_cache = read_json(str(out / "kabko" / f"{prov_slug}.json")) or []
        kabko_token = next((k["id"] for k in kabko_cache if k["name"] == kabko), None)

        with con:
            con.execute(
                """INSERT INTO locations (prov, kabko, lat, lon, prov_token, kabko_token)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(prov, kabko) DO UPDATE SET
                     lat=excluded.lat, lon=excluded.lon,
                     prov_token=excluded.prov_token, kabko_token=excluded.kabko_token""",
                (
                    prov,
                    kabko,
                    parse_dms(payload.get("lintang")),
                    parse_dms(payload.get("bujur")),
                    prov_token_by_name.get(prov),
                    kabko_token,
                ),
            )
            loc_id = con.execute(
                "SELECT id FROM locations WHERE prov = ? AND kabko = ?", (prov, kabko)
            ).fetchone()[0]
            con.executemany(
                f"INSERT OR REPLACE INTO schedules (location_id, date, {', '.join(TIME_KEYS)}) "
                f"VALUES ({', '.join('?' * (2 + len(TIME_KEYS)))})",
                [
                    (loc_id, date, *(day.get(k) for k in TIME_KEYS))
                    for date, day in sorted(payload["data"].items())
                ],
            )
        if i % 500 == 0 or i == len(month_files):
            log(f"  {i}/{len(month_files)} month files imported")

    stats = con.execute(
        """SELECT (SELECT COUNT(*) FROM locations),
                  (SELECT COUNT(*) FROM schedules),
                  (SELECT MIN(date) FROM schedules),
                  (SELECT MAX(date) FROM schedules)"""
    ).fetchone()
    con.close()
    log(
        f"{db_path}: {stats[0]} locations, {stats[1]} schedule rows, "
        f"{stats[2]}..{stats[3]}, built in {time.monotonic()-started:.1f}s"
    )
    return 0


# -- export / listing ----------------------------------------------------------


def iter_schedule_files(out: Path) -> list[Path]:
    schedules = out / "schedules"
    if not schedules.is_dir():
        return []
    return sorted(schedules.rglob("*.json"))


def export(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out_file = args.out_file or str(Path(args.out) / f"jadwal.{args.format}")
    files = iter_schedule_files(out)
    if not files:
        raise SystemExit(f"no merged schedules under {out / 'schedules'} - run a scrape first")

    dest = Path(out_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for path in files:
        sched = read_json(str(path))
        if not isinstance(sched, dict):
            continue
        for date, day in (sched.get("data") or {}).items():
            rows.append(
                flat_day_row(
                    date, day, sched.get("prov", ""), sched.get("kabko", ""),
                    sched.get("lat"), sched.get("lon"),
                )
            )
    rows.sort(key=lambda r: (r["date"], r["province"], r["city"]))

    if args.format == "csv":
        with open(dest, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
    else:  # jsonl
        with open(dest, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    log(f"wrote {len(rows)} day-rows from {len(files)} schedules -> {dest}")
    return 0


def list_locations(args: argparse.Namespace) -> int:
    out = Path(args.out)
    kabko_dir = out / "kabko"
    if not (out / "provinsi.json").is_file():
        raise SystemExit(f"no {out / 'provinsi.json'} - run a scrape first")
    caches = sorted(kabko_dir.glob("*.json"))
    if not caches:
        raise SystemExit(f"no kabko caches under {kabko_dir} - run a scrape first")
    for cache in caches:
        entries = read_json(str(cache)) or []
        print(f"{cache.stem}  ({len(entries)} kabko)")
        if args.kabko:
            needle = args.kabko.lower()
            for k in entries:
                if needle in k["name"].lower():
                    print(f"    {k['name']}")
    return 0


# -- entry point ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape jadwal sholat from bimasislam.kemenag.dev",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="fetch schedules (resumable)")
    p_scrape.add_argument("--year", default=str(datetime.now().year),
                          help="e.g. 2026, 2026-2030 or 2026,2030 (default: this year)")
    p_scrape.add_argument("--months", default="1-12",
                          help="e.g. 1-3 or 1,6,12 (default: all)")
    p_scrape.add_argument("--prov", help="province name filter (substring, case-insensitive)")
    p_scrape.add_argument("--kabko", help="regency/city name filter (substring)")
    p_scrape.add_argument("--out", default="data", help="output directory (default: data)")
    p_scrape.add_argument("--workers", type=int, default=4, help="parallel requests (default: 4)")
    p_scrape.add_argument("--delay", type=float, default=0.25,
                          help="min seconds between any two requests (default: 0.25)")
    p_scrape.add_argument("--retries", type=int, default=4)
    p_scrape.add_argument("--timeout", type=float, default=30.0)
    p_scrape.add_argument("--force", action="store_true", help="re-fetch even if cached")
    p_scrape.set_defaults(func=scrape)

    p_db = sub.add_parser("db", help="build jadwal.db (SQLite) from the raw cache")
    p_db.add_argument("--out", default="data", help="scrape output dir (default: data)")
    p_db.add_argument("--db", default=None, help="db path (default: <out>/jadwal.db)")
    p_db.add_argument("--year", default=None,
                      help="only import these years, e.g. 2026-2070 (default: all)")
    p_db.add_argument("--rebuild", action="store_true",
                      help="drop and recreate tables instead of upserting")
    p_db.set_defaults(func=db_cmd)

    p_export = sub.add_parser("export", help="export merged schedules to csv/jsonl")
    p_export.add_argument("--format", choices=["csv", "jsonl"], default="csv")
    p_export.add_argument("--out", default="data", help="scrape output dir (default: data)")
    p_export.add_argument("--out-file", default=None,
                          help="destination file (default: <out>/jadwal.<format>)")
    p_export.set_defaults(func=export)

    p_list = sub.add_parser("list", help="list cached provinces/kabko")
    p_list.add_argument("--out", default="data")
    p_list.add_argument("--kabko", help="only show kabko matching this substring")
    p_list.set_defaults(func=list_locations)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
