"""Phase 3 entity resolver — main orchestration.

Walks every raw_facility_record row in source order, applies the
exclusion filters, attempts ID-first matching, falls back to a
RapidFuzz score-based match with proximity tiebreak, and writes
canonical_facility / facility_record_link / field_provenance rows.

Usage:
    python -m resolver.entity_resolver           # full pass
    python -m resolver.entity_resolver --dry-run # plan only, no writes

Locked source-processing order (the field merge policy is 'first non-null
wins'):

  1. epa_cwns_2022                       (POTW spine — most complete identity)
  2. epa_echo                            (collapses to CWNS via NPDES)
  3. tceq_msw_facilities_xls
  4. nc_deq_non_discharge_facilities
  5. nc_deq_solid_waste_facility_list
  6. nc_deq_septage_firm_list

Stop-and-report at the end: dumps counts per source, per filter, per
match decision, and a hold-for-review summary so Ryan can verify before
authorizing any cleanup or re-runs.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

# Project-root path shim so this runs via `python -m resolver.entity_resolver`
# and via `python resolver/entity_resolver.py`.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from orchestration.geocoder import _address_hash, _normalize_address  # noqa: E402
from resolver._canonicalize import (  # noqa: E402
    CanonicalRowState,
    PendingLink,
    derive_state_permit_id,
    flush_canonicals,
    flush_links,
    flush_provenance,
    make_provenance_rows,
    new_canonical_id,
)
from resolver._category_map import map_to_canonical  # noqa: E402
from resolver._filters import EXCLUSION_REASONS, apply_filters  # noqa: E402
from resolver._id_match import IdRegistry  # noqa: E402
from resolver._normalize import normalize, synthesize_address_for_geocoding  # noqa: E402
from resolver._residential_filter import check_residential  # noqa: E402
from resolver._score_match import (  # noqa: E402
    CandidateCanonical,
    CanonicalIndex,
    find_best_match,
)
from scrapers._loader_utils import db_connect  # noqa: E402

# Locked source-processing order.
SOURCE_ORDER: tuple[str, ...] = (
    "epa_cwns_2022",
    "epa_echo",
    "tceq_msw_facilities_xls",
    "nc_deq_non_discharge_facilities",
    "nc_deq_solid_waste_facility_list",
    "nc_deq_septage_firm_list",
)

BATCH_SIZE = 1000  # flush canonical/link/provenance batches every N raws
PRINT_EVERY = 5000  # progress heartbeat


def _load_raw_chunk(cur, source_slug: str, chunk_size: int = 5000):
    """Yield batches of (raw_id, source_record_id, raw_payload) for one source.
    Ordered by raw_facility_record.id for deterministic processing."""
    cur.execute(
        """
        SELECT r.id, r.source_record_id, r.raw_payload
          FROM raw_facility_record r
          JOIN source s ON s.id = r.source_id
         WHERE s.slug = %s
         ORDER BY r.id
        """,
        (source_slug,),
    )
    while True:
        rows = cur.fetchmany(chunk_size)
        if not rows:
            return
        yield rows


REBUILD_SQL = """
TRUNCATE canonical_facility,
         facility_record_link,
         field_provenance,
         canonical_facility_history
   CASCADE;
