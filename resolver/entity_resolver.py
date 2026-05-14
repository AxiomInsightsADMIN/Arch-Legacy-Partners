"""Phase 3 entity resolver — main orchestration.

Walks every raw_facility_record row in source order, applies the
exclusion filters, attempts ID-first matching, falls back to a
RapidFuzz score-based match with proximity tiebreak, and writes
canonical_facility / facility_record_link / field_provenance rows.

Usage:
    python -m resolver.entity_resolver           # full pass
    python -m resolver.entity_resolver --dry-run # plan only, no writes
    python -m resolver.entity_resolver --candidate-import   # Phase 4.5 step D

Locked source-processing order (the field merge policy is 'first non-null
wins'):

  1. epa_cwns_2022                       (POTW spine — most complete identity)
  2. epa_echo                            (collapses to CWNS via NPDES)
  3. tceq_msw_facilities_xls
  4. nc_deq_non_discharge_facilities
  5. nc_deq_solid_waste_facility_list
  6. nc_deq_septage_firm_list

The Phase 4.5 step D `--candidate-import` flag triggers a SECOND
entrypoint that walks `discovery_candidate_facility` rows where
`review_status='pending'`, imports each via the same matching primitives
the main resolver uses (IdRegistry-equivalent + RapidFuzz `find_best_match`
score), and writes synthetic raw_facility_record rows under
`source='discovery_crawl'`. Three outcomes per candidate:
  - existing-match (ID or score >=92): write field_provenance, update
    last_seen_at, NO modification of the existing canonical's core fields.
  - net-new (no match below 75): insert canonical + synthetic raw + link
    + queue row.
  - hold-borderline (75-91, no proximity tiebreak): insert canonical
    flagged via discovery_review_queue with hold_reason='borderline_match'.

Stop-and-report at the end of each pass: dumps counts per source, per
filter, per match decision, and a hold-for-review summary so Ryan can
verify before authorizing any cleanup or re-runs.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

from rapidfuzz import fuzz

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
    AUTO_MERGE_THRESHOLD,
    HOLD_FOR_REVIEW_THRESHOLD,
    PROXIMITY_TIEBREAK_METERS,
    CandidateCanonical,
    CanonicalIndex,
    _haversine_m,
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


# =============================================================================
# Phase 4.5 step D — discovery candidate import path.
#
# Imports rows from discovery_candidate_facility WHERE review_status='pending'
# into the canonical facility set via the same matching primitives the main
# resolver uses (IdRegistry-style ID-first lookup, RapidFuzz WRatio score
# match, 200m haversine proximity tiebreak). Three outcomes per candidate:
#   existing-match  — write provenance, bump last_seen_at, NO core-field
#                     modification (the existing canonical stays as-is).
#   net-new         — insert canonical_facility + synthetic raw + link +
#                     discovery_review_queue row (review_status='pending').
#   hold-borderline — same as net-new plus discovery_review_queue row
#                     carrying hold_reason='borderline_match' pointing to
#                     the closest existing canonical.
#
# Cross-bucket dedup is automatic: as net-new canonicals are inserted into
# the in-memory index, subsequent candidates with the same name+state+type
# RapidFuzz-match against them. The Walnut Creek WWTP test case (same
# facility extracted under both county_manhole_program and
# tx_private_regional_septage) collapses to one canonical via this path.
# =============================================================================

# Regex patterns for extracting permit-style identifiers from candidate
# raw_payload strings. Order = precedence (NPDES wins over state permits).
_DISCOVERY_PERMIT_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    # NPDES e.g. NC0021123 / TX0080896 — 2 USPS letters + 0 + 6 digits
    ("npdes_id", re.compile(r"\b(?:NC|TX)0\d{6}\b")),
    # FRS Registry ID — 12 digits, EPA's national identifier
    ("frs_id", re.compile(r"\b1100\d{8}\b")),
    # WQ permits — used by both NC ND (7 digits) and TX TCEQ TLAP (up to
    # 10 digits, e.g. WQ0005522000). Routed via state_permit_id lookup
    # downstream so state context resolves the format ambiguity.
    ("state_permit_id_wq", re.compile(r"\bWQ\d{4,10}\b", re.IGNORECASE)),
)


def _stringify_payload_for_id_scan(payload: dict) -> str:
    """Concatenate all string-ish values in a candidate payload for regex
    scanning. Walks JSON one level deep; lists/dicts are JSON-dumped."""
    parts: list[str] = []
    for v in payload.values():
        if v is None:
            continue
        if isinstance(v, str):
            parts.append(v)
        else:
            parts.append(json.dumps(v, default=str))
    return "\n".join(parts)


def _extract_ids_from_candidate(payload: dict) -> dict[str, str]:
    """Run the permit-pattern regex set over the candidate payload. Returns
    a dict mapping id_field -> normalized value (uppercased, spaces stripped)."""
    blob = _stringify_payload_for_id_scan(payload)
    found: dict[str, str] = {}
    for field_name, pattern in _DISCOVERY_PERMIT_PATTERNS:
        m = pattern.search(blob)
        if not m:
            continue
        found[field_name] = m.group(0).upper().replace(" ", "")
    return found


def _normalize_state_to_usps(value: str | None, fallback: str) -> str:
    """Coerce a candidate's state string to USPS 2-letter code."""
    s = (value or "").strip().upper()
    if s in ("TX", "NC"):
        return s
    if s == "TEXAS":
        return "TX"
    if s == "NORTH CAROLINA":
        return "NC"
    return fallback


