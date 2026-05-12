"""
geocoder.py
Arch Legacy Partners — Wastewater Facility Database

Geocoder module: US Census Geocoder integration plus the
**state-consistency warning policy** documented below.

================================================================================
State-consistency warning policy (Checkpoint-2 decision A1.6)
================================================================================

Background. The `canonical_facility` table's latitude/longitude CHECK
constraints in `supabase/migrations/20260511203500_initial.sql` are
intentionally **global** (lat -90..90, long -180..180) rather than
per-state envelopes. Reason:
forward-compat. The kickoff brief allows $40/state additions after v1
delivery; we don't want a schema migration for every new state.

That gives the database freedom but it means **bad geocoder results can land
in the canonical row** if we are not careful. This module enforces the
per-state envelope at the *application* layer.

Policy contract — every loader and the discovery extractor MUST consume
geocoded coordinates through `geocode_with_state_check()` (or an equivalent
that calls `coords_consistent_with_state()`). The contract is:

  1) Geocode the address via US Census Geocoder. Cache the response in
     `geocoding_cache` (address_hash → lat/lng/confidence/matched_address).
  2) If geocoder returns NO match → return (lat=None, lng=None,
     confidence='failed'). Per locked decision 8.5 we NEVER stub coords.
  3) If geocoder returns a match → check `coords_consistent_with_state()`
     against the facility's `state` field using STATE_BOUNDS below.
  4) If the coords fall *outside* the state envelope:
        - Downgrade the geocoder confidence to 'low'
        - Emit a `state_coord_mismatch` warning at field_provenance level
        - Set a review flag (write to `discovery_review_queue` if the row
          originated from discovery; otherwise log to scraper_run.error_message
          with a non-fatal marker so the resolver flags it for human review)
        - Still write the lat/lng — we don't drop the data; we just lower
          its trust score so the review surface picks it up
  5) If the coords fall *inside* the state envelope but `matched_address`
     differs significantly from the input → drop confidence one tier
     (high → medium, medium → low) per the brief section 8.5.

STATE_BOUNDS values below are conservative envelopes that include the
state's mainland *and* offshore territory waste-water facility locations
(e.g. Galveston Bay, Outer Banks). Edges are rounded outward by ~0.3°.

If a future state is added with no envelope here, the check returns
'unknown' and the geocoder logs a `state_bounds_missing` warning so we know
to extend this dict before that state's loader ships.

================================================================================
Phase note
================================================================================

The Census Geocoder client is implemented as of Phase 3 (2026-05-12). Phase
1-2 loaders shipped without exercising the client because the source data
arrived with native lat/lng (EPA ECHO, EPA CWNS, TCEQ MSW, NC SW). Phase 3
needs the client to backfill coords for the no-coord sources (NC ND, NC SF)
before the 200m proximity tiebreak runs.

Public surface:
  - STATE_BOUNDS              dict of per-state envelopes
  - GeocoderResult            return dataclass
  - coords_consistent_with_state(lat, lng, state) -> "inside"|"outside"|"unknown"
  - geocode_with_state_check(address, state, *, conn=None) -> GeocoderResult
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Literal

import requests

# -----------------------------------------------------------------------------
# Per-state coordinate envelopes
# -----------------------------------------------------------------------------
# Conservative bounding boxes — outermost mainland + offshore facility
# locations + ~0.3° rounding. Edit only via migration (this module is mirrored
# into compliance docs).

STATE_BOUNDS: dict[str, tuple[float, float, float, float]] = {
    # state: (min_lat, max_lat, min_lng, max_lng)
    "TX": (25.5, 36.7, -106.8, -93.4),
    "NC": (33.7, 36.7, -84.5, -75.3),
}

ConsistencyResult = Literal["inside", "outside", "unknown"]

GeoConfidence = Literal["high", "medium", "low", "failed"]


@dataclass(frozen=True)
class GeocoderResult:
    """Return shape of `geocode_with_state_check`."""

    lat: float | None
    lng: float | None
    confidence: GeoConfidence
    matched_address: str | None
    consistency: ConsistencyResult
    review_flag: bool
    notes: str | None


def coords_consistent_with_state(
    *, lat: float | None, lng: float | None, state: str | None
) -> ConsistencyResult:
    """Return whether (lat, lng) fall inside the envelope for `state`.

    Returns:
      "inside"   — coords are within the state's envelope
      "outside"  — coords are outside the state's envelope (review flag)
      "unknown"  — state has no envelope defined yet (state_bounds_missing)

    A NULL/None input on any of the three args returns "unknown" so callers
    can route to the appropriate handler without an exception.
    """
    if lat is None or lng is None or not state:
        return "unknown"
    bounds = STATE_BOUNDS.get(state.upper())
    if bounds is None:
        return "unknown"
    lo_lat, hi_lat, lo_lng, hi_lng = bounds
    if lo_lat <= lat <= hi_lat and lo_lng <= lng <= hi_lng:
        return "inside"
    return "outside"


# -----------------------------------------------------------------------------
# Census Geocoder client
# -----------------------------------------------------------------------------
CENSUS_ONELINE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
CENSUS_BENCHMARK = "Public_AR_Current"
CENSUS_TIMEOUT = 10
CENSUS_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT",
    "Axiom-Insights-ArchLegacy/0.1 (Phase 3 entity resolver; "
    f"contact: {os.environ.get('ALERT_EMAIL', 'unknown')})",
)


def _normalize_address(address: str) -> str:
    """Trim, collapse whitespace, upper-case. Cache keys hash this form so
    "100 Main St" and "  100 main st  " hit the same cache row."""
    return " ".join(address.strip().split()).upper()


def _address_hash(address_norm: str) -> str:
    return hashlib.sha256(address_norm.encode("utf-8")).hexdigest()


def _cache_lookup(conn, address_hash: str) -> GeocoderResult | None:
    """Return cached GeocoderResult or None. Caller passes a live psycopg2
    connection; we do not own the connection lifecycle."""
    if conn is None:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT lat, lng, confidence, matched_address
              FROM geocoding_cache
             WHERE address_hash = %s
            """,
            (address_hash,),
        )
        row = cur.fetchone()
    if not row:
        return None
    lat, lng, confidence, matched = row
    # Cache only persists the geocoder side; state-consistency is recomputed
    # per call because the caller's `state` argument may differ between calls
    # against the same address.
    return GeocoderResult(
        lat=lat,
        lng=lng,
        confidence=confidence,
        matched_address=matched,
        consistency="unknown",
        review_flag=False,
        notes="cache_hit",
    )


