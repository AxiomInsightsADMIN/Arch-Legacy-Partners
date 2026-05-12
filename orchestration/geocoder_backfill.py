"""Census Geocoder backfill for NC ND + NC SF.

Targets the ~2,000 NC facilities that arrived from Phase 2 with addresses
but no native coords:

  - nc_deq_septage_firm_list      759 rows  (street + city from `Address`)
  - nc_deq_non_discharge_facilities 1,259 rows  (FACILITY + COUNTY,
                                                  no street column; most
                                                  won't geocode but SFR
                                                  rows often have an
                                                  address-like FACILITY)

The script:
  1) Walks every raw row in those two sources whose normalized form has
     no native lat/lng.
  2) Synthesizes the one-line address via
     `resolver._normalize.synthesize_address_for_geocoding(raw)`.
  3) Calls `orchestration.geocoder.geocode_with_state_check(...)` which:
       - Looks up `geocoding_cache` by address hash (skips re-geocoding)
       - Calls Census `onelineaddress` on cache miss
       - Caches success / failure / mismatch outcomes
  4) Reports counts: total attempted, success, failure (no match),
     state-consistency mismatch, transport error, plus the confidence
     distribution.

Usage:
  python -m orchestration.geocoder_backfill                # full pass
  python -m orchestration.geocoder_backfill --limit 50     # smoke test
  python -m orchestration.geocoder_backfill --sources nc_deq_septage_firm_list

This is a one-off operational tool, not a recurring scraper. Re-running
is free because `geocoding_cache` covers all attempts (success and
failure). The 10-minute Bash-tool wall clock is respected via a
ThreadPoolExecutor with concurrency=4 (Census doesn't publish a hard rate
limit but recommends polite usage; 4 concurrent stays well under any
realistic threshold).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from orchestration.geocoder import geocode_with_state_check  # noqa: E402
from resolver._normalize import normalize, synthesize_address_for_geocoding  # noqa: E402
from scrapers._loader_utils import db_connect  # noqa: E402

DEFAULT_SOURCES: tuple[str, ...] = (
    "nc_deq_septage_firm_list",
    "nc_deq_non_discharge_facilities",
)

DEFAULT_CONCURRENCY = 4


def _load_targets(cur, sources: tuple[str, ...], limit: int | None) -> list[tuple]:
    """Return list of (raw_id, source_slug, source_record_id, raw_payload,
    address, state) for raws that need geocoding (no native coords + has
    a synthesizeable address). Filters out rows already in geocoding_cache
    so re-runs are fast."""
    placeholders = ",".join(["%s"] * len(sources))
    sql = f"""
        SELECT r.id, s.slug, r.source_record_id, r.raw_payload
          FROM raw_facility_record r
          JOIN source s ON s.id = r.source_id
         WHERE s.slug IN ({placeholders})
         ORDER BY r.id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    cur.execute(sql, sources)
    rows = cur.fetchall()
    out: list[tuple] = []
    for raw_id, slug, recid, payload in rows:
        raw = normalize(
            raw_id=raw_id,
            source_slug=slug,
            source_record_id=recid,
            raw_payload=payload,
        )
        # Skip rows that already carry native coords.
        if raw.latitude is not None and raw.longitude is not None:
            continue
        addr = synthesize_address_for_geocoding(raw)
        if not addr:
            continue
        out.append((raw_id, slug, recid, addr, raw.state))
    return out


def _geocode_one(conn, *, raw_id: int, address: str, state: str | None) -> dict:
    """Wrapper around geocode_with_state_check for the ThreadPoolExecutor.
    Returns a dict with the raw_id and the outcome fields."""
    result = geocode_with_state_check(address=address, state=state, conn=conn)
    return {
        "raw_id": raw_id,
        "address": address,
        "state": state,
        "lat": result.lat,
        "lng": result.lng,
        "confidence": result.confidence,
        "consistency": result.consistency,
        "notes": result.notes,
        "review_flag": result.review_flag,
    }