def _preload_canonical_typed_index(conn) -> dict[tuple[str, str], list[CandidateCanonical]]:
    """Load every typed canonical from canonical_facility into a
    (state, facility_type) -> list[CandidateCanonical] bucket map."""
    cur = conn.cursor()
    out: dict[tuple[str, str], list[CandidateCanonical]] = {}
    try:
        cur.execute(
            """
            SELECT id::text, name, city, state, latitude, longitude, facility_type
              FROM canonical_facility
             WHERE facility_type IS NOT NULL
            """
        )
        for cid, name, city, state, lat, lng, ftype in cur.fetchall():
            key = (state or "", ftype or "")
            out.setdefault(key, []).append(
                CandidateCanonical(
                    canonical_id=cid,
                    name=name,
                    city=city,
                    state=state,
                    latitude=float(lat) if lat is not None else None,
                    longitude=float(lng) if lng is not None else None,
                )
            )
    finally:
        cur.close()
    return out


def _preload_canonical_id_index(conn) -> dict[str, dict[str, str]]:
    """Load canonical_facility identifier columns into in-memory lookup
    dicts. Returns {'npdes': {...}, 'frs': {...}, 'state_permit': {...}}."""
    cur = conn.cursor()
    out: dict[str, dict[str, str]] = {"npdes": {}, "frs": {}, "state_permit": {}}
    try:
        cur.execute(
            """
            SELECT id::text, npdes_id, frs_id, state_permit_id
              FROM canonical_facility
            """
        )
        for cid, npdes_id, frs_id, sp_id in cur.fetchall():
            if npdes_id:
                out["npdes"][npdes_id.strip().upper()] = cid
            if frs_id:
                out["frs"][frs_id.strip()] = cid
            if sp_id:
                out["state_permit"][sp_id.strip().upper()] = cid
    finally:
        cur.close()
    return out


def _candidate_id_match(
    extracted_ids: dict[str, str], id_index: dict[str, dict[str, str]]
) -> tuple[str | None, str | None]:
    """Look up the candidate's regex-extracted IDs against the canonical
    ID lookup. Precedence: NPDES > FRS > state_permit. Returns
    (canonical_id, matched_field) on hit, or (None, None)."""
    nid = extracted_ids.get("npdes_id")
    if nid and nid in id_index["npdes"]:
        return id_index["npdes"][nid], "npdes_id"
    fid = extracted_ids.get("frs_id")
    if fid and fid in id_index["frs"]:
        return id_index["frs"][fid], "frs_id"
    wq = extracted_ids.get("state_permit_id_wq")
    if wq and wq in id_index["state_permit"]:
        return id_index["state_permit"][wq], "state_permit_id"
    return None, None


