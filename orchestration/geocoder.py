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

This module is a **stub** as of 2026-05-11. The functions are declared with
signatures and the policy contract but only `coords_consistent_with_state()`
and `STATE_BOUNDS` are implemented; the actual Census Geocoder client is
delivered in Phase 1 when the EPA ECHO loader needs it. The state-consistency
policy lives here regardless of when the geocoder client lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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


def geocode_with_state_check(
    *, address: str, state: str | None
) -> GeocoderResult:  # pragma: no cover — Phase-1 stub
    """End-to-end geocode + state-consistency check.

    This is a STUB until Phase 1 implements the US Census Geocoder client.
    The function signature and the policy contract are frozen here so loaders
    can be written against the stable interface.

    Implementation contract for Phase 1:
      - Hash `address` (sha256 of normalized form) and look up `geocoding_cache`.
      - On cache miss: call Census `onelineaddress` endpoint with `benchmark=4`
        (Public_AR_Current) and `vintage=4`. Time out at 10s.
      - On no match: return GeocoderResult(None, None, 'failed', None,
        'unknown', False, 'census_no_match').
      - On match: call `coords_consistent_with_state(...)`:
          inside  -> confidence='high' (or 'medium' if Census reports approximate)
          outside -> confidence='low', review_flag=True, notes='state_coord_mismatch'
          unknown -> confidence='medium', review_flag=True, notes='state_bounds_missing'
      - Cache the (lat, lng, confidence, matched_address) tuple under the
        address_hash regardless of outcome.

    No coordinates are ever stubbed. NULL means "we asked and got nothing."
    """
    raise NotImplementedError(
        "geocode_with_state_check() is a stub until Phase 1. "
        "Use coords_consistent_with_state() for the policy check standalone."
    )
