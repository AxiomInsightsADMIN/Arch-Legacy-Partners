"""Carry acceptance-flag verdicts forward across a resolver rebuild.

Why this exists: `resolver.entity_resolver --rebuild --force` TRUNCATEs
`canonical_facility` and regenerates every row with a fresh uuid4. The
Phase 4 `accepts_*` verdicts live on those rows, and the
`llm_enrichment_cache` content hash embeds the (now dead) facility id,
so the cache cannot restore them after a rebuild. The 2026-06 cron
shipped a CSV with blank `accepts_*` columns because of exactly this
(see build log 2026-06-13).

Strategy: the previous month's committed `exports/facilities_primary.csv`
(present in the workflow checkout BEFORE the new export overwrites it)
carries the last-known verdicts alongside name/state/facility_type.
Re-apply them to the freshly rebuilt canonical rows by exact
(name, state, facility_type) match.

Rules:
  - Only rows whose three accepts_* columns are ALL NULL are updated —
    carry-forward never clobbers a fresher verdict.
  - Keys that are ambiguous on either side (duplicate name+state+type
    with conflicting verdicts) are skipped and reported.
  - Exits 1 if the source CSV contains no verdicts at all: that is the
    blank-flags regression this script exists to prevent, and a silent
    pass would defeat the point.

Idempotent: re-running converges to the same state.

Run in the monthly workflow between the resolver rebuild and the CSV
export. Manual invocation: `python -m enrichment.carry_forward_accepts
[path/to/prior_primary.csv]`.
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

from psycopg2.extras import execute_values

from scrapers._loader_utils import db_connect

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "exports" / "facilities_primary.csv"
VALID = ("Yes", "No", "Unknown")
ACCEPT_COLS = ("accepts_septage", "accepts_grease_trap", "accepts_portable_toilet")


def _load_prior_verdicts(csv_path: Path) -> dict[tuple[str, str, str], tuple[str, str, str]]:
    """Map (name, state, facility_type) -> (septage, grease, portable).

    Keys with conflicting duplicate verdicts in the CSV are dropped.
    Rows with no verdict in any of the three columns are ignored.
    """
    groups: dict[tuple[str, str, str], set[tuple[str, str, str]]] = defaultdict(set)
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            verdict = tuple(row.get(c, "").strip() for c in ACCEPT_COLS)
            if not any(verdict):
                continue
            if not all(v in VALID for v in verdict):
                continue
            key = (row["name"], row["state"], row["facility_type"])
            groups[key].add(verdict)  # type: ignore[arg-type]
    out: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    dropped = 0
    for key, verdicts in groups.items():
        if len(verdicts) == 1:
            out[key] = next(iter(verdicts))
        else:
            dropped += 1
    if dropped:
        print(f"[carry-forward] dropped {dropped} CSV keys with conflicting duplicate verdicts")
    return out


def main() -> int:
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.exists():
        print(f"[carry-forward] no prior CSV at {csv_path}; nothing to carry forward")
        return 0

    prior = _load_prior_verdicts(csv_path)
    if not prior:
        print(
            f"[carry-forward] ERROR: {csv_path} contains zero acceptance verdicts. "
            "The previous export shipped blank accepts_* columns — investigate "
            "before letting this refresh proceed (build log 2026-06-13)."
        )
        return 1
    print(f"[carry-forward] {len(prior)} prior verdict keys loaded from {csv_path.name}")

    conn = db_connect()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, name, state, facility_type
              FROM canonical_facility
             WHERE facility_type IS NOT NULL
               AND accepts_septage IS NULL
               AND accepts_grease_trap IS NULL
               AND accepts_portable_toilet IS NULL
            """
        )
        rows = cur.fetchall()
        updates: list[tuple[str, str, str, str]] = []
        unmatched = 0
        for fid, name, state, ftype in rows:
            verdict = prior.get((name or "", state or "", ftype or ""))
            if verdict is None:
                unmatched += 1
                continue
            updates.append((*verdict, str(fid)))

        print(
            f"[carry-forward] {len(rows)} rebuilt rows with NULL flags; "
            f"{len(updates)} matched a prior verdict; {unmatched} unmatched "
            "(new or renamed facilities — candidates for fresh enrichment)"
        )
        if updates:
            execute_values(
                cur,
                """
                UPDATE canonical_facility AS c
                   SET accepts_septage         = v.s,
                       accepts_grease_trap     = v.g,
                       accepts_portable_toilet = v.p
                  FROM (VALUES %s) AS v(s, g, p, id)
                 WHERE c.id = v.id::uuid
                """,
                updates,
                page_size=2000,
            )
            conn.commit()
            print(f"[carry-forward] committed {len(updates)} canonical updates")
    finally:
        cur.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