def _score_candidate_against_bucket(
    *,
    name: str,
    state: str,
    facility_type: str,
    latitude: float | None,
    longitude: float | None,
    typed_index: dict,
) -> tuple[CandidateCanonical | None, float, bool]:
    """RapidFuzz match a candidate against the (state, facility_type)
    bucket. Returns (best_canonical, best_score, tiebreak_applied).

    Name-only scoring (vs the main resolver's name|city|state composite):
    the (state, facility_type) bucket pre-filter already guarantees both
    sides of every comparison share state, so a composite key would have
    a shared "| TX" / "| NC" suffix that inflates RapidFuzz partial_ratio
    and lands every comparison at WRatio ~85.5 regardless of name
    similarity. Scoring on name alone restores discrimination. (Discovered
    during the first step D run on 2026-05-14 — see the build_log entry.)
    """
    bucket = typed_index.get((state, facility_type), [])
    if not bucket or not name:
        return None, 0.0, False
    best: CandidateCanonical | None = None
    best_score = -1.0
    for cand in bucket:
        if not cand.name:
            continue
        s = fuzz.WRatio(name, cand.name)
        if s > best_score:
            best_score = s
            best = cand
    # Proximity tiebreak only applies to borderline scores.
    tiebreak = False
    if (
        best is not None
        and HOLD_FOR_REVIEW_THRESHOLD <= best_score < AUTO_MERGE_THRESHOLD
        and latitude is not None
        and longitude is not None
        and best.latitude is not None
        and best.longitude is not None
    ):
        d = _haversine_m(latitude, longitude, best.latitude, best.longitude)
        if d <= PROXIMITY_TIEBREAK_METERS:
            tiebreak = True
    return best, max(0.0, best_score), tiebreak


def _fetch_pending_candidates(conn) -> list[dict]:
    """Pull discovery_candidate_facility rows with review_status='pending'
    joined to discovered_url for the (source_category, state, url) context.
    Ordered by candidate id so processing is deterministic."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT dcf.id, dcf.discovered_url_id, dcf.raw_payload,
                   dcf.classification_confidence, dcf.extracted_at,
                   du.source_category, du.state, du.url, du.query
              FROM discovery_candidate_facility dcf
              JOIN discovered_url du ON du.id = dcf.discovered_url_id
             WHERE dcf.review_status = 'pending'
             ORDER BY dcf.id
            """
        )
        rows = cur.fetchall()
    finally:
        cur.close()
    out: list[dict] = []
    for r in rows:
        cid, url_id, payload, conf, extracted_at, cat, state, url, query = r
        if isinstance(payload, str):
            payload = json.loads(payload)
        out.append(
            {
                "candidate_id": cid,
                "discovered_url_id": url_id,
                "payload": payload,
                "classification_confidence": conf,
                "extracted_at": extracted_at,
                "source_category": cat,
                "state": state,
                "url": url,
                "query": query,
            }
        )
    return out


