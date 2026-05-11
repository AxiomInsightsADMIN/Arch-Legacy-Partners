-- =============================================================================
-- 20260511220000_tceq_subsource_seed.sql
-- Arch Legacy Partners — Wastewater Facility Database
-- Phase 2: TCEQ sub-source seed (1 active row).
-- Authored 2026-05-11 by Axiom Insights.
--
-- DO NOT APPLY until Ryan has reviewed docs/tceq_pdl_audit.md.
-- Run after 0002_source_seed.sql (which seeds the umbrella TCEQ rows).
--
-- This migration adds dedicated source rows for the TCEQ sub-pages we will
-- *actively scrape* — each gets its own slug for drift-detection granularity
-- (per Ryan's instruction at the start of Phase 2).
--
-- Sources we are NOT seeding here (documented in docs/tceq_pdl_audit.md
-- with reasons):
--   - tceq_wqpaq, tceq_wq_dpa, tceq_wwps, tceq_steers
--     (all on robots-disallowed subdomains www2/www3/www6/www18; we honor
--      the disallow per locked decision 8.12)
--   - tceq_msw_closed_facilities_xls (historical; defer to Phase 6)
--   - tceq_msw_revoked_facilities_xls (revoked/denied; defer)
--   - Sludge / Septage transporter registry (not publicly listed on
--     www.tceq.texas.gov; falls to Phase-4.5 discovery)
--
-- Idempotent: ON CONFLICT (slug) DO UPDATE so re-running is safe.
-- =============================================================================

BEGIN;

INSERT INTO source (
    slug, name, type, base_url, tos_url, tos_posture, robots_txt_status, notes, last_checked_at
) VALUES

-- ---------------------------------------------------------------------------
-- TCEQ MSW Active Facilities XLS — the primary Phase-2 TCEQ data source.
-- Covers v1 categories 5 (composting), 7 (transfer stations), and 6
-- (anaerobic digesters with MSW classification).
-- ---------------------------------------------------------------------------
(
    'tceq_msw_facilities_xls',
    'TCEQ MSW Active Facilities (msw-facilities-texas.xls)',
    'state',
    'https://www.tceq.texas.gov/assets/public/permitting/waste/msw/msw-facilities-texas.xls',
    'https://www.tceq.texas.gov/help/policies/index.html',
    'permissive',
    'allow',
    'Weekly-refreshed XLS (BIFF) listing all issued/acknowledged MSW permits and '
    'registrations plus pending applications. Fields: facility name and type, '
    'permit/registration/notification number, authorization status, physical status, '
    'location. Schema reference: GI-613 publication '
    '(https://www.tceq.texas.gov/downloads/permitting/waste-permits/publications/gi-613-description-of-fields-msw-data-files.pdf). '
    'Feeds v1 categories 5 (composting), 6 (AD with MSW classification, partial), '
    '7 (transfer stations). robots.txt on www.tceq.texas.gov allows /assets/public/. '
    'Loader path: GET the XLS, parse with pandas + xlrd>=2.0.1, upsert one row per '
    'TCEQ permit/registration number into raw_facility_record with source_record_id = '
    'the permit/registration number. Entry hub: '
    'https://www.tceq.texas.gov/permitting/waste_permits/msw_permits/msw-data',
    NOW()
)

ON CONFLICT (slug) DO UPDATE SET
    name              = EXCLUDED.name,
    type              = EXCLUDED.type,
    base_url          = EXCLUDED.base_url,
    tos_url           = EXCLUDED.tos_url,
    tos_posture       = EXCLUDED.tos_posture,
    robots_txt_status = EXCLUDED.robots_txt_status,
    notes             = EXCLUDED.notes,
    last_checked_at   = EXCLUDED.last_checked_at;

COMMIT;

-- =============================================================================
-- End of 20260511220000_tceq_subsource_seed.sql
-- =============================================================================
