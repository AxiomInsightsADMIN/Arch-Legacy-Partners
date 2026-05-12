-- =============================================================================
-- all_nc_transfer_stations.sql
--
-- Purpose
--   Every North Carolina canonical_facility that maps to the
--   `transfer_station` v1 category. Includes the 89 NC DEQ DWM
--   `Activity='Trans'` rows. **Excludes** the 30 `Activity='Collection'`
--   HHW citizen-drop-off rows (filter rule pinned in
--   docs/build_log.md -> Phase 3 prep).
--
-- Parameters
--   None.
--
-- Output columns
--   id, name, street, city, county, zip,
--   latitude, longitude, phone,
--   state_permit_id (NC DEQ Facility Id, format
--                    "<county-prefix>-<type-code>-<year-or-suffix>"),
--   first_seen_at, last_seen_at.
--
-- Example use case
--   Operations needs a CSV of NC transfer stations to map against hauler
--   routes. Run this in SQL Editor, export. 97.7% of rows carry lat/lng
--   (NC DEQ DWM publishes geometry on this list, unlike DWR Non-Discharge).
-- =============================================================================

SELECT id, name, street, city, county, zip,
       latitude, longitude, phone,
       state_permit_id,
       first_seen_at, last_seen_at
  FROM v_nc_transfer_station
 ORDER BY COALESCE(county, ''), COALESCE(name, '');
