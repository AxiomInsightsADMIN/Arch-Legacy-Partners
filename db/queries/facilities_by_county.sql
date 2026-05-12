-- =============================================================================
-- facilities_by_county.sql
--
-- Purpose
--   Grouped count of in-scope canonical_facility rows per (state, county,
--   facility_type). Surfaces geographic coverage gaps and concentration.
--
-- Parameters
--   None. To restrict, append a HAVING clause or a WHERE on the outer
--   SELECT (e.g. `... WHERE state='NC' AND facility_type='transfer_station'`).
--
-- Output columns
--   state, county, facility_type, facility_count.
--
-- Example use case
--   "Which NC counties have zero transfer stations?" — pivot the result
--   in a spreadsheet against a master county list. Or "Which TX counties
--   have the most composting facilities?" — sort by facility_count DESC
--   within state=TX, facility_type='composting_facility'.
-- =============================================================================

SELECT state,
       COALESCE(county, '(unknown)') AS county,
       facility_type,
       COUNT(*) AS facility_count
  FROM v_all_in_scope
 GROUP BY state, county, facility_type
 ORDER BY state, county, facility_type;
