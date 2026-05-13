"""Phase 4.5 step B — discovery URL harvester.

Walks every (category, state, template) tuple in
config/discovery_queries.yaml, executes the resolved Brave query on the
paid tier, harvests up to 10 URLs per query, and persists them into
discovered_url via per-row inserts with ON CONFLICT DO NOTHING. The
table's UNIQUE (url, source_category, state) constraint handles
persistent cross-query dedup; in-memory dedup within a single Brave
response prevents wasted INSERT attempts.

Initial row state on insert:
  fetch_status         = 'pending'   (schema default; URL not yet fetched)
  classified_relevance = NULL        (set later by step C extraction)
  content_hash         = NULL        (set later by step C after fetch)
  fetched_at           = NULL        (set later by step C after fetch)

Step C (scrapers/discovery/extraction.py) advances fetch_status from
'pending' to 'fetched' / 'failed' and fills the other fields.

Operational parameters (from Ryan's step-B brief):
  - Hard Brave cost cap at $5 (estimated as queries x $0.005). On
    breach, the loop exits cleanly and reports the partial state.
  - Heartbeat log line every HEARTBEAT_EVERY queries plus the first
    query and any error.
  - Checkpoint to local/_discovery_harvest_progress.json every
    CHECKPOINT_EVERY queries.
  - POLITE_DELAY_S between Brave calls (we are on paid tier; this
    delay is courtesy, not a free-tier requirement).
  - Per-row commit on INSERT so a mid-run kill loses at most one
    in-flight URL.
  - On Brave non-200 / transport error: log, increment error
    counter, continue. The harvest phase tolerates partial
    completion per the spec.

Run from the project root (.venv activated):
  .venv/Scripts/python.exe -m scrapers.discovery.discovery_crawl

Outputs:
  - discovered_url table populated (Supabase, durable artifact)
  - local/_discovery_harvest.log (process stdout, gitignored)
  - local/_discovery_harvest_progress.json (running checkpoint)
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from enrichment import _brave  # noqa: E402 (re-use the Brave client from Phase 4)
from scrapers._loader_utils import db_connect  # noqa: E402

# Constants ---------------------------------------------------------------
COST_CAP_USD = 5.0
HEARTBEAT_EVERY = 25
CHECKPOINT_EVERY = 50
POLITE_DELAY_S = 0.5
BRAVE_USD_PER_QUERY = 0.005  # paid-tier estimate ($5 per 1,000 queries)
RESULTS_PER_QUERY = 10

YAML_PATH = _PROJECT_ROOT / "config" / "discovery_queries.yaml"
PROGRESS_PATH = _PROJECT_ROOT / "local" / "_discovery_harvest_progress.json"


# --------------------------------------------------------------------------
@dataclass
class HarvestStats:
    """Per-query result counters for the running summary + checkpoint."""

    queries_executed: int = 0
    queries_errored: int = 0
    urls_returned_total: int = 0
    urls_inserted_total: int = 0
    urls_duplicate_total: int = 0  # ON CONFLICT no-ops at DB level
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)
    cumulative_cost_usd: float = 0.0

    def bump_category(self, category: str, key: str, n: int = 1) -> None:
        bucket = self.by_category.setdefault(
            category,
            {"queries": 0, "errored": 0, "returned": 0, "inserted": 0, "duplicates": 0},
        )
        bucket[key] = bucket.get(key, 0) + n


def load_query_tuples(yaml_path: Path) -> tuple[list[tuple[str, str, str, str]], dict]:
    """Parse the YAML and expand every (category, state) x template into a
    flat list of (category, state_abbr, state_name, query) tuples. Returns
    the tuples plus the raw doc for any metadata the caller needs."""
    doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    states_lookup: dict[str, str] = doc.get("states", {})
    tuples: list[tuple[str, str, str, str]] = []
    for cat_name, cat in doc["categories"].items():
        for state_abbr in cat["target_states"]:
            state_name = states_lookup.get(state_abbr, state_abbr)
            for template in cat["templates"]:
                query = template.replace("{state}", state_name)
                tuples.append((cat_name, state_abbr, state_name, query))
    return tuples, doc


def insert_url(cur, *, category: str, state: str, query: str, url: str) -> bool:
    """INSERT INTO discovered_url ... ON CONFLICT DO NOTHING.

    Returns True if the row was actually inserted, False if it was a
    duplicate that hit the (url, source_category, state) UNIQUE
    constraint. fetch_status defaults to 'pending' per the schema
    CHECK constraint; classified_relevance defaults to NULL.
    """
    cur.execute(
        """
        INSERT INTO discovered_url
            (source_category, state, query, url)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (url, source_category, state) DO NOTHING
        RETURNING id
        """,
        (category, state, query, url),
    )
    row = cur.fetchone()
    return row is not None


def write_checkpoint(stats: HarvestStats, *, total: int) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(
            {
                "stats": asdict(stats),
                "total_queries_planned": total,
                "remaining": total - stats.queries_executed,
                "checkpoint_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    tmp.replace(PROGRESS_PATH)


def main() -> int:
    print("[discovery] loading query templates from config/discovery_queries.yaml", flush=True)
    tuples, _doc = load_query_tuples(YAML_PATH)
    total = len(tuples)
    print(f"[discovery] {total} (category, state, template) tuples to execute", flush=True)
    print(f"[discovery] cost cap: ${COST_CAP_USD:.2f}", flush=True)
    print("[discovery] target table: discovered_url", flush=True)

    # Pre-flight category summary
    from collections import Counter

    by_cat_pre = Counter((c, s) for c, s, _, _ in tuples)
    print("[discovery] per-(category,state) query plan:", flush=True)
    for (cat, state), n in sorted(by_cat_pre.items()):
        print(f"  {cat:32s} {state:3s}  queries={n}", flush=True)

    conn = db_connect()
    stats = HarvestStats()
    started = time.time()
    hit_cap = False

    try:
        for i, (category, state, _state_name, query) in enumerate(tuples, 1):
            # Pre-cap check (each query costs ~BRAVE_USD_PER_QUERY)
            if stats.cumulative_cost_usd + BRAVE_USD_PER_QUERY > COST_CAP_USD:
                print(
                    f"\n[discovery] HARD STOP: next query would push cost "
                    f"${stats.cumulative_cost_usd:.4f} + "
                    f"${BRAVE_USD_PER_QUERY:.4f} above cap ${COST_CAP_USD:.2f}",
                    flush=True,
                )
                hit_cap = True
                break

            stats.queries_executed += 1
            stats.cumulative_cost_usd += BRAVE_USD_PER_QUERY
            stats.bump_category(category, "queries")

            # Execute Brave query
            try:
                results = _brave.search(query, count=RESULTS_PER_QUERY)
            except Exception as e:
                stats.queries_errored += 1
                stats.bump_category(category, "errored")
                stats.errors.append(
                    {
                        "i": i,
                        "category": category,
                        "state": state,
                        "query": query,
                        "error": f"{type(e).__name__}: {e}",
                    }
                )
                print(
                    f"[{i:>4}/{total}] ERROR  {category:30s} {state}  "
                    f"{type(e).__name__}: {str(e)[:80]}",
                    flush=True,
                )
                # Continue per spec
                if not stats.queries_errored % 10:  # progress save on every 10th error
                    write_checkpoint(stats, total=total)
                time.sleep(POLITE_DELAY_S)
                continue

            stats.urls_returned_total += len(results)
            stats.bump_category(category, "returned", len(results))

            # Per-row insert with cross-result dedup inside this response
            seen_urls_this_query: set[str] = set()
            row_cur = conn.cursor()
            try:
                inserted_this_query = 0
                duplicates_this_query = 0
                for r in results:
                    url = (r.get("url") or "").strip()
                    if not url or url in seen_urls_this_query:
                        continue
                    seen_urls_this_query.add(url)
                    was_inserted = insert_url(
                        row_cur, category=category, state=state, query=query, url=url
                    )
                    conn.commit()
                    if was_inserted:
                        inserted_this_query += 1
                    else:
                        duplicates_this_query += 1
                stats.urls_inserted_total += inserted_this_query
                stats.urls_duplicate_total += duplicates_this_query
                stats.bump_category(category, "inserted", inserted_this_query)
                stats.bump_category(category, "duplicates", duplicates_this_query)
            finally:
                row_cur.close()

            # Heartbeat
            if i % HEARTBEAT_EVERY == 0 or i == 1:
                print(
                    f"[{i:>4}/{total}] {category:30s} {state}  "
                    f"returned={len(results):>2}  inserted={inserted_this_query:>2}  "
                    f"dup={duplicates_this_query:>2}  "
                    f"cumcost=${stats.cumulative_cost_usd:.4f}",
                    flush=True,
                )

            # Checkpoint
            if i % CHECKPOINT_EVERY == 0:
                write_checkpoint(stats, total=total)

            time.sleep(POLITE_DELAY_S)
    finally:
        conn.close()

    # Final checkpoint write
    write_checkpoint(stats, total=total)
    elapsed = time.time() - started

    # ---- Summary ----
    print(flush=True)
    print(
        f"[discovery] processed {stats.queries_executed}/{total} queries in {elapsed / 60:.1f} min",
        flush=True,
    )
    print(f"  queries errored: {stats.queries_errored}", flush=True)
    print(f"  URLs returned:   {stats.urls_returned_total}", flush=True)
    print(f"  URLs inserted:   {stats.urls_inserted_total}", flush=True)
    print(f"  URLs duplicate:  {stats.urls_duplicate_total}", flush=True)
    if stats.urls_returned_total > 0:
        dedup_ratio = stats.urls_inserted_total / stats.urls_returned_total
        print(
            f"  dedup ratio:     {dedup_ratio:.3f} ({stats.urls_inserted_total} unique / {stats.urls_returned_total} returned)",
            flush=True,
        )
    print(
        f"  cost USD:        ${stats.cumulative_cost_usd:.4f}  (cap ${COST_CAP_USD:.2f})",
        flush=True,
    )
    if hit_cap:
        print("  [WARN] harvest stopped early due to cost cap.", flush=True)

    print(flush=True)
    print("[discovery] per-category result:", flush=True)
    for cat, counters in sorted(stats.by_category.items()):
        ratio_str = "n/a"
        if counters.get("returned", 0) > 0:
            ratio_str = f"{counters['inserted'] / counters['returned']:.3f}"
        print(
            f"  {cat:32s}  queries={counters.get('queries', 0):>3}  "
            f"errored={counters.get('errored', 0):>2}  "
            f"returned={counters.get('returned', 0):>4}  "
            f"inserted={counters.get('inserted', 0):>4}  "
            f"dup={counters.get('duplicates', 0):>3}  "
            f"dedup_ratio={ratio_str}",
            flush=True,
        )

    if stats.errors:
        print(flush=True)
        print(f"[discovery] error sample (first 10 of {len(stats.errors)}):", flush=True)
        for e in stats.errors[:10]:
            print(
                f"  i={e['i']:>4}  {e['category']:30s} {e['state']}  {e['error'][:120]}", flush=True
            )

    return 0 if not hit_cap else 2


if __name__ == "__main__":
    raise SystemExit(main())
