-- =============================================================================
-- 20260511221000_add_last_modified_to_source_signature.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 2 step 1: add `last_modified` to source_signature so loaders can
-- record the Last-Modified HTTP response header at fetch time.
-- Authored 2026-05-11 by Axiom Insights. Authorized by Ryan after the
-- ADD COLUMN spec was clarified.
--
-- Triggered by the TCEQ MSW XLS loader, whose source emits a weekly-refresh
-- cadence via the Last-Modified header. Recording it separately from
-- schema_hash / row_count lets the drift detector distinguish "site
-- updated their cadence" from "site changed the schema" — both are
-- interesting signals but require different responses.
--
-- Column shape: TEXT, NULL allowed, no default, no constraints.
-- Loaders that do not receive a Last-Modified header on the response leave
-- this NULL (per the loader-side fallback that records bytes anyway via
-- response_byte_size).
-- =============================================================================

BEGIN;

ALTER TABLE source_signature
    ADD COLUMN last_modified TEXT;

COMMENT ON COLUMN source_signature.last_modified IS
    'Raw Last-Modified HTTP header captured at fetch time (RFC 7231 format, '
    'e.g. "Fri, 09 May 2026 12:34:56 GMT"). NULL when the server does not '
    'emit one. Stored as TEXT so malformed headers do not break the insert; '
    'parsing to TIMESTAMPTZ happens at the application layer in the drift '
    'detector.';

COMMIT;

-- =============================================================================
-- End of 20260511221000_add_last_modified_to_source_signature.sql
-- =============================================================================
