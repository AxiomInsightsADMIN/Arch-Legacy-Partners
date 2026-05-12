-- =============================================================================
-- all_tx_septage_facilities.sql
--
-- Purpose
--   Every Texas canonical_facility that maps to the
--   `private_regional_septage_facility` v1 category. The primary surface for
--   "Who handles septage in Texas?" — sorted by city + name for stable
--   one-click CSV export.
--
-- Parameters
--   None.
--
-- Output columns
--   id, name, city, county, street, zip,
--   latitude, longitude, phone, email, website,
--   accepts_septage, accepts_grease_trap, accepts_portable_toilet,
--   state_permit_id (TCEQ Additional ID for these rows),
--   first_seen_at, last_seen_at.
--
-- Example use case
--   Sales Ops needs the TX septage list for a hauler-prospecting CSV.
--   Run this in Supabase SQL Editor, click "Export → CSV", drop into
--   the prospecting sheet. The accepts_* columns will be empty until
--   Phase 4 Haiku enrichment fills them; pricing_notes is also a Phase 4
--   add. Today the view returns ~36 rows (sourced from TCEQ MSW Physical
--   Type 5GG / 5TL / 5GM per GI-613).
-- =============================================================================

SELECT id, name, city, county, street, zip,
       latitude, longitude, phone, email, website,
       accepts_septage, accepts_grease_trap, accepts_portable_toilet,
       state_permit_id,
       first_seen_at, last_seen_at
  FROM v_tx_private_regional_septage_facility
 ORDER BY COALESCE(city, ''), COALESCE(name, '');
