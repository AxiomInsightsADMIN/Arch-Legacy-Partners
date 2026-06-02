"""NC manual-drop data freshness gate.

Runs in the monthly refresh workflow between the NC scrapers and the
drift detector. Confirms that BOTH NC manual-drop sources had a
successful scraper_run within the last 7 days.

Why this gate exists:
  - The two NC DEQ DWM rosters are now fetched autonomously from edocs
    by the NC scraper steps (discover the current docid -> browser-
    session download; see scrapers.state._nc_edocs). edocs IS reachable
    from the US runner -- the historic "network block" was geographic,
    not a network-layer WAF.
  - This gate stays in place as a SAFETY NET while that automation
    proves out across >= 2 live cron cycles. The two NC scraper steps
    keep `continue-on-error: true` so a transient autonomous-fetch
    failure (or a manual-drop month) does not fail the whole workflow
    at those steps.
  - WITHOUT this gate, the workflow would happily run the resolver
    against stale NC data if BOTH the autonomous fetch and any manual
    drop were missing -- poisoning canonical_facility with a
    multi-month-old snapshot.

The gate checks freshness, not just presence:
  - SELECT s.slug, MAX(sr.finished_at)
      FROM scraper_run sr JOIN source s ...
     WHERE s.slug ∈ {nc_deq_solid_waste_facility_list,
                     nc_deq_septage_firm_list}
       AND sr.status = 'success'
       AND sr.finished_at > NOW() - INTERVAL '7 days'

Exit codes:
  0 — both NC sources have a recent successful run; downstream proceeds
  1 — one or both missing; resolver MUST NOT run against stale data

See `docs/runbook_monthly_refresh.md` §5 for the operator procedure
that resolves a failure here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scrapers._loader_utils import db_connect  # noqa: E402

NC_MANUAL_DROP_SLUGS: tuple[str, ...] = (
    "nc_deq_solid_waste_facility_list",
    "nc_deq_septage_firm_list",
)
FRESHNESS_WINDOW_DAYS = 7


def main() -> int:
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT s.slug, MAX(sr.finished_at) AS latest
          FROM scraper_run sr
          JOIN source s ON s.id = sr.source_id
         WHERE s.slug = ANY(%s)
           AND sr.status = 'success'
           AND sr.finished_at > NOW() - INTERVAL '{FRESHNESS_WINDOW_DAYS} days'
         GROUP BY s.slug
        """,
        (list(NC_MANUAL_DROP_SLUGS),),
    )
    rows = {slug: latest for slug, latest in cur.fetchall()}
    cur.close()
    conn.close()

    missing = [s for s in NC_MANUAL_DROP_SLUGS if s not in rows]
    if missing:
        msg = (
            f"ERROR: Manual drop required for one or both NC sources within "
            f"the last {FRESHNESS_WINDOW_DAYS} days.\n"
            f"  Missing successful scraper_run for: {', '.join(missing)}\n"
            f"  See docs/runbook_monthly_refresh.md section 5 for the "
            f"operator procedure.\n"
            f"  Re-trigger via workflow_dispatch after the manual drop "
            f"completes."
        )
        print(msg, file=sys.stderr, flush=True)
        return 1

    for slug, latest in rows.items():
        print(
            f"[verify-nc-manual-drop] {slug:40s} latest success at {latest}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
