-- =============================================================================
-- facilities_with_low_confidence_geocoding.sql
--
-- Purpose
--   Data-quality query: surfaces addresses where the Census Geocoder
--   returned coordinates BUT they fell outside the facility's state
--   envelope (per orchestration/geocoder.py STATE_BOUNDS). These rows
--   are cached as `confidence='low'` in `geocoding_cache` and are
--   DELIBERATELY EXCLUDED from the resolver's coord-enrichment step
--   (resolver/entity_resolver.py:_load_geocoding_cache filters on
--   `confidence='high'`) so they don't drive false-positive 200m
--   proximity merges against wrong-state street-name collisions.
--
-- Parameters
--   None.
--
-- Output columns
--   address_hash (sha256 of normalized address, useful for joining back
--                 to the source raw row),
--   normalized_input (the one-line address sent to Census),
--   lat, lng (Census-returned but state-mismatched),
--   matched_address (what Census echoed back),
--   geocoded_at.
--
-- Example use case
--   "Show me every address the geocoder mismatched so I can manually
--   confirm or correct." Operator inspects each row, decides whether
--   the address is in the wrong state (no-fix; Census did its best),
--   the address string is ambiguous (consider improving the synth in
--   resolver/_normalize.synthesize_address_for_geocoding), or the
--   STATE_BOUNDS envelope is too tight (consider widening). Today
--   returns 27 rows (NC SF: 11; NC ND: 16) from the Phase 5 backfill.
-- =============================================================================

SELECT address_hash,
       normalized_input,
       lat,
       lng,
       matched_address,
       geocoded_at
  FROM geocoding_cache
 WHERE confidence = 'low'
 ORDER BY geocoded_at DESC;