def run(
    *,
    sources: tuple[str, ...] = DEFAULT_SOURCES,
    limit: int | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> dict:
    started = time.time()
    print(
        f"[geocoder-backfill] sources={sources} limit={limit} concurrency={concurrency}",
        flush=True,
    )

    conn = db_connect()
    # ThreadPoolExecutor + a shared psycopg2 connection is unsafe for the
    # cache-write step because psycopg2 connections aren't thread-safe.
    # Open one connection per worker thread.
    cur = conn.cursor()
    targets = _load_targets(cur, sources, limit)
    cur.close()
    conn.close()

    print(f"[geocoder-backfill] target rows after filter: {len(targets):,}", flush=True)
    if not targets:
        return {"attempted": 0, "elapsed_sec": 0.0}

    # One connection per worker thread.
    def worker(rec):
        local_conn = db_connect()
        try:
            return _geocode_one(
                local_conn,
                raw_id=rec[0],
                address=rec[3],
                state=rec[4],
            )
        finally:
            local_conn.close()

    counts: Counter = Counter()
    confidence: Counter = Counter()
    notes: Counter = Counter()
    per_source_status: dict[str, Counter] = {slug: Counter() for slug in sources}
    by_source = {rec[0]: rec[1] for rec in targets}

    progress_every = max(50, len(targets) // 20)
    completed_n = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(worker, rec): rec for rec in targets}
        for fut in as_completed(futures):
            res = fut.result()
            slug = by_source[res["raw_id"]]
            confidence[res["confidence"]] += 1
            per_source_status[slug][res["confidence"]] += 1
            if res["lat"] is not None and res["lng"] is not None:
                counts["geocoded"] += 1
            else:
                counts["failed"] += 1
            if res["consistency"] == "outside":
                counts["state_mismatch"] += 1
            if res["notes"]:
                notes[res["notes"]] += 1
            completed_n += 1
            if completed_n % progress_every == 0 or completed_n == len(targets):
                elapsed = time.time() - started
                rate = completed_n / max(elapsed, 0.1)
                print(
                    f"[geocoder-backfill]   {completed_n:>5,}/{len(targets):,}  "
                    f"({rate:.1f}/s)  "
                    f"geocoded={counts['geocoded']:,} failed={counts['failed']:,}",
                    flush=True,
                )

    elapsed = time.time() - started
    return {
        "elapsed_sec": round(elapsed, 1),
        "attempted": len(targets),
        "geocoded": counts["geocoded"],
        "failed": counts["failed"],
        "state_mismatch": counts["state_mismatch"],
        "confidence_distribution": dict(confidence),
        "notes_distribution": dict(notes),
        "per_source_status": {slug: dict(c) for slug, c in per_source_status.items()},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sources",
        nargs="+",
        default=list(DEFAULT_SOURCES),
        help="Space-separated source slugs to backfill (default: NC SF + NC ND).",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit rows for smoke-testing.",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Concurrent geocoder workers (default: {DEFAULT_CONCURRENCY}).",
    )
    args = ap.parse_args()

    stats = run(
        sources=tuple(args.sources),
        limit=args.limit,
        concurrency=args.concurrency,
    )

    print("\n=== Geocoder backfill summary ===")
    print(f"  elapsed:           {stats['elapsed_sec']}s")
    print(f"  attempted:         {stats['attempted']:,}")
    print(f"  geocoded (success):{stats.get('geocoded', 0):,}")
    print(f"  failed (no match): {stats.get('failed', 0):,}")
    print(f"  state mismatch:    {stats.get('state_mismatch', 0):,}")
    print("\n=== Confidence distribution ===")
    for k, v in sorted((stats.get("confidence_distribution") or {}).items()):
        print(f"  {k:10s}  {v:>6,}")
    print("\n=== Notes distribution ===")
    for k, v in sorted((stats.get("notes_distribution") or {}).items()):
        print(f"  {k:30s}  {v:>6,}")
    print("\n=== Per-source confidence distribution ===")
    for slug, breakdown in (stats.get("per_source_status") or {}).items():
        print(f"  {slug}")
        for k, v in sorted(breakdown.items()):
            print(f"    {k:10s}  {v:>6,}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
