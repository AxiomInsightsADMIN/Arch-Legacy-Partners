-- =============================================================================
-- facilities_missing_coords.sql
--
-- Purpose
--   Data-quality query: surfaces every in-scope canonical_facility that
--   has no lat/lng. These rows can't drive proximity-based features
--   (map exports, route planning, 200m-radius searches) until the
--   coords are filled. Phase 4 geocoder backfill expands beyond NC SF +
--   NC ND; today these are mostly NC ND rows where the source data has
--   no street address.
--
-- Parameters
--   None. Filter further with a WHERE clause on state / facility_type.
--
-- Output columns
--   id, name, state, county, facility_type,
--   street, city, zip,
--   state_permit_id (helps trace back to source loader),
--   last_seen_at.
--
-- Example use case
--   "What's the coverage gap on the NC side?" — count rows by county
--   and prioritize manual review or Phase 4 augmentation.
-- =============================================================================

SELECT id, name, state, county, facility_type,
       street, city, zip,
       state_permit_id,
       last_seen_at
  FROM v_all_in_scope
 WHERE latitude IS NULL OR longitude IS NULL
 ORDER BY state, COALESCE(county, ''), COALESCE(name, '');
