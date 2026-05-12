-- =============================================================================
-- per_source_row_counts.sql
--
-- Purpose
--   Operational: returns raw_facility_record counts grouped by source,
--   plus the latest signature metadata (bytes, schema_hash, captured_at).
--   The "is our data fresh?" sanity check before running Phase 4
--   enrichment or shipping a CSV refresh.
--
-- Parameters
--   None.
--
-- Output columns
--   source_slug,
--   raw_rows (live count from raw_facility_record),
--   latest_signature_rows (row_count reported by the most recent
--                          successful scraper_run via source_signature),
--   latest_signature_bytes (response_byte_size of that signature),
--   latest_signature_schema_hash (12-char prefix),
--   latest_signature_captured_at.
--
-- Example use case
--   "Are my raw counts in sync with the most recent signature?"
--   raw_rows should equal latest_signature_rows for sources with hash-
--   based idempotent loaders — any drift between them means rows landed
--   outside a tracked scraper_run. Investigate.
-- =============================================================================

WITH latest_sig AS (
    SELECT DISTINCT ON (ss.source_id)
           ss.source_id,
           ss.row_count,
           ss.response_byte_size,
           LEFT(ss.schema_hash, 12) AS schema_hash_prefix,
           ss.captured_at
      FROM source_signature ss
      JOIN scraper_run sr ON sr.id = ss.scraper_run_id
     WHERE sr.status = 'success'
     ORDER BY ss.source_id, ss.captured_at DESC
)
SELECT s.slug AS source_slug,
       COUNT(r.*) AS raw_rows,
       ls.row_count    AS latest_signature_rows,
       ls.response_byte_size AS latest_signature_bytes,
       ls.schema_hash_prefix AS latest_signature_schema_hash,
       ls.captured_at        AS latest_signature_captured_at
  FROM source s
  LEFT JOIN raw_facility_record r ON r.source_id = s.id
  LEFT JOIN latest_sig ls          ON ls.source_id = s.id
 GROUP BY s.slug, ls.row_count, ls.response_byte_size,
          ls.schema_hash_prefix, ls.captured_at
 ORDER BY raw_rows DESC NULLS LAST;