"""


def _load_geocoding_cache(cur) -> dict[str, tuple[float, float, str]]:
    """Pre-load `geocoding_cache` into a dict keyed by address_hash.

    Filters to confidence='high' only. 'low' confidence entries are
    state-mismatch coords (the geocoder returned a location outside the
    facility's state envelope) and would produce false-positive proximity
    matches against the wrong place. 'medium' confidence entries
    (state_bounds_missing) don't occur for the NC backfill because the
    NC envelope is defined; if a future state ships without an envelope,
    extend STATE_BOUNDS first.
    The resolver consults this dict at normalize-time for any raw that
    has no native coords but does have a synthesizeable address.
    """
    cur.execute(
        """
        SELECT address_hash, lat, lng, confidence
          FROM geocoding_cache
         WHERE lat IS NOT NULL AND lng IS NOT NULL
           AND confidence = 'high'
        """
    )
    out: dict[str, tuple[float, float, str]] = {}
    for ahash, lat, lng, conf in cur.fetchall():
        out[ahash] = (float(lat), float(lng), conf)
    return out


def _enrich_coords_from_cache(raw, geocoding_lookup: dict) -> bool:
    """If raw has no native coords and a synthesizeable address, fill in
    coords from the geocoding cache. Returns True if coords were filled."""
    if raw.latitude is not None and raw.longitude is not None:
        return False
    addr = synthesize_address_for_geocoding(raw)
    if not addr:
        return False
    ahash = _address_hash(_normalize_address(addr))
    hit = geocoding_lookup.get(ahash)
    if hit is None:
        return False
    lat, lng, _conf = hit
    raw.latitude = lat
    raw.longitude = lng
    return True


def _rebuild_truncate(conn) -> dict[str, int]:
    """Truncate the four resolver-owned tables. Returns row counts BEFORE
    truncation so the caller can log what was wiped."""
    cur = conn.cursor()
    counts = {}
    for tbl in (
        "canonical_facility",
        "facility_record_link",
        "field_provenance",
        "canonical_facility_history",
    ):
        cur.execute(f"SELECT COUNT(*) FROM {tbl}")
        counts[tbl] = cur.fetchone()[0]
    cur.execute(REBUILD_SQL)
    conn.commit()
    cur.close()
    return counts


def run(*, dry_run: bool = False, rebuild: bool = False) -> dict:
    """Main entrypoint. Returns a stats dict for the report.

    Args:
        dry_run: when True, the pipeline runs in plan-only mode (no writes
                 to canonical_facility / facility_record_link / field_provenance).
        rebuild: when True, TRUNCATE the four resolver-owned tables before
                 resolving. Caller is responsible for the --force gate; this
                 function trusts the flag.
    """
    started = time.time()
    print(
        f"[resolver] starting full pass (dry_run={dry_run}, rebuild={rebuild})",
        flush=True,
    )

    conn = db_connect()
    cur = conn.cursor()
    write_cur = conn.cursor()

    if rebuild and not dry_run:
        print("[resolver] --rebuild requested; truncating four tables...", flush=True)
        wiped = _rebuild_truncate(conn)
        for tbl, n in wiped.items():
            print(f"[resolver]   wiped  {tbl:30s} {n:>10,} rows", flush=True)
    elif rebuild and dry_run:
        print(
            "[resolver] --rebuild is a no-op under --dry-run (no writes happen anyway)",
            flush=True,
        )

    # Pre-load geocoding_cache so we can enrich coord-less raws (NC ND, NC SF)
    # at normalize-time. Lookup is O(1) on address_hash.
    geocoding_lookup = _load_geocoding_cache(cur)
    print(
        f"[resolver] loaded {len(geocoding_lookup):,} entries from geocoding_cache",
        flush=True,
    )

    canonical_index = CanonicalIndex()
    id_registry = IdRegistry()
    canonical_state: dict[str, CanonicalRowState] = {}

    dirty_canonical_ids: set[str] = set()
    pending_links: list[PendingLink] = []
    pending_prov: list = []

    stats: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    flush_count = 0

    def maybe_flush(force: bool = False) -> None:
        nonlocal flush_count
        if dry_run:
            dirty_canonical_ids.clear()
            pending_links.clear()
            pending_prov.clear()
            return
        if not force and len(pending_links) < BATCH_SIZE:
            return
        # Materialize dirty canonical rows from in-memory state.
        canonicals_to_write = [canonical_state[cid] for cid in dirty_canonical_ids]
        # Order matters: canonicals first (FK targets), then links + provenance.
        flush_canonicals(write_cur, canonicals_to_write)
        flush_links(write_cur, pending_links)
        flush_provenance(write_cur, pending_prov)
        conn.commit()
        flush_count += 1
        dirty_canonical_ids.clear()
        pending_links.clear()
        pending_prov.clear()

    total_seen = 0
    for source_slug in SOURCE_ORDER:
        print(f"[resolver] === {source_slug} ===", flush=True)
        source_seen = 0
        for chunk in _load_raw_chunk(cur, source_slug):
            for raw_id, source_record_id, raw_payload in chunk:
                total_seen += 1
                source_seen += 1
                stats[source_slug]["raws_seen"] += 1

                raw = normalize(
                    raw_id=raw_id,
                    source_slug=source_slug,
                    source_record_id=source_record_id,
                    raw_payload=raw_payload,
                )

                # Enrich missing coords from geocoding_cache (no-op when raw
                # already has native coords or no address is synthesizeable).
                if _enrich_coords_from_cache(raw, geocoding_lookup):
                    stats[source_slug]["coords_from_geocoder_cache"] += 1

                filt = apply_filters(raw)
                if not filt.keep:
                    stats[source_slug][f"excluded_{filt.reason}"] += 1
                    continue

                cat = map_to_canonical(
                    source_slug=source_slug,
                    raw_type=raw.raw_facility_type_string,
                )

                # 1) ID-first match
                id_hit = id_registry.lookup(raw)
                if id_hit.conflict:
                    stats[source_slug]["id_first_conflict"] += 1

                if id_hit.canonical_id is not None:
                    canonical_id = id_hit.canonical_id
                    state = canonical_state[canonical_id]
                    state.merge_first_non_null(raw, state_permit=derive_state_permit_id(raw))
                    # Also stamp facility_type if not set
                    if state.facility_type is None and cat.canonical_type:
                        state.facility_type = cat.canonical_type
                        state.dirty = True
                    # Refresh score-based index entry in case city/state filled in
                    cand = canonical_index.get(canonical_id)
                    if cand is not None:
                        cand.name = state.name
                        cand.city = state.city
                        cand.state = state.state
                        cand.latitude = state.latitude
                        cand.longitude = state.longitude
                    match_method = "id_match"
                    match_score = None
                    stats[source_slug][f"id_match_{id_hit.matched_field}"] += 1
                else:
                    # 2a) Residential-address-pattern filter (Phase 4 follow-on).
                    # For NC ND rows whose FACILITY name matches the SFR
                    # pattern, bypass the RapidFuzz score-based match entirely.
                    # The raw still passed the ID-first lookup above; standalone
                    # canonical creation is the correct outcome for residential
                    # permits (they should NOT merge with NC SF septage
                    # businesses or ECHO industrial NPDES rows at the same
                    # address). See resolver/_residential_filter.py for the
                    # pattern + decision rules.
                    sfr = check_residential(raw)
                    sfr_matched_this_raw = sfr.matched
                    if sfr.matched:
                        if sfr.high_confidence:
                            stats[source_slug]["residential_filter_excluded"] += 1
                        else:
                            stats[source_slug]["residential_filter_review"] += 1
                        # Synthetic "no merge" outcome to flow into the new-
                        # canonical branch below without running score-based.
                        score_result = None
                    else:
                        # 2b) Score-based match
                        score_result = find_best_match(raw=raw, index=canonical_index)

                    if score_result is not None and score_result.canonical_id is not None:
                        canonical_id = score_result.canonical_id
                        state = canonical_state[canonical_id]
                        state.merge_first_non_null(raw, state_permit=derive_state_permit_id(raw))
                        if state.facility_type is None and cat.canonical_type:
                            state.facility_type = cat.canonical_type
                            state.dirty = True
                        cand = canonical_index.get(canonical_id)
                        if cand is not None:
                            cand.name = state.name
                            cand.city = state.city
                            cand.state = state.state
                            cand.latitude = state.latitude
                            cand.longitude = state.longitude
                        match_method = "rapidfuzz"
                        match_score = score_result.score
                        stats[source_slug][f"score_{score_result.decision}"] += 1
                    else:
                        # 3) New canonical
                        canonical_id = new_canonical_id()
                        state = CanonicalRowState(canonical_id=canonical_id)
                        state.merge_first_non_null(raw, state_permit=derive_state_permit_id(raw))
                        # Phase 4 follow-on: SFR residential permits land as
                        # NULL facility_type. Residential wastewater-irrigation
                        # systems are not hauler-disposal sites — they should
                        # not appear in v_nc_private_regional_septage_facility
                        # or v_all_in_scope. Raw payload is still preserved on
                        # raw_facility_record; the canonical type is the
                        # consumer-facing "this is hauler-relevant" assertion,
                        # which residential systems fail. Same NULL-out-of-scope
                        # pattern we use for the ~70K ECHO industrial NPDES rows.
                        # See docs/build_log.md Phase 4 design pin for the
                        # recorded correction on this mechanic.
                        if cat.canonical_type and not sfr_matched_this_raw:
                            state.facility_type = cat.canonical_type
                            state.dirty = True
                        canonical_state[canonical_id] = state
                        canonical_index.add(
                            CandidateCanonical(
                                canonical_id=canonical_id,
                                name=state.name,
                                city=state.city,
                                state=state.state,
                                latitude=state.latitude,
                                longitude=state.longitude,
                            )
                        )
                        # Always 'rapidfuzz' for non-ID matches per the CHECK
                        # constraint allowlist. Score is non-null if we actually
                        # ran a comparison; null if the bucket was empty OR the
                        # SFR filter bypassed score-based matching entirely.
                        match_method = "rapidfuzz"
                        if score_result is None:
                            # SFR filter bypassed score-based matching. Stats
                            # already incremented under residential_filter_*
                            # above; nothing to add here.
                            match_score = None
                        else:
                            match_score = score_result.score if score_result.score > 0 else None
                            if score_result.decision == "hold":
                                stats[source_slug]["score_hold_new_canonical"] += 1
                            else:
                                stats[source_slug]["new_canonical"] += 1

                # Register IDs against the canonical (idempotent)
                id_registry.register(raw, canonical_id)

                # Pending link
                pending_links.append(
                    PendingLink(
                        raw_facility_record_id=raw_id,
                        canonical_facility_id=canonical_id,
                        match_score=match_score,
                        match_method=match_method,
                    )
                )

                # Pending provenance — one row per populated raw field
                pending_prov.extend(
                    make_provenance_rows(
                        canonical_id=canonical_id,
                        raw=raw,
                        raw_source_url=None,
                        raw_source_date=None,
                        category_confidence=cat.confidence,
                    )
                )

                # Track this canonical for flush if it's dirty
                if state.dirty:
                    dirty_canonical_ids.add(canonical_id)

                if total_seen % PRINT_EVERY == 0:
                    print(
                        f"[resolver]   seen={total_seen:>7,}  "
                        f"canonicals={len(canonical_state):>6,}  "
                        f"flushed_batches={flush_count}",
                        flush=True,
                    )

                maybe_flush(force=False)
        print(
            f"[resolver]   {source_slug:38s} done: {source_seen:,} raws processed",
            flush=True,
        )

    # Final flush
    maybe_flush(force=True)

    elapsed = time.time() - started
    print(
        f"[resolver] complete: {total_seen:,} raws -> "
        f"{len(canonical_state):,} canonicals in {elapsed:.1f}s",
        flush=True,
    )

    cur.close()
    write_cur.close()
    conn.close()

    # Aggregate stats for the report
    return {
        "elapsed_sec": round(elapsed, 1),
        "total_raws_seen": total_seen,
        "total_canonicals": len(canonical_state),
        "id_registry_sizes": id_registry.size(),
        "per_source": {k: dict(v) for k, v in stats.items()},
        "flush_batches": flush_count,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan only; do not write canonical_facility / facility_record_link / field_provenance.",
    )
    ap.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Truncate canonical_facility, facility_record_link, field_provenance, "
            "and canonical_facility_history before resolving. Required for "
            "idempotent re-runs (the resolver mints fresh UUIDs each pass)."
        ),
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help=(
            "Required confirmation when --rebuild is set. Without --force, "
            "--rebuild errors out to prevent accidental data wipes."
        ),
    )
    args = ap.parse_args()

    if args.rebuild and not args.force:
        print(
            "ERROR: --rebuild truncates canonical_facility, facility_record_link, "
            "field_provenance, and canonical_facility_history.\n"
            "       Pass --force to confirm. Without --force, the resolver refuses "
            "to wipe data.\n"
            "       Example:  python -m resolver.entity_resolver --rebuild --force",
            file=sys.stderr,
        )
        return 2

    stats = run(dry_run=args.dry_run, rebuild=args.rebuild)

    print("\n=== Run summary ===")
    print(f"  elapsed:        {stats['elapsed_sec']}s")
    print(f"  raws seen:      {stats['total_raws_seen']:,}")
    print(f"  canonicals:     {stats['total_canonicals']:,}")
    print(f"  flush batches:  {stats['flush_batches']}")
    print(f"  ID registry sizes: {stats['id_registry_sizes']}")
    print("\n=== Per-source breakdown ===")
    for slug, breakdown in stats["per_source"].items():
        print(f"  {slug}")
        for k, v in sorted(breakdown.items()):
            print(f"    {k:40s}  {v:>8,}")

    print("\n=== Exclusion reason legend ===")
    for k, v in EXCLUSION_REASONS.items():
        print(f"  {k:30s}  {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
