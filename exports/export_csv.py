"""CSV export for the Arch Legacy Partners wastewater facility database.

Produces two files per the locked architectural decision (kickoff brief
section 8.4):

  1. exports/facilities_primary.csv  — one row per canonical_facility
     where facility_type IS NOT NULL (excludes the ~70K ECHO industrial
     NPDES rows that don't map to any of the 7 v1 categories). This is
     the consumer-facing file.

  2. exports/facilities_provenance.csv  — long format, one row per
     (canonical_facility_id, field_name, observed_at) for every
     populated field on a canonical that appears in the primary file.
     Join key is canonical_facility_id (= primary.id). Carries
     source_url, source_date, extraction_method, confidence so a
     consumer can audit any value back to the source it came from.

Usage:
  python -m exports.export_csv                 # default exports/ dir
  python -m exports.export_csv --output-dir /tmp/out

Re-runs are idempotent — the CSV writer overwrites both files and the
canonical state is the authoritative source. The CSVs themselves are
gitignored (`exports/*.csv` is in .gitignore); the monthly refresh
workflow force-adds them on a dated `refresh/YYYY-MM-DD` branch.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

# Project-root path shim so this runs via `python -m exports.export_csv`
# and via `python exports/export_csv.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scrapers._loader_utils import db_connect  # noqa: E402

PRIMARY_FILENAME = "facilities_primary.csv"
PROVENANCE_FILENAME = "facilities_provenance.csv"

PRIMARY_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "facility_type",
    "street",
    "city",
    "state",
    "zip",
    "county",
    "latitude",
    "longitude",
    "accepts_septage",
    "accepts_grease_trap",
    "accepts_portable_toilet",
    "pricing_notes",
    "phone",
    "email",
    "website",
    "frs_id",
    "npdes_id",
    "state_permit_id",
    "first_seen_at",
    "last_seen_at",
)

PROVENANCE_COLUMNS: tuple[str, ...] = (
    "canonical_facility_id",
    "field_name",
    "value",
    "source_url",
    "source_date",
    "extraction_method",
    "confidence",
    "observed_at",
)

PRIMARY_SQL = """
    SELECT id, name, facility_type, street, city, state, zip, county,
           latitude, longitude,
           accepts_septage, accepts_grease_trap, accepts_portable_toilet,
           pricing_notes, phone, email, website,
           frs_id, npdes_id, state_permit_id,
           first_seen_at, last_seen_at
      FROM canonical_facility
     WHERE facility_type IS NOT NULL
     ORDER BY id
"""

# Provenance rows are scoped to canonicals that appear in primary, so the
# join key works end-to-end without orphans.
PROVENANCE_SQL = """
    SELECT fp.canonical_facility_id, fp.field_name, fp.value,
           fp.source_url, fp.source_date, fp.extraction_method,
           fp.confidence, fp.observed_at
      FROM field_provenance fp
      JOIN canonical_facility cf ON cf.id = fp.canonical_facility_id
     WHERE cf.facility_type IS NOT NULL
     ORDER BY fp.canonical_facility_id, fp.field_name, fp.observed_at
"""


def _write_csv(cur, sql: str, columns: tuple[str, ...], out_path: Path) -> int:
    """Stream-write a CSV. Returns row count. Uses `cur.fetchmany` to
    avoid loading the whole result set into memory (provenance is ~30K
    rows for the current typed subset but will grow with Phase 4)."""
    cur.execute(sql)
    written = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        while True:
            batch = cur.fetchmany(5000)
            if not batch:
                break
            for row in batch:
                writer.writerow(row)
                written += 1
    return written


def run(output_dir: Path) -> dict:
    started = time.time()
    print(f"[export-csv] output_dir={output_dir}", flush=True)

    conn = db_connect()
    cur = conn.cursor()

    primary_path = output_dir / PRIMARY_FILENAME
    primary_rows = _write_csv(cur, PRIMARY_SQL, PRIMARY_COLUMNS, primary_path)
    primary_size = primary_path.stat().st_size
    print(
        f"[export-csv]   {PRIMARY_FILENAME:32s} rows={primary_rows:>8,}  bytes={primary_size:>12,}",
        flush=True,
    )

    provenance_path = output_dir / PROVENANCE_FILENAME
    provenance_rows = _write_csv(cur, PROVENANCE_SQL, PROVENANCE_COLUMNS, provenance_path)
    provenance_size = provenance_path.stat().st_size
    print(
        f"[export-csv]   {PROVENANCE_FILENAME:32s} "
        f"rows={provenance_rows:>8,}  bytes={provenance_size:>12,}",
        flush=True,
    )

    # Spot-check three facilities by canonical_facility_id. Picks one TX,
    # one NC, and one cross-source (>1 raw linked) so the consumer can
    # verify the join key end-to-end against the most interesting cases.
    print("\n[export-csv] spot-check (3 facilities, join key = canonical_facility_id)")
    cur.execute("""
        SELECT cf.id, cf.name, cf.facility_type, cf.state, cf.city,
               (SELECT COUNT(*) FROM facility_record_link l WHERE l.canonical_facility_id = cf.id) AS n_raws,
               (SELECT COUNT(*) FROM field_provenance fp WHERE fp.canonical_facility_id = cf.id) AS n_prov
          FROM canonical_facility cf
         WHERE cf.facility_type IS NOT NULL
           AND cf.state IS NOT NULL
         ORDER BY n_raws DESC NULLS LAST, cf.state
         LIMIT 3
    """)
    samples = cur.fetchall()
    for s in samples:
        print(
            f"  id={s[0][:8]}...  state={s[3]:>2}  "
            f"name={(s[1] or '')[:35]!r:37}  type={s[2]!r:36s}  "
            f"raws={s[5]:>3}  prov={s[6]:>3}",
            flush=True,
        )

    cur.close()
    conn.close()
    elapsed = time.time() - started
    return {
        "elapsed_sec": round(elapsed, 1),
        "primary_path": str(primary_path),
        "primary_rows": primary_rows,
        "primary_bytes": primary_size,
        "provenance_path": str(provenance_path),
        "provenance_rows": provenance_rows,
        "provenance_bytes": provenance_size,
        "spot_check_samples": [
            {"id": s[0], "name": s[1], "facility_type": s[2], "state": s[3]} for s in samples
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=_PROJECT_ROOT / "exports",
        help="Directory to write the two CSV files (default: <repo>/exports/).",
    )
    args = ap.parse_args()

    stats = run(args.output_dir)
    print("\n=== Export summary ===")
    print(f"  elapsed:            {stats['elapsed_sec']}s")
    print(f"  primary rows:       {stats['primary_rows']:,}")
    print(f"  primary bytes:      {stats['primary_bytes']:,}")
    print(f"  provenance rows:    {stats['provenance_rows']:,}")
    print(f"  provenance bytes:   {stats['provenance_bytes']:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