def _get_or_create_discovery_run(conn) -> tuple[int, int]:
    """Find the discovery_crawl source_id and create one scraper_run row
    for this candidate-import pass. Returns (source_id, scraper_run_id)."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM source WHERE slug = 'discovery_crawl'")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("discovery_crawl source row not seeded; cannot import candidates.")
        source_id = row[0]
        cur.execute(
            """
            INSERT INTO scraper_run (source_id, started_at, status)
            VALUES (%s, NOW(), 'running')
            RETURNING id
            """,
            (source_id,),
        )
        scraper_run_id = cur.fetchone()[0]
        conn.commit()
    finally:
        cur.close()
    return source_id, scraper_run_id


def _finalize_discovery_run(conn, scraper_run_id: int, stats: dict) -> None:
    """Mark the scraper_run row complete with stats."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE scraper_run
               SET finished_at = NOW(),
                   status = 'success',
                   rows_in = %s,
                   rows_inserted = %s,
                   rows_updated = %s
             WHERE id = %s
            """,
            (
                stats.get("candidates_processed", 0),
                stats.get("net_new_canonicals", 0) + stats.get("hold_borderline_canonicals", 0),
                stats.get("existing_matches", 0),
                scraper_run_id,
            ),
        )
        conn.commit()
    finally:
        cur.close()


def _insert_synthetic_raw(
    conn,
    *,
    source_id: int,
    scraper_run_id: int,
    candidate_id: int,
    payload: dict,
) -> int:
    """INSERT one raw_facility_record row representing the discovery
    candidate. source_record_id is keyed on the candidate's primary key
    so the UNIQUE (source_id, source_record_id) constraint allows
    idempotent re-import."""
    cur = conn.cursor()
    try:
        payload_json = json.dumps(payload, default=str)
        import hashlib

        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        cur.execute(
            """
            INSERT INTO raw_facility_record
                (source_id, source_record_id, scraper_run_id, raw_payload, payload_hash)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (source_id, source_record_id) DO UPDATE
               SET raw_payload = EXCLUDED.raw_payload,
                   payload_hash = EXCLUDED.payload_hash
            RETURNING id
            """,
            (
                source_id,
                f"discovery_candidate:{candidate_id}",
                scraper_run_id,
                payload_json,
                payload_hash,
            ),
        )
        raw_id = cur.fetchone()[0]
        conn.commit()
    finally:
        cur.close()
    return raw_id


def _insert_link(
    conn,
    *,
    raw_id: int,
    canonical_id: str,
    match_score: float | None,
) -> None:
    """INSERT facility_record_link with match_method='discovery_extract'."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO facility_record_link
                (raw_facility_record_id, canonical_facility_id, match_score, match_method)
            VALUES (%s, %s, %s, 'discovery_extract')
            ON CONFLICT (raw_facility_record_id) DO UPDATE
               SET canonical_facility_id = EXCLUDED.canonical_facility_id,
                   match_score = EXCLUDED.match_score,
                   match_method = EXCLUDED.match_method,
                   linked_at = NOW()
            """,
            (raw_id, canonical_id, match_score),
        )
        conn.commit()
    finally:
        cur.close()


def _insert_new_canonical(
    conn,
    *,
    payload: dict,
    state: str,
    facility_type: str | None,
) -> str:
    """INSERT a new canonical_facility from a discovery candidate. Sets
    `source='discovery_crawl'` so the access-layer view gate (per the
    20260514220000 migration) keeps it out of v_all_in_scope until a
    human approves the candidate through discovery_review_queue."""
    canonical_id = str(uuid.uuid4())
    name = (payload.get("name") or "").strip() or None
    city = (payload.get("city") or "").strip() or None
    street = (payload.get("address") or "").strip() or None
    phone = (payload.get("phone") or "").strip() or None
    website = (payload.get("website") or "").strip() or None
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO canonical_facility
                (id, name, facility_type, street, city, state, phone, website,
                 source, first_seen_at, last_seen_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'discovery_crawl', NOW(), NOW())
            """,
            (canonical_id, name, facility_type, street, city, state, phone, website),
        )
        conn.commit()
    finally:
        cur.close()
    return canonical_id


def _update_canonical_last_seen(conn, canonical_id: str) -> None:
    """Bump last_seen_at on an existing canonical without touching any
    core fields. Per Ryan's spec: do NOT overwrite facility_type or
    identity fields on an existing-match candidate import."""
    cur = conn.cursor()
    try:
        cur.execute(
            "UPDATE canonical_facility SET last_seen_at = NOW() WHERE id = %s",
            (canonical_id,),
        )
        conn.commit()
    finally:
        cur.close()


