-- =============================================================================
-- canonical_history_recent.sql
--
-- Purpose
--   Returns the most recent canonical_facility field-level changes,
--   joined to the canonical row name so the audit is readable. Useful
--   for "what changed in the last monthly refresh?" before the
--   refresh/YYYY-MM-DD branch PR gets merged.
--
-- Parameters
--   None. Default is "last 30 days"; adjust the INTERVAL to widen.
--
-- Output columns
--   changed_at, canonical_facility_id, facility_name, facility_state,
--   field_name, old_value, new_value, change_source.
--
-- Example use case
--   Operator reviews the May→June monthly refresh PR. Running this
--   query against main after merge shows every canonical_facility
--   value mutation in the last 30 days, sourced from the monthly
--   resolver-rebuild.
--
-- Phase note
--   The v1 resolver (resolver/_canonicalize.py) intentionally does
--   NOT populate canonical_facility_history. The schema exists for
--   future field-level audit but the v1 audit surface is
--   field_provenance (one row per canonical × field per linked raw).
--   This query returns 0 rows until Phase 4 enrichment starts writing
--   history entries.
-- =============================================================================

SELECT h.changed_at,
       h.canonical_facility_id,
       cf.name        AS facility_name,
       cf.state       AS facility_state,
       h.field_name,
       h.old_value,
       h.new_value,
       h.change_source
  FROM canonical_facility_history h
  JOIN canonical_facility cf ON cf.id = h.canonical_facility_id
 WHERE h.changed_at > NOW() - INTERVAL '30 days'
 ORDER BY h.changed_at DESC, h.canonical_facility_id, h.field_name;