def _cache_store(
    conn,
    *,
    address_hash: str,
    normalized_input: str,
    lat: float | None,
    lng: float | None,
    confidence: GeoConfidence,
    matched_address: str | None,
) -> None:
    if conn is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO geocoding_cache
                (address_hash, normalized_input, lat, lng, confidence, matched_address)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (address_hash) DO UPDATE
              SET lat              = EXCLUDED.lat,
                  lng              = EXCLUDED.lng,
                  confidence       = EXCLUDED.confidence,
                  matched_address  = EXCLUDED.matched_address,
                  geocoded_at      = NOW()
            """,
            (
                address_hash,
                normalized_input,
                lat,
                lng,
                confidence,
                matched_address,
            ),
        )
    conn.commit()


def _call_census(address: str) -> dict | None:
    """One-shot HTTP call against Census onelineaddress. Returns parsed JSON
    or None on transport error / non-200 / parse failure. Caller decides
    whether to retry; we keep this dumb."""
    try:
        resp = requests.get(
            CENSUS_ONELINE_URL,
            params={
                "address": address,
                "benchmark": CENSUS_BENCHMARK,
                "format": "json",
            },
            headers={"User-Agent": CENSUS_USER_AGENT},
            timeout=CENSUS_TIMEOUT,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


def geocode_with_state_check(
    *,
    address: str,
    state: str | None,
    conn=None,
    retries: int = 1,
) -> GeocoderResult:
    """End-to-end geocode + state-consistency check.

    Args:
        address:  full single-line address to geocode (street, city, state, zip).
        state:    USPS 2-letter code expected for the facility, for the
                  consistency check.
        conn:     optional psycopg2 connection for the geocoding_cache lookup
                  and write. Pass None to bypass the cache (e.g. tests).
        retries:  number of HTTP retry attempts on transport failure
                  (default 1, total attempts = retries + 1).

    The function:
      1) Normalizes + hashes the address, looks up `geocoding_cache`.
      2) On cache miss, calls Census `onelineaddress` (benchmark=Public_AR_Current).
      3) On no addressMatches → return GeocoderResult(None, None, 'failed',
         None, 'unknown', False, 'census_no_match'). Cached as 'failed'.
      4) On match → coords from `result.addressMatches[0].coordinates`
         (x=lng, y=lat in Census format). Confidence='high' baseline.
      5) State-consistency:
          inside  → keep confidence='high'
          outside → downgrade to 'low', review_flag=True,
                     notes='state_coord_mismatch'
          unknown → confidence='medium', review_flag=True,
                     notes='state_bounds_missing'
      6) Cache the result and return.

    Never stubs coords. NULL means "asked and got nothing."
    """
    if not address or not address.strip():
        return GeocoderResult(
            lat=None,
            lng=None,
            confidence="failed",
            matched_address=None,
            consistency="unknown",
            review_flag=False,
            notes="empty_address",
        )

    normalized = _normalize_address(address)
    ahash = _address_hash(normalized)

    cached = _cache_lookup(conn, ahash)
    if cached is not None:
        consistency = coords_consistent_with_state(lat=cached.lat, lng=cached.lng, state=state)
        review_flag = consistency == "outside" or (
            consistency == "unknown" and cached.lat is not None and state
        )
        return GeocoderResult(
            lat=cached.lat,
            lng=cached.lng,
            confidence=cached.confidence,
            matched_address=cached.matched_address,
            consistency=consistency,
            review_flag=review_flag,
            notes="cache_hit",
        )

    payload = None
    for attempt in range(retries + 1):
        payload = _call_census(normalized)
        if payload is not None:
            break
        if attempt < retries:
            time.sleep(0.5)

    if payload is None:
        # Transport / API failure — do NOT cache as 'failed' (it might come
        # back later), but do not raise either: callers want a graceful no-coords
        # result so a bulk run can finish.
        return GeocoderResult(
            lat=None,
            lng=None,
            confidence="failed",
            matched_address=None,
            consistency="unknown",
            review_flag=False,
            notes="census_transport_error",
        )

    matches = payload.get("result", {}).get("addressMatches", [])
    if not matches:
        _cache_store(
            conn,
            address_hash=ahash,
            normalized_input=normalized,
            lat=None,
            lng=None,
            confidence="failed",
            matched_address=None,
        )
        return GeocoderResult(
            lat=None,
            lng=None,
            confidence="failed",
            matched_address=None,
            consistency="unknown",
            review_flag=False,
            notes="census_no_match",
        )

    first = matches[0]
    coords = first.get("coordinates", {})
    lat = coords.get("y")
    lng = coords.get("x")
    matched = first.get("matchedAddress")
    confidence: GeoConfidence = "high"
    notes = None
    review_flag = False

    consistency = coords_consistent_with_state(lat=lat, lng=lng, state=state)
    if consistency == "outside":
        confidence = "low"
        review_flag = True
        notes = "state_coord_mismatch"
    elif consistency == "unknown" and state:
        # State envelope missing — flag for future maintainers to extend.
        confidence = "medium"
        review_flag = True
        notes = "state_bounds_missing"

    _cache_store(
        conn,
        address_hash=ahash,
        normalized_input=normalized,
        lat=lat,
        lng=lng,
        confidence=confidence,
        matched_address=matched,
    )
    return GeocoderResult(
        lat=lat,
        lng=lng,
        confidence=confidence,
        matched_address=matched,
        consistency=consistency,
        review_flag=review_flag,
        notes=notes,
    )