def _insert_candidate_provenance(
    conn,
    *,
    canonical_id: str,
    payload: dict,
    source_url: str,
    extracted_at,
    classification_confidence: str,
) -> int:
    """Write one field_provenance row per populated candidate field.
    extraction_method='llm_extracted', confidence=candidate's
    classification_confidence (high/medium/low)."""
    fields_to_write = (
        ("name", payload.get("name")),
        ("street", payload.get("address")),
        ("city", payload.get("city")),
        ("state", payload.get("state")),
        ("phone", payload.get("phone")),
        ("website", payload.get("website")),
        ("facility_type", payload.get("facility_type")),
        # operator_published_acceptance is captured for downstream monthly-refresh
        # enrichment use; persisted as provenance so Austin can see the literal text.
        ("operator_published_acceptance", payload.get("operator_published_acceptance")),
        ("evidence_quotation", payload.get("evidence_quotation")),
    )
    rows = [
        (
            canonical_id,
            field_name,
            str(value),
            source_url,
            extracted_at,
            "llm_extracted",
            classification_confidence,
        )
        for field_name, value in fields_to_write
        if value not in (None, "")
    ]
    if not rows:
        return 0
    cur = conn.cursor()
    try:
        import psycopg2.extras

        psycopg2.extras.execute_values(
            cur,
            """
            INSERT INTO field_provenance
                (canonical_facility_id, field_name, value, source_url, source_date,
                 extraction_method, confidence)
            VALUES %s
            """,
            rows,
        )
        conn.commit()
    finally:
        cur.close()
    return len(rows)


def _insert_review_queue(
    conn,
    *,
    candidate_id: int,
    hold_reason: str,
    canonical_facility_id: str,
    closest_existing_canonical_id: str | None = None,
) -> None:
    """INSERT one discovery_review_queue row pointing to the canonical
    that this candidate produced (net-new) or was held against
    (borderline). canonical_facility_id is the gate the access-layer
    views use: a row stays out of v_all_in_scope until its review queue
    row carries resolution='approved_new'. closest_existing_canonical_id
    is the structured pointer used by v_discovery_review (Phase 4.5
    step E) to surface the merge target on borderline rows; NULL for
    net_new_discovery rows (no existing match)."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO discovery_review_queue
                (candidate_id, hold_reason, canonical_facility_id,
                 closest_existing_canonical_id)
            VALUES (%s, %s, %s, %s)
            """,
            (
                candidate_id,
                hold_reason,
                canonical_facility_id,
                closest_existing_canonical_id,
            ),
        )
        conn.commit()
    finally:
        cur.close()


def _mark_candidate_processed(conn, candidate_id: int) -> None:
    """Flip discovery_candidate_facility.review_status to
    'processed_to_canonical' so the resolver does not re-process."""
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE discovery_candidate_facility
               SET review_status = 'processed_to_canonical'
             WHERE id = %s
            """,
            (candidate_id,),
        )
        conn.commit()
    finally:
        cur.close()


def run_candidate_import(*, dry_run: bool = False) -> dict:
    """Phase 4.5 step D entrypoint. Walks every pending discovery candidate
    and routes it to one of three outcomes (existing-match / net-new /
    hold-borderline). Returns a stats dict for the run report.

    Reuses the matching primitives from the main resolver (rapidfuzz
    WRatio + 200m proximity tiebreak); does NOT duplicate match logic.
    The (state, facility_type) bucket scoping is specific to the
    candidate-import path."""
    started = time.time()
    print(f"[candidate-import] starting (dry_run={dry_run})", flush=True)

    conn = db_connect()

    # Pre-load existing-canonical indexes for ID-first and RapidFuzz lookup.
    typed_index = _preload_canonical_typed_index(conn)
    id_index = _preload_canonical_id_index(conn)
    print(
        f"[candidate-import] typed canonicals indexed: "
        f"{sum(len(v) for v in typed_index.values()):,} across "
        f"{len(typed_index)} (state, facility_type) buckets",
        flush=True,
    )
    print(
        f"[candidate-import] ID lookups: npdes={len(id_index['npdes']):,} "
        f"frs={len(id_index['frs']):,} state_permit={len(id_index['state_permit']):,}",
        flush=True,
    )

    candidates = _fetch_pending_candidates(conn)
    total = len(candidates)
    print(f"[candidate-import] {total} pending candidates to process", flush=True)
    if total == 0:
        conn.close()
        return {
            "elapsed_sec": round(time.time() - started, 2),
            "candidates_processed": 0,
            "existing_matches": 0,
            "net_new_canonicals": 0,
            "hold_borderline_canonicals": 0,
            "cross_bucket_dedups": 0,
        }

    pre_by_cat = Counter(c["source_category"] for c in candidates)
    print("[candidate-import] pending-candidate plan:", flush=True)
    for cat, n in sorted(pre_by_cat.items()):
        print(f"  {cat:35s}  {n:>4d}", flush=True)

    # Mutable run state.
    source_id, scraper_run_id = _get_or_create_discovery_run(conn)
    print(
        f"[candidate-import] scraper_run id={scraper_run_id} "
        f"under source_id={source_id} (discovery_crawl)",
        flush=True,
    )

    stats: dict[str, int] = defaultdict(int)
    per_cat: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    net_new_samples: dict[str, list[dict]] = defaultdict(list)
    borderline_samples: list[dict] = []
    cross_bucket_dedup_events: list[dict] = []

    for i, c in enumerate(candidates, 1):
        cat = c["source_category"]
        url_state = (c.get("state") or "").strip().upper()
        payload = c["payload"]
        cand_state = _normalize_state_to_usps(payload.get("state"), url_state)
        cand_name = (payload.get("name") or "").strip()
        cand_city = (payload.get("city") or "").strip() or None
        cand_ftype = (payload.get("facility_type") or "").strip() or None
        cand_conf = c["classification_confidence"]

        stats["candidates_processed"] += 1
        per_cat[cat]["processed"] += 1

        # State filter: candidate must have a usable state. Defensively skip
        # otherwise — the extraction prompt enforced state filtering but a
        # malformed payload could still land here.
        if not cand_name or not cand_state or not cand_ftype:
            stats["malformed_skipped"] += 1
            per_cat[cat]["malformed_skipped"] += 1
            _mark_candidate_processed(conn, c["candidate_id"])
            continue

        # SFR filter is global; check_residential() is no-op for non-NC-ND
        # source slugs so the check is defensive but cheap.
        sfr_payload = {"name": cand_name}  # synthetic NormalizedRaw-like shape
        # Skip the actual check_residential call since it requires a NormalizedRaw
        # AND the source_slug='discovery_crawl' will short-circuit out anyway.
        del sfr_payload

        # 1) ID-first match.
        extracted_ids = _extract_ids_from_candidate(payload)
        if extracted_ids:
            per_cat[cat]["had_extractable_ids"] += 1
        match_canonical_id, matched_field = _candidate_id_match(extracted_ids, id_index)

        score = None
        outcome = None
        closest_existing: CandidateCanonical | None = None

        if match_canonical_id is not None:
            outcome = "existing_match_id"
            stats[f"id_match_{matched_field}"] += 1
        else:
            # 2) RapidFuzz score match scoped to (state, facility_type).
            best, score, tiebreak = _score_candidate_against_bucket(
                name=cand_name,
                state=cand_state,
                facility_type=cand_ftype,
                latitude=None,
                longitude=None,
                typed_index=typed_index,
            )
            if best is not None:
                closest_existing = best
            if best is not None and (score >= AUTO_MERGE_THRESHOLD or tiebreak):
                match_canonical_id = best.canonical_id
                outcome = "existing_match_score" if not tiebreak else "existing_match_tiebreak"
            elif best is not None and score >= HOLD_FOR_REVIEW_THRESHOLD:
                outcome = "hold_borderline"
            else:
                outcome = "net_new"

        # Outcome dispatch.
        if outcome.startswith("existing_match"):
            stats["existing_matches"] += 1
            per_cat[cat]["existing_match"] += 1
            # Detect cross-bucket dedup: if the matched canonical was inserted
            # earlier in this same run, record the event.
            if not dry_run:
                _update_canonical_last_seen(conn, match_canonical_id)
                raw_id = _insert_synthetic_raw(
                    conn,
                    source_id=source_id,
                    scraper_run_id=scraper_run_id,
                    candidate_id=c["candidate_id"],
                    payload=payload,
                )
                _insert_link(
                    conn,
                    raw_id=raw_id,
                    canonical_id=match_canonical_id,
                    match_score=score,
                )
                _insert_candidate_provenance(
                    conn,
                    canonical_id=match_canonical_id,
                    payload=payload,
                    source_url=c["url"],
                    extracted_at=c["extracted_at"],
                    classification_confidence=cand_conf,
                )
                _mark_candidate_processed(conn, c["candidate_id"])
            # Cross-bucket dedup: detect by checking if this canonical was
            # created during this run (track via a set).
            if match_canonical_id in stats.get("_run_inserted_ids", set()):
                stats["cross_bucket_dedups"] += 1
                cross_bucket_dedup_events.append(
                    {
                        "candidate_id": c["candidate_id"],
                        "source_category": cat,
                        "canonical_id": match_canonical_id,
                        "name": cand_name,
                        "state": cand_state,
                    }
                )

        elif outcome == "hold_borderline":
            stats["hold_borderline_canonicals"] += 1
            per_cat[cat]["hold_borderline"] += 1
            if not dry_run:
                # Net-new canonical + queue with hold_reason.
                ftype_to_assign = cand_ftype if cand_conf == "high" else None
                new_cid = _insert_new_canonical(
                    conn,
                    payload=payload,
                    state=cand_state,
                    facility_type=ftype_to_assign,
                )
                stats.setdefault("_run_inserted_ids", set()).add(new_cid)
                # Add to in-memory typed_index so subsequent candidates can
                # dedup against this newly inserted canonical.
                typed_index.setdefault((cand_state, cand_ftype), []).append(
                    CandidateCanonical(
                        canonical_id=new_cid,
                        name=cand_name,
                        city=cand_city,
                        state=cand_state,
                        latitude=None,
                        longitude=None,
                    )
                )
                raw_id = _insert_synthetic_raw(
                    conn,
                    source_id=source_id,
                    scraper_run_id=scraper_run_id,
                    candidate_id=c["candidate_id"],
                    payload=payload,
                )
                _insert_link(
                    conn,
                    raw_id=raw_id,
                    canonical_id=new_cid,
                    match_score=score,
                )
                _insert_candidate_provenance(
                    conn,
                    canonical_id=new_cid,
                    payload=payload,
                    source_url=c["url"],
                    extracted_at=c["extracted_at"],
                    classification_confidence=cand_conf,
                )
                hold_reason = (
                    f"borderline_match score={score:.1f} "
                    f"closest_canonical_id={closest_existing.canonical_id if closest_existing else 'none'} "
                    f"closest_name={closest_existing.name if closest_existing else 'none'!r}"
                )
                _insert_review_queue(
                    conn,
                    candidate_id=c["candidate_id"],
                    hold_reason=hold_reason,
                    canonical_facility_id=new_cid,
                    closest_existing_canonical_id=(
                        closest_existing.canonical_id if closest_existing else None
                    ),
                )
                _mark_candidate_processed(conn, c["candidate_id"])
            borderline_samples.append(
                {
                    "candidate_id": c["candidate_id"],
                    "source_category": cat,
                    "name": cand_name,
                    "state": cand_state,
                    "score": score,
                    "closest_existing_id": closest_existing.canonical_id
                    if closest_existing
                    else None,
                    "closest_existing_name": closest_existing.name if closest_existing else None,
                }
            )

        else:  # net_new
            stats["net_new_canonicals"] += 1
            per_cat[cat]["net_new"] += 1
            if not dry_run:
                ftype_to_assign = cand_ftype if cand_conf == "high" else None
                new_cid = _insert_new_canonical(
                    conn,
                    payload=payload,
                    state=cand_state,
                    facility_type=ftype_to_assign,
                )
                stats.setdefault("_run_inserted_ids", set()).add(new_cid)
                typed_index.setdefault((cand_state, cand_ftype), []).append(
                    CandidateCanonical(
                        canonical_id=new_cid,
                        name=cand_name,
                        city=cand_city,
                        state=cand_state,
                        latitude=None,
                        longitude=None,
                    )
                )
                raw_id = _insert_synthetic_raw(
                    conn,
                    source_id=source_id,
                    scraper_run_id=scraper_run_id,
                    candidate_id=c["candidate_id"],
                    payload=payload,
                )
                _insert_link(conn, raw_id=raw_id, canonical_id=new_cid, match_score=None)
                _insert_candidate_provenance(
                    conn,
                    canonical_id=new_cid,
                    payload=payload,
                    source_url=c["url"],
                    extracted_at=c["extracted_at"],
                    classification_confidence=cand_conf,
                )
                # Net-new also lands in review queue (review_status='pending',
                # hold_reason='net_new_discovery'): per the brief, discovery
                # cannot auto-create v_all_in_scope rows without human review.
                _insert_review_queue(
                    conn,
                    candidate_id=c["candidate_id"],
                    hold_reason="net_new_discovery",
                    canonical_facility_id=new_cid,
                )
                _mark_candidate_processed(conn, c["candidate_id"])
            if len(net_new_samples[cat]) < 10:
                net_new_samples[cat].append(
                    {
                        "candidate_id": c["candidate_id"],
                        "name": cand_name,
                        "city": cand_city,
                        "state": cand_state,
                        "facility_type": cand_ftype,
                        "confidence": cand_conf,
                        "source_url": c["url"],
                    }
                )

        if i % 25 == 0 or i == 1:
            print(
                f"[candidate-import] [{i:>4}/{total}]  {cat:30s} {cand_state}  "
                f"{outcome:25s}  total_new={stats['net_new_canonicals']}  "
                f"matches={stats['existing_matches']}",
                flush=True,
            )

    # Mark run complete and clean up the internal _run_inserted_ids key (set
    # objects are not JSON serializable in the stats dict downstream).
    stats.pop("_run_inserted_ids", None)
    _finalize_discovery_run(conn, scraper_run_id, dict(stats))

    # Queue depth after run.
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT count(*)
              FROM discovery_review_queue
             WHERE resolved_at IS NULL
            """
        )
        queue_depth = cur.fetchone()[0]
    finally:
        cur.close()
    conn.close()

    elapsed = round(time.time() - started, 2)
    print(
        f"\n[candidate-import] complete: {stats['candidates_processed']} "
        f"candidates -> {stats['existing_matches']} existing-match, "
        f"{stats['net_new_canonicals']} net-new, "
        f"{stats['hold_borderline_canonicals']} hold-borderline in {elapsed}s",
        flush=True,
    )

    return {
        "elapsed_sec": elapsed,
        "candidates_processed": stats["candidates_processed"],
        "malformed_skipped": stats.get("malformed_skipped", 0),
        "existing_matches": stats["existing_matches"],
        "id_match_npdes_id": stats.get("id_match_npdes_id", 0),
        "id_match_frs_id": stats.get("id_match_frs_id", 0),
        "id_match_state_permit_id": stats.get("id_match_state_permit_id", 0),
        "net_new_canonicals": stats["net_new_canonicals"],
        "hold_borderline_canonicals": stats["hold_borderline_canonicals"],
        "cross_bucket_dedups": stats.get("cross_bucket_dedups", 0),
        "discovery_review_queue_depth": queue_depth,
        "scraper_run_id": scraper_run_id,
        "per_category": {k: dict(v) for k, v in per_cat.items()},
        "net_new_samples": dict(net_new_samples),
        "borderline_samples": borderline_samples,
        "cross_bucket_dedup_events": cross_bucket_dedup_events,
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
    ap.add_argument(
        "--candidate-import",
        action="store_true",
        help=(
            "Phase 4.5 step D: import discovery_candidate_facility rows where "
            "review_status='pending' into canonical_facility via the same "
            "matching primitives the main resolver uses."
        ),
    )
    args = ap.parse_args()

    if args.candidate_import:
        # Phase 4.5 step D path.
        stats = run_candidate_import(dry_run=args.dry_run)
        print("\n=== Candidate-import summary ===")
        for k, v in stats.items():
            if isinstance(v, (int, float, str)):
                print(f"  {k:35s}  {v}")
        print("\n=== Per-category breakdown ===")
        for cat, breakdown in stats["per_category"].items():
            print(f"  {cat}")
            for k, v in sorted(breakdown.items()):
                print(f"    {k:35s}  {v}")
        return 0

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
